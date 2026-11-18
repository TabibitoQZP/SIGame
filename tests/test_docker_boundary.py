"""Check container mount and network boundaries."""

import json
from pathlib import Path

import pytest

from gpa.execution.docker import Container, preflight
from gpa.execution.resources import ResourceSession

pytestmark = pytest.mark.docker


def test_developer_cannot_read_controller_games_peers_keys_or_network(tmp_path):
    preflight()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = [workspace / name for name in ("evidence", "versions", "peers")]
    for folder in protected:
        folder.mkdir()
        (folder / "receipt.txt").write_text("controller-owned")
    private = tmp_path / "private"
    private.mkdir()
    (private / "score.json").write_text('{"secret_score":99}')
    peer = tmp_path / "peer"
    peer.mkdir()
    (peer / "policy.py").write_text("secret_peer = True")
    session = ResourceSession.create(tmp_path / "resources").start()
    probe = None
    try:
        probe = Container(
            session,
            None,
            "developer",
            "rgbgame-agent:0.1.0",
            ["/opt/venv/bin/python", "-c", "import time; time.sleep(90)"],
            mounts=[(workspace, "/work", True)]
            + [(folder, "/work/" + folder.name, False) for folder in protected],
        ).start()
        script = f"""
import importlib.util, json, os, pathlib, socket
result = {{}}
for name, path in {{"private":{str(private / "score.json")!r}, "peer":{str(peer / "policy.py")!r}, "docker":"/var/run/docker.sock", "games":"/games/benchmark", "controller":"/app/src/gpa"}}.items():
    result[name] = pathlib.Path(path).exists()
result["controller_import"] = importlib.util.find_spec("gpa") is not None
result["real_keys"] = [key for key in ("DEEPSEEK_API_KEY", "CV_API_KEY", "ANTHROPIC_API_KEY") if os.environ.get(key)]
for name, address in {{"external":("1.1.1.1",443), "cdp":("127.0.0.1",9222), "game":("game",8000)}}.items():
    try:
        connection=socket.create_connection(address,timeout=1);connection.close();result[name]=True
    except OSError:
        result[name]=False
print(json.dumps(result))
"""
        result = probe.run(["/opt/venv/bin/python", "-c", script])
        assert result.returncode == 0, result.stderr
        checks = json.loads(result.stdout)
        assert not any(checks.values()), checks
        info = probe.inspect()
        assert info["Config"]["User"] != "0"
        assert info["HostConfig"]["ReadonlyRootfs"]
        assert info["HostConfig"]["CapDrop"] == ["ALL"]
        assert info["HostConfig"]["NetworkMode"] == "none"
        readonly_probe = """
import errno, pathlib
for name in ('evidence', 'versions', 'peers'):
    path = pathlib.Path('/work') / name / 'receipt.txt'
    for operation in (lambda: path.write_text('modified'), lambda: path.chmod(0o777)):
        try:
            operation()
        except OSError as error:
            assert error.errno == errno.EROFS, (name, error)
        else:
            raise AssertionError(name + ' is writable')
    assert path.read_text() == 'controller-owned'
"""
        result = probe.run(["/opt/venv/bin/python", "-c", readonly_probe])
        assert result.returncode == 0, result.stderr
    finally:
        if probe:
            probe.close()
        session.close()


def test_policy_scratch_is_bounded_tmpfs(tmp_path):
    session = ResourceSession.create(tmp_path / "resources").start()
    probe = None
    try:
        probe = Container(
            session,
            None,
            "policy",
            "rgbgame-agent:0.1.0",
            ["/opt/venv/bin/python", "-c", "import time; time.sleep(90)"],
            tmpfs=[("/scratch", 64 * 1024**2)],
        ).start()
        script = """
import errno, os
assert os.statvfs('/scratch').f_blocks * os.statvfs('/scratch').f_frsize == 64 * 1024**2
try:
    with open('/scratch/fill', 'wb') as stream:
        for _ in range(80):
            stream.write(b'x' * 1024**2)
except OSError as error:
    assert error.errno == errno.ENOSPC, error
else:
    raise AssertionError('scratch exceeded its 64MiB bound')
os.unlink('/scratch/fill')
"""
        result = probe.run(["/opt/venv/bin/python", "-c", script])
        assert result.returncode == 0, result.stderr
        assert probe.inspect()["State"]["Running"]
    finally:
        if probe:
            probe.close()
        session.close()


def test_real_policy_seccomp_denies_exec_and_ip_socket(tmp_path):
    source = Path(__file__).parents[1] / "src/gpa/coding/realtime_policy_worker.py"
    session = ResourceSession.create(tmp_path / "resources").start()
    probe = None
    try:
        script = """
import importlib.util, json, socket, subprocess
spec=importlib.util.spec_from_file_location("worker","/worker.py")
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.restrict()
checks={}
for name, fn in {"exec":lambda:subprocess.run(["/bin/true"]), "ip_socket":lambda:socket.socket(socket.AF_INET)}.items():
    try: fn();checks[name]=False
    except PermissionError: checks[name]=True
print(json.dumps(checks))
"""
        probe = Container(
            session,
            None,
            "policy",
            "rgbgame-agent:0.1.0",
            ["/opt/venv/bin/python", "-c", "import time; time.sleep(90)"],
            mounts=[(source, "/worker.py", False)],
        ).start()
        result = probe.run(["/opt/venv/bin/python", "-c", script])
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"exec": True, "ip_socket": True}
    finally:
        if probe:
            probe.close()
        session.close()
