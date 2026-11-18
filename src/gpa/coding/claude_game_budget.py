"""Parse token usage from Messages responses."""

import json


class BudgetClosed(RuntimeError):
    pass


class ResponseUsage:
    """SSE usage values are cumulative, not per-chunk increments."""

    def __init__(self, streaming):
        self.streaming = streaming
        self.buffer = b""
        self.usage = {}
        self.complete = False
        self.protocol_error = False

    def feed(self, chunk):
        self.buffer += chunk
        if not self.streaming:
            return
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.startswith(b"data:"):
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "message_start":
                    self.usage.update(event.get("message", {}).get("usage", {}))
                elif kind == "message_delta":
                    self.usage.update(event.get("usage", {}))
                elif kind == "message_stop":
                    self.complete = True
                elif kind == "error":
                    self.protocol_error = True

    def finish(self):
        if self.streaming:
            if self.buffer:
                self.feed(b"\n")
        else:
            try:
                obj = json.loads(self.buffer)
            except ValueError:
                return
            if obj.get("type") == "message" and isinstance(obj.get("usage"), dict):
                self.usage = obj["usage"]
                self.complete = True
        if self.protocol_error:
            self.complete = False
