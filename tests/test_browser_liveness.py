import threading
import time

import pytest

from gpa.adapters.gameworld170.browser import BrowserAdapter as ExtendedBrowser
from gpa.commands import wait_browser
from gpa.execution.docker import Container, docker
from gpa.execution.resources import ResourceSession
from gpa.realtime.browser import BrowserAdapter
from gpa.realtime.runtime import Runtime

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("adapter_class", [BrowserAdapter, ExtendedBrowser])
@pytest.mark.parametrize("kill_browser", [False, True])
def test_browser_loss_stops_runtime_and_normal_close_is_allowed(tmp_path, adapter_class, kill_browser):
    class WaitPolicy:
        def can_request(self):
            return False

        def close(self):
            pass

    session = ResourceSession.create(tmp_path / "resources").start()
    container = None
    runtime = None
    thread = None
    result = []
    try:
        container = Container(
            session, None, "browser", "rgbgame-browser:0.1.0",
            ["/opt/venv/bin/python", "/opt/rgbgame/browser_server.py"],
            network="bridge", ports=["9222/tcp"],
        ).start()
        adapter = adapter_class("data:text/html,<html><body style='background:green'>liveness</body></html>")
        adapter.remote_cdp = wait_browser(container)
        runtime = Runtime(adapter, WaitPolicy(), tmp_path / "play", ticks=50 if kill_browser else 2)
        thread = threading.Thread(target=lambda: result.append(runtime.run()))
        thread.start()
        ready_until = time.monotonic() + 20
        while runtime.origin_ns is None and thread.is_alive() and time.monotonic() < ready_until:
            time.sleep(0.05)
        assert runtime.origin_ns is not None, result
        if kill_browser:
            docker(["kill", container.name])
        thread.join(6)
        assert not thread.is_alive(), "a dead browser must not keep the game clock running"
        assert (tmp_path / "play/input_authority.signal").read_text() == "0"
        if kill_browser:
            assert result[0]["error"]["origin"] == "host", result
            assert result[0]["real_seconds"] < 6
            assert result[0]["termination"] == "host_error"
            assert any(r["event"] == "adapter_error" for r in runtime.events)
        else:
            assert result[0]["error"] is None, result
            assert result[0]["termination"] == "time_budget"
    finally:
        if runtime:
            with runtime.condition:
                runtime.stop.set()
                runtime.condition.notify_all()
        if thread:
            thread.join(6)
        try:
            if container:
                container.close()
        finally:
            session.close()
