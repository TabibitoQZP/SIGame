"""Manage the game server and shared model services."""

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .docker import Container, docker
from .resources import ResourceSession, atomic_json, process_alive
from gpa.gameworld import default_assets, verify_assets


def game_service(session, *, assets=None):
    assets = Path(assets).resolve() if assets else default_assets()
    with (session.path / "game-service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = session.path / "game-service.json"
        if record.exists():
            value = json.loads(record.read_text())
            if value["assets"] != str(assets):
                raise ValueError("one resource session cannot mix asset roots")
            info = json.loads(docker(["inspect", value["container"]]).stdout)[0]
            if not info["State"]["Running"]:
                raise RuntimeError("game service exited")
            return value
        verification = verify_assets(assets)
        network = "rgbgame-games-" + session.id[:12]
        docker(["network", "create", "--label", "rgbgame.session=" + session.id, network])
        container = Container(
            session,
            None,
            "game",
            "rgbgame-browser:0.1.0",
            ["/opt/venv/bin/python", "/game_server.py"],
            mounts=[
                (assets, "/games", False),
                (Path(__file__).with_name("game_server.py"), "/game_server.py", False),
            ],
            network=network,
            aliases=["game"],
        ).start()
        value = {
            "assets": str(assets),
            "network": network,
            "container": container.name,
            "verification": verification,
        }
        atomic_json(record, value)
        return value


def state_root():
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "rgbgame"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def active_session():
    pointer = state_root() / "active.json"
    if not pointer.exists():
        return None
    record = json.loads(pointer.read_text())
    session = ResourceSession(record["session"])
    if not process_alive(session.config["owner_pid"]):
        raise RuntimeError("managed service owner exited; inspect " + str(session.path))
    session.check()
    return session


def configure_run(session, config):
    if config["backend"] == "gameworld":
        service = game_service(session, assets=config.get("game_assets"))
        config = {**config, "browser_network": service["network"]}
    return config


def up(
    *,
    output_limit=512 * 1024,
    model_concurrency=4,
    resume_session=None,
):
    if output_limit <= 0 or not 1 <= model_concurrency <= 4:
        raise ValueError("positive output limit and model concurrency 1..4 required")
    with (state_root() / "service-control.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _up(
            output_limit=output_limit,
            model_concurrency=model_concurrency,
            resume_session=resume_session,
        )


def _up(
    *,
    output_limit=512 * 1024,
    model_concurrency=4,
    resume_session=None,
):
    from .docker import preflight

    preflight()
    existing = active_session()
    if existing:
        if resume_session and existing.path != Path(resume_session).resolve():
            raise ValueError("another managed session is still active")
        if (
            existing.config["output_limit"] != output_limit
            or existing.config["model_concurrency"] != model_concurrency
        ):
            raise ValueError(
                "existing service budget differs; changing a live usage ledger is not allowed"
            )
        return {"status": "OK", "session": str(existing.path)}
    images = ["rgbgame-agent:0.1.0", "rgbgame-browser:0.1.0"]
    for image in images:
        if docker(["image", "inspect", image], check=False).returncode:
            raise RuntimeError(
                "missing " + image + "; run tools/build_runtime.py before services up"
            )
    if resume_session:
        resume_session = Path(resume_session).resolve()
        previous = ResourceSession(resume_session)
        if process_alive(previous.config["owner_pid"]):
            raise RuntimeError("cannot recover a live service owner")
        if (
            previous.config["output_limit"] != output_limit
            or previous.config["model_concurrency"] != model_concurrency
        ):
            raise ValueError("recovery must preserve the original model budget and concurrency")
        path = resume_session.parent
        archive = path / "control-history" / str(time.time_ns())
        archive.mkdir(parents=True)
        for name in ("ready.json", "failed.json", "shutdown.json", "daemon.log"):
            original = path / name
            if original.exists():
                original.replace(archive / name)
    else:
        path = state_root() / ("services-" + uuid.uuid4().hex[:12])
        path.mkdir(mode=0o700)
    command = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--property=MemoryMax=1920M",
        "--property=MemorySwapMax=0",
        sys.executable,
        "-m",
        __name__,
        str(path),
    ]
    command += [
        str(output_limit),
        str(model_concurrency),
        str(resume_session) if resume_session else "",
    ]
    with (path / "daemon.log").open("w") as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (path / "ready.json").exists():
            return json.loads((path / "ready.json").read_text())
        if (path / "failed.json").exists() or process.poll() is not None:
            raise RuntimeError("service startup failed; see " + str(path / "daemon.log"))
        time.sleep(0.2)
    raise TimeoutError("service startup timed out; see " + str(path))


def down():
    pointer = state_root() / "active.json"
    if not pointer.exists():
        return {"status": "OK", "message": "no managed services"}
    session = ResourceSession(json.loads(pointer.read_text())["session"])
    snapshot = session.snapshot()
    if not process_alive(session.config["owner_pid"]):
        containers = docker(
            ["ps", "-aq", "--filter", "label=rgbgame.session=" + session.id]
        ).stdout.split()
        if containers:
            docker(["rm", "-f", *containers])
        networks = docker(
            ["network", "ls", "-q", "--filter", "label=rgbgame.session=" + session.id]
        ).stdout.split()
        if networks:
            docker(["network", "rm", *networks])
        pointer.unlink(missing_ok=True)
        return {
            "status": "OK",
            "session": str(session.path),
            "message": "stale service resources cleaned; usage ledger retained",
        }
    if snapshot["groups"] or any(
        row["kind"] != "game" for row in snapshot["allocations"]
    ):
        raise RuntimeError(
            "active evaluations still own resource groups; finish or cancel them first"
        )
    atomic_json(session.path.parent / "shutdown.json", {"reason": "services_down"})
    deadline = time.monotonic() + 30
    while process_alive(session.config["owner_pid"]) and time.monotonic() < deadline:
        time.sleep(0.2)
    if process_alive(session.config["owner_pid"]):
        raise TimeoutError("service shutdown not confirmed; see " + str(session.path))
    pointer.unlink(missing_ok=True)
    return {"status": "OK", "session": str(session.path)}


def daemon(
    path, output_limit=512 * 1024, model_concurrency=4, resume_session=None
):
    import traceback

    path = Path(path)
    session = (
        ResourceSession.recover(resume_session)
        if resume_session
        else ResourceSession.create(
            path / "resources",
            output_limit=int(output_limit),
            model_concurrency=int(model_concurrency),
        )
    ).start()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        game_service(session)
        value = {"status": "OK", "session": str(session.path)}
        atomic_json(state_root() / "active.json", value)
        atomic_json(path / "ready.json", value)
        while not stopping and not (path / "shutdown.json").exists():
            session.check()
            time.sleep(0.5)
    except BaseException:
        atomic_json(path / "failed.json", {"error": traceback.format_exc()})
        raise
    finally:
        session.close()
        pointer = state_root() / "active.json"
        if pointer.exists() and json.loads(pointer.read_text())["session"] == str(session.path):
            pointer.unlink()


if __name__ == "__main__":
    daemon(*sys.argv[1:])
