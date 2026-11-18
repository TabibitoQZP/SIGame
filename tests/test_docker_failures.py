import http.server
import json
import threading
import time

import pytest

from gpa.cli import run
from gpa.config import load_config
from gpa.execution.docker import docker

from conftest import ROOT

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("delay", [0, 2])
def test_model_error_or_read_timeout_cleans_developer_container(short_dir, monkeypatch, delay):
    class FaultyModels:
        def __init__(self):
            self.calls = []
            fixture = self

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    fixture.calls.append({"path": self.path, "body": body})
                    time.sleep(delay)
                    self.send_response(503)
                    self.end_headers()
                    try:
                        self.wfile.write(b'{"error":"injected model failure"}')
                    except BrokenPipeError:
                        pass

            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.url = "http://127.0.0.1:" + str(self.server.server_port)

        def __enter__(self):
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *_):
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)

    monkeypatch.setattr("gpa.testing.models.FakeModels", FaultyModels)
    config = load_config(ROOT / "configs/fake-single_agent.json")
    config.update(initial="cold", request_timeout_seconds=1)
    output = short_dir / "failed"
    result = run(config, output)
    assert result["status"] == "ERROR", result
    expected = "upstream_absolute_deadline" if delay else "claude_process_exit"
    assert expected in result["error"], result
    session = json.loads((output / "resources/session.json").read_text())
    assert not docker(["ps", "-aq", "--filter", "label=rgbgame.session=" + session["id"]]).stdout.strip()
    cleanup = json.loads((output / "resources/cleanup.json").read_text())
    assert cleanup["complete"]
    assert (output / "shared-initial/play/input_authority.signal").read_text() == "0"
