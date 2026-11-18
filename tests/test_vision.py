import http.server
import json
import threading


from gpa.coding.vision import perceive

def test_jev_uses_native_state_and_choice_probabilities(monkeypatch):
    class Native(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/systemone"
            assert self.headers["Authorization"] == "Bearer jev-test-key"
            image = body["state"]["screen"]["image"]["url"]
            assert image.startswith("data:image/png;base64,")
            assert body["state"]["context"] == "Look only at RGB"
            assert body["questions"]["target"]["type"] == "choice"
            value = {
                "model": "vev-4b",
                "answers": {
                    "target": {
                        "type": "choice",
                        "choice": "A",
                        "probabilities": {"A": 0.8, "B": 0.2},
                    },
                    "visible": {"type": "noul", "noul": 0.9},
                },
            }
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Native)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("JEV_API_KEY", "jev-test-key")
    try:
        cfg = {"endpoints": {"jev": {"url": f"http://127.0.0.1:{server.server_port}"}}}
        questions = {
            "target": {
                "type": "choice",
                "instructions": "Which side?",
                "criteria": {"A": "left", "B": "right"},
            },
            "visible": {"type": "noul", "instructions": "Is target visible?"},
        }
        image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jvMsAAAAASUVORK5CYII="
        result = perceive(
            cfg,
            "jev",
            {
                "image": image,
                "questions": questions,
                "instructions": "Look only at RGB",
            },
        )
        assert result["answers"]["target"]["probabilities"] == {"A": 0.8, "B": 0.2}
        assert result["answers"]["visible"]["noul"] == 0.9
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
