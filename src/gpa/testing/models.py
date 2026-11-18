"""Loopback HTTP fixtures for the actual OpenAI/SSE and Messages transports."""

import http.server
import json
import re
import threading
import time


class FakeModels:
    def __init__(self, *, delay=0, missing_usage=False):
        self.calls = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls.append({"path": self.path, "body": body})
                if delay:
                    time.sleep(delay)
                if self.path == "/v1/chat/completions":
                    match = re.search(r"(?:exactly|up to) (\d+) tick", json.dumps(body), re.I)
                    count = int(match.group(1)) if match else 15
                    content = "\n".join(["(press(right))"] * count)
                    chunk = {
                        "id": "fake-completion",
                        "model": body["model"],
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": content,
                                    "reasoning_content": "Synthetic fixture reasoning.",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    if not missing_usage:
                        chunk["usage"] = {
                            "prompt_tokens": 80,
                            "completion_tokens": 40,
                            "total_tokens": 120,
                        }
                    raw = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
                    kind = "text/event-stream"
                elif self.path == "/v1/messages/count_tokens":
                    raw, kind = json.dumps({"input_tokens": 25}).encode(), "application/json"
                elif self.path == "/v1/messages":
                    review = "review" in str(body.get("system", ""))
                    result = {
                        "id": "fake-message",
                        "type": "message",
                        "role": "assistant",
                        "model": body["model"],
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "Inspect the RGB evidence first.",
                                "signature": "fake",
                            },
                            {
                                "type": "tool_use",
                                "id": "fake-tool",
                                "name": "write_review" if review else "submit",
                                "input": {
                                    "content": "Inspected peer policy and frame evidence. Try a shorter movement. [fake]"
                                }
                                if review
                                else {},
                            },
                        ],
                        "stop_reason": "tool_use",
                        "stop_sequence": None,
                    }
                    if not missing_usage:
                        result["usage"] = {
                            "input_tokens": 100,
                            "output_tokens": min(48, body["max_tokens"]),
                        }
                    if body.get("stream"):
                        start = {
                            **result,
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 100, "output_tokens": 0},
                        }
                        events = [{"type": "message_start", "message": start}]
                        for index, block in enumerate(result["content"]):
                            if block["type"] == "thinking":
                                events += [
                                    {
                                        "type": "content_block_start",
                                        "index": index,
                                        "content_block": {"type": "thinking", "thinking": ""},
                                    },
                                    {
                                        "type": "content_block_delta",
                                        "index": index,
                                        "delta": {
                                            "type": "thinking_delta",
                                            "thinking": block["thinking"],
                                        },
                                    },
                                    {
                                        "type": "content_block_delta",
                                        "index": index,
                                        "delta": {
                                            "type": "signature_delta",
                                            "signature": block["signature"],
                                        },
                                    },
                                ]
                            else:
                                events += [
                                    {
                                        "type": "content_block_start",
                                        "index": index,
                                        "content_block": {**block, "input": {}},
                                    },
                                    {
                                        "type": "content_block_delta",
                                        "index": index,
                                        "delta": {
                                            "type": "input_json_delta",
                                            "partial_json": json.dumps(block["input"]),
                                        },
                                    },
                                ]
                            events.append({"type": "content_block_stop", "index": index})
                        events.append(
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                                "usage": {}
                                if missing_usage
                                else {"output_tokens": min(48, body["max_tokens"])},
                            }
                        )
                        events.append({"type": "message_stop"})
                        raw = "".join(
                            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                        ).encode()
                        kind = "text/event-stream"
                    else:
                        raw, kind = json.dumps(result).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
