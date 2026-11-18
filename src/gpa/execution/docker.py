"""Docker workloads with mandatory cgroup limits and session-owned cleanup."""

import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from gpa.config import DEFAULT_BROWSER_MEMORY_MIB
from .resources import GIB, MIB, ResourceSession, ResourceStopped, atomic_json

LIMITS = {
    "developer": (1536 * MIB, 1, 256),
    "browser": (DEFAULT_BROWSER_MEMORY_MIB * MIB, 4, 512),
    "policy": (768 * MIB, 1, 64),
    "game": (512 * MIB, 1, 64),
    "vision": (6 * GIB, 4, 256),
    "build": (3 * GIB, 4, 256),
    "probe": (256 * MIB, 1, 64),
}


def docker(arguments, *, check=True, timeout=60, **kwargs):
    return subprocess.run(
        ["/usr/bin/docker", *arguments],
        check=check,
        timeout=timeout,
        text=True,
        capture_output=True,
        **kwargs,
    )


def missing_container(result):
    message = result.stderr.lower()
    return "no such object" in message or "no such container" in message


def preflight():
    info = json.loads(docker(["info", "--format", "{{json .}}"], timeout=10).stdout)
    if info.get("CgroupVersion") != "2" or not info.get("MemoryLimit") or not info.get("SwapLimit"):
        raise ResourceStopped("Docker must support cgroup v2 memory and swap limits")
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "rgbgame-eval.slice",
            "-p",
            "MemoryMax",
            "-p",
            "MemorySwapMax",
            "-p",
            "MemoryHigh",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    if (
        values.get("MemorySwapMax") != "0"
        or values.get("MemoryMax") == "infinity"
        or not 0 < int(values.get("MemoryMax", "0")) <= 16 * GIB
    ):
        raise ResourceStopped("configure rgbgame-eval.slice memory limits before evaluation")
    return {
        "server": info["ServerVersion"],
        "cgroup_version": info["CgroupVersion"],
        "parent_limits": values,
    }


