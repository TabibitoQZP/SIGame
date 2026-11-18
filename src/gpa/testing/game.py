"""Small continuously updating RGB fixture with real monotonic timestamps."""

import io
import threading
import time
from PIL import Image, ImageDraw
from gpa.realtime.runtime import Frame
from gpa.realtime.browser import BrowserAdapter


class FakeGame:
    capabilities = {**BrowserAdapter.capabilities, "adapter": "synthetic_fixture_not_a_benchmark"}

    def __init__(self):
        self.stop = threading.Event()
        self.epoch = self.sequence = self.position = 0
        self.lock = threading.Lock()

    def start(self, receive, emit):
        self.receive, self.emit = receive, emit
        self.thread = threading.Thread(target=self.capture, daemon=True)
        self.thread.start()

    def capture(self):
        while not self.stop.is_set():
            with self.lock:
                image = Image.new("RGB", (160, 90), (20, 30, 40))
                draw = ImageDraw.Draw(image)
                x = 10 + self.position % 120
                draw.rectangle((x, 40, x + 10, 50), fill=(220, 40, 40))
                draw.text((4, 4), "FAKE RGB TEST", fill="white")
                data = io.BytesIO()
                image.save(data, format="JPEG")
                self.sequence += 1
                frame = Frame(data.getvalue(), time.monotonic_ns(), self.epoch, self.sequence)
            self.receive(frame)
            self.stop.wait(0.04)

    def dispatch(self, action, receipt):
        if self.stop.is_set() or time.monotonic_ns() >= receipt["window_end_ns"]:
            return False
        with self.lock:
            if action.kind == "reset":
                self.epoch = receipt["epoch"]
                self.position = 0
            else:
                self.position += 5 if "right" in action.keys else -5 if "left" in action.keys else 0
        self.emit("input_sent", **receipt, action=action.record())
        return True

    def release(self, reason):
        self.emit("input_released", reason=reason)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
