"""Build the game and policy runtime images."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

from gpa.execution.docker import docker, preflight
from gpa.execution.resources import GIB, ResourceSession, atomic_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=["agent", "browser", "all"], default="all")
    parser.add_argument(
        "--cache-volume",
        default="rgbgame-build-cache",
        help="Docker volume retaining the trusted BuildKit cache",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    preflight()
    session = ResourceSession.create(args.output / "resources").start()
    lock = json.loads((ROOT / "configs/runtime-lock.json").read_text())
    name = "rgbgame-buildkit-" + uuid.uuid4().hex[:10]
    builder = "rgbgame-remote-" + uuid.uuid4().hex[:10]
    allocation = None
    try:
        allocation = session.allocate(name, "build", 3 * GIB, name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [
            "run",
            "-d",
            "--name",
            name,
            "--privileged",
            "--network=host",
            "--label",
            "rgbgame.session=" + session.id,
            "--label",
            "rgbgame.role=trusted-buildkit",
            "--cgroup-parent=rgbgame-eval.slice",
            "--memory=3g",
            "--memory-swap=3g",
            "--cpus=4",
            "--pids-limit=256",
            "--mount",
            "type=volume,src=" + args.cache_volume + ",dst=/var/lib/buildkit",
        ]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if os.environ.get(key):
                command += ["--env", key]
        command += [
            lock["buildkit_image"],
            "--addr",
            f"tcp://127.0.0.1:{port}",
            "--allow-insecure-entitlement",
            "network.host",
        ]
        docker(command, timeout=300)
        info = json.loads(docker(["inspect", name]).stdout)[0]
        session.attach(allocation, info["State"]["Pid"])
        assert info["HostConfig"]["Memory"] == 3 * GIB
        assert info["HostConfig"]["CgroupParent"] == "rgbgame-eval.slice"
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                session.check()
                time.sleep(0.1)
        docker(
            ["buildx", "create", "--name", builder, "--driver", "remote", f"tcp://127.0.0.1:{port}"]
        )
        targets = ["agent", "browser"] if args.target == "all" else [args.target]
        receipts = {}
        for target in targets:
            command = [
                "/usr/bin/docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--output",
                "type=docker,dest=" + str((args.output / (target + ".tar")).resolve()),
                "--progress=plain",
                "--network=host",
                "--allow=network.host",
                "--target",
                target,
                "--build-arg",
                "CLAUDE_CODE_VERSION=" + lock["claude_code_version"],
                "--tag",
                lock[target + "_image"],
                "--file",
                str(ROOT / "docker/Dockerfile"),
            ]
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
            ):
                if os.environ.get(key):
                    command += ["--build-arg", key]
            command += [str(ROOT)]
            with (args.output / (target + ".log")).open("w") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                try:
                    while process.poll() is None:
                        session.check()
                        time.sleep(1)
                    if process.returncode:
                        raise RuntimeError(f"{target} image build failed; see {log.name}")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
            archive = args.output / (target + ".tar")
            docker(["load", "--input", str(archive.resolve())], timeout=600)
            archive.unlink()
            receipts[target] = json.loads(
                docker(
                    ["image", "inspect", lock[target + "_image"], "--format", "{{json .Id}}"]
                ).stdout
            )
            atomic_json(args.output / "images.json", receipts)
            print(target, receipts[target], flush=True)
    finally:
        docker(["buildx", "rm", builder], check=False)
        docker(["rm", "-f", name], check=False)
        if allocation:
            session.release(allocation)
        session.close()


if __name__ == "__main__":
    main()