class Container:
    def __init__(
        self,
        session,
        group,
        kind,
        image,
        command,
        *,
        mounts=(),
        env=None,
        network="none",
        ports=(),
        aliases=(),
        gpu=False,
        workdir=None,
        memory=None,
        prepaid=None,
        cpus=None,
        tmpfs=(),
    ):
        self.session = session
        self.name = "rgbgame-" + kind + "-" + uuid.uuid4().hex[:12]
        self.kind, self.image = kind, image
        self.allocation = None
        self.closed = False
        limit, default_cpus, pids = LIMITS[kind]
        cpus = default_cpus if cpus is None else cpus
        limit = memory or limit
        if kind in {"developer", "policy"} and network != "none":
            raise ValueError("untrusted workloads require network=none")
        if type(limit) is not int or limit < 6 * MIB:
            raise ValueError("invalid container memory limit")
        args = [
            "create",
            "--init",
            "--name",
            self.name,
            "--label",
            "rgbgame.session=" + session.id,
            "--label",
            "rgbgame.role=" + kind,
            "--cgroup-parent",
            session.config["cgroup_parent"],
            "--memory",
            str(limit),
            "--memory-swap",
            str(limit),
            "--cpus",
            str(cpus),
            "--pids-limit",
            str(pids),
            "--ulimit",
            "nofile=65536:65536",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=10m",
            "--log-opt",
            "max-file=2",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--network",
            network,
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={512 * MIB if kind == 'browser' else 128 * MIB},mode=1777",
            "--shm-size",
            "256m",
            "-i",
        ]
        for source, destination, writable in mounts:
            source = Path(source).resolve(strict=True)
            if "," in str(source) or "," in str(destination):
                raise ValueError("commas are not supported in container mount paths")
            args += [
                "--mount",
                f"type=bind,src={source},dst={destination}" + ("" if writable else ",readonly"),
            ]
        for destination, size in tmpfs:
            if not Path(destination).is_absolute() or type(size) is not int or size <= 0:
                raise ValueError("invalid bounded tmpfs mount")
            args += ["--tmpfs", f"{destination}:rw,nosuid,nodev,size={size},mode=1777"]
        for port in ports:
            args += ["--publish", f"127.0.0.1::{port}"]
        for alias in aliases:
            args += ["--network-alias", alias]
        self.environment = {**os.environ, **(env or {})}
        for key in env or {}:
            args += ["--env", key]
        if gpu:
            args += ["--gpus", "all"]
        if workdir:
            args += ["--workdir", str(workdir)]
        args += [image, *command]
        if prepaid:
            session.validate_play(prepaid, group, kind=kind, memory=limit)
        self.allocation = session.allocate(
            group or self.name,
            kind + "_prepaid" if prepaid else kind,
            0 if prepaid else limit,
            self.name,
        )
        try:
            docker(args, env=self.environment)
            atomic_json(
                session.path / (self.name + ".json"),
                {
                    "name": self.name,
                    "kind": kind,
                    "image": image,
                    "memory": limit,
                    "group": group,
                    "network": network,
                    "created": time.time(),
                    "cpu_limit": cpus,
                    "pid_limit": pids,
                    "nofile_limit": 65536,
                    "image_id": self.inspect()["Image"],
                },
            )
        except BaseException:
            self.close()
            raise

    def inspect(self):
        return json.loads(docker(["inspect", self.name]).stdout)[0]

    def start(self):
        try:
            docker(["start", self.name])
            state = self.inspect()["State"]
            if state.get("Pid"):
                self.session.attach(self.allocation, state["Pid"])
            else:
                raise ResourceStopped("container_exited_during_start:" + self.name)
        except BaseException:
            self.close()
            raise
        return self

    def port(self, port):
        value = docker(["port", self.name, str(port)]).stdout.strip()
        return value.splitlines()[0]

    def exec_command(self, command, env=None, cwd=None):
        args = ["/usr/bin/docker", "exec", "-i"]
        if cwd:
            args += ["--workdir", str(cwd)]
        for key in env or {}:
            args += ["--env", key]
        return [*args, self.name, *command]

    def run(self, command, *, env=None, cwd=None, timeout=60):
        return subprocess.run(
            self.exec_command(command, env, cwd),
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def close(self):
        if self.closed:
            return
        try:
            result = docker(["inspect", self.name], check=False)
            if result.returncode == 0:
                info = json.loads(result.stdout)[0]
                logs = docker(["logs", "--tail", "100", self.name], check=False)
                (self.session.path / (self.name + ".log")).write_text(
                    (logs.stdout + logs.stderr)[-1024 * 1024 :]
                )
                initial_state = info["State"]
                if initial_state.get("Running"):
                    docker(["stop", "--time", "1", self.name], check=False, timeout=10)
                    final = docker(["inspect", self.name], check=False)
                    if final.returncode == 0:
                        info = json.loads(final.stdout)[0]
                    elif missing_container(final):
                        info = {**info, "State": {"Status": "removed_by_supervisor"}}
                    else:
                        raise ResourceStopped("cannot_confirm_container_cleanup:" + self.name)
                atomic_json(
                    self.session.path / (self.name + ".exit.json"),
                    {
                        "state": info["State"],
                        "before_cleanup": initial_state,
                        "memory": info["HostConfig"]["Memory"],
                        "memory_swap": info["HostConfig"]["MemorySwap"],
                        "cgroup_parent": info["HostConfig"]["CgroupParent"],
                    },
                )
                docker(["rm", "-f", self.name], check=False)
            elif not missing_container(result):
                raise ResourceStopped("cannot_confirm_container_cleanup:" + self.name)
            remaining = docker(["ps", "-aq", "--filter", "name=^/" + self.name + "$"], timeout=10)
            if remaining.stdout.strip():
                raise ResourceStopped("container_cleanup_incomplete:" + self.name)
        except BaseException:
            path = self.session.path / "abort.json"
            if not path.exists():
                atomic_json(path, {"reason": "container_cleanup_failed", "container": self.name})
            atomic_json(
                self.session.path / (self.name + ".cleanup-error.json"),
                {"reason": "container_cleanup_failed", "container": self.name},
            )
            raise
        else:
            self.session.release(self.allocation)
            self.closed = True


class ModelBridge:
    """A Unix-only route to exactly one trusted localhost HTTP proxy."""

    def __init__(self, path, port):
        import socketserver
        import socket
        import select
        import threading

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection(("127.0.0.1", port), timeout=10) as target:
                    target.settimeout(None)
                    peers = {self.request: target, target: self.request}
                    while True:
                        ready, _, _ = select.select(list(peers), [], [], 2)
                        if not ready:
                            if stopping.is_set():
                                return
                            continue
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                return
                            peers[source].sendall(data)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        stopping = self.stopping = threading.Event()
        self.path = Path(path)
        self.server = Server(str(path), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.path.unlink(missing_ok=True)


def current_session():
    return ResourceSession.current()
