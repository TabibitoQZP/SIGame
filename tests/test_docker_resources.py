"""Exercise Docker memory limits and container cleanup."""

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from gpa.execution.docker import Container, docker, preflight
from gpa.execution.resources import MIB, ResourceSession


@pytest.mark.docker
def test_actual_cgroup_limits_oom_and_owned_cleanup(tmp_path):
    preflight()
    session = ResourceSession.create(tmp_path / "resources").start()
    name = "rgbgame-control-test-" + uuid.uuid4().hex[:10]
    external = False
    probe = None
    try:
        docker(["run", "-d", "--name", name, "--memory=64m", "--memory-swap=64m", "--network=none", "--user=1000:1000", "python:3.12-slim-bookworm", "python", "-c", "import time; time.sleep(90)"])
        external = True
        probe = Container(session, None, "probe", "python:3.12-slim-bookworm", ["python", "-c", "import time; time.sleep(90)"], memory=64 * MIB).start()
        info = probe.inspect()
        assert info["HostConfig"]["Memory"] == info["HostConfig"]["MemorySwap"] == 64 * MIB
        assert info["HostConfig"]["CgroupParent"] == "rgbgame-eval.slice"
        result = probe.run(["python", "-c", "import os; print(os.getuid()); print(bool(os.environ.get('DEEPSEEK_API_KEY')))"])
        assert result.stdout.splitlines() == [str(os.getuid()), "False"]
        probe.run(["python", "-c", "a=bytearray(256*1024*1024)"], timeout=20)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (session.path / "abort.json").exists():
            time.sleep(0.1)
        assert (session.path / "abort.json").exists()
        assert "oom" in json.loads((session.path / "abort.json").read_text())["reason"]
        assert docker(["inspect", name, "--format", "{{.State.Running}}" ]).stdout.strip() == "true"
    finally:
        try:
            if probe:
                probe.close()
        finally:
            try:
                session.close()
            finally:
                if external:
                    docker(["rm", "-f", name], check=False)


@pytest.mark.docker
def test_watchdog_survives_controller_scope_oom_and_removes_only_owned_containers(tmp_path):
    resource_path = tmp_path / "orphan-resources"
    receipt = tmp_path / "ready.json"
    control = "rgbgame-control-oom-" + uuid.uuid4().hex[:10]
    script = f"""
import json, os
from pathlib import Path
from gpa.execution.resources import ResourceSession, MIB
from gpa.execution.docker import Container
session = ResourceSession.create({str(resource_path)!r}).start()
probe = Container(session, None, 'probe', 'python:3.12-slim-bookworm', ['python', '-c', 'import time; time.sleep(60)'], memory=64*MIB).start()
watchdog_cgroup = Path('/proc').joinpath(str(session.watchdog.pid),'cgroup').read_text()
watchdog_memory = Path('/sys/fs/cgroup').joinpath(watchdog_cgroup.strip().split(':',2)[2].lstrip('/'),'memory.max').read_text()
Path({str(receipt)!r}).write_text(json.dumps({{'id':session.id,'container':probe.name,'owner_cgroup':Path('/proc/self/cgroup').read_text(),'watchdog_cgroup':watchdog_cgroup,'watchdog_memory_max':int(watchdog_memory)}}))
allocation = bytearray(512 * MIB)
"""
    docker(["run", "-d", "--name", control, "--memory=64m", "--memory-swap=64m", "--network=none", "--user=1000:1000", "python:3.12-slim-bookworm", "python", "-c", "import time; time.sleep(60)"])
    try:
        completed = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect",
             "--property=MemoryMax=256M", "--property=MemorySwapMax=0",
             sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert completed.returncode in {-9, -15, 137, 143}, completed.stderr
        assert receipt.exists(), completed.stderr
        record = json.loads(receipt.read_text())
        assert record["owner_cgroup"] != record["watchdog_cgroup"]
        assert record["watchdog_memory_max"] == 128 * MIB
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (resource_path / "cleanup.json").exists():
            time.sleep(0.1)
        assert json.loads((resource_path / "cleanup.json").read_text())["complete"]
        assert json.loads((resource_path / "abort.json").read_text())["reason"] == "controller_disappeared"
        assert not docker(["ps", "-aq", "--filter", "label=rgbgame.session=" + record["id"]]).stdout.strip()
        assert docker(["inspect", control, "--format", "{{.State.Running}}"]).stdout.strip() == "true"
    finally:
        docker(["rm", "-f", control], check=False)
