"""Write frame and event journals off the game thread."""

import hashlib
import json
from pathlib import Path
import queue
import threading
from collections import OrderedDict
from dataclasses import replace


class FrameStore:
    """Keep only the Mixed-history window in RAM; archived frames stay on disk."""

    def __init__(self, output, *, capacity=96):
        self.output = Path(output)
        self.capacity = capacity
        self.hot = OrderedDict()
        self.metadata = {}
        self.references = {}

    def __contains__(self, key):
        return key in self.metadata

    def __setitem__(self, key, frame):
        self.metadata[key] = replace(frame, data=b"")
        self.hot[key] = frame
        self.hot.move_to_end(key)
        while len(self.hot) > self.capacity:
            self.hot.popitem(last=False)

    def __getitem__(self, key):
        if key in self.hot:
            return self.hot[key]
        reference = self.references[key]
        return replace(self.metadata[key], data=(self.output / reference["path"]).read_bytes())


class Journal:
    def __init__(self, output):
        self.output = Path(output)
        self.queue = queue.Queue(maxsize=2048)
        self.error = None
        self.closed = False
        self.references = {}
        self.pending_image_bytes = 0
        self.byte_lock = threading.Lock()
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def put(self, kind, value):
        if self.closed:
            self.error = self.error or "write_after_close"
            return
        try:
            if kind == "image":
                with self.byte_lock:
                    if self.pending_image_bytes + len(value.data) > 32 * 1024**2:
                        self.error = "journal_image_memory_budget_exceeded"
                        return
                    self.pending_image_bytes += len(value.data)
            self.queue.put_nowait((kind, value))
        except queue.Full:
            if kind == "image":
                with self.byte_lock:
                    self.pending_image_bytes -= len(value.data)
            self.error = "journal_queue_overflow"

    def _write(self):
        streams = {}
        try:
            (self.output / "frames").mkdir(exist_ok=True)
            while True:
                item = self.queue.get()
                if item is None:
                    return
                kind, value = item
                if kind == "image":
                    sha = hashlib.sha256(value.data).hexdigest()
                    path = self.output / "frames" / (sha + ".jpg")
                    if not path.exists():
                        temporary = path.with_suffix(".next")
                        temporary.write_bytes(value.data)
                        temporary.replace(path)
                    self.references[value.identity] = {"path": str(path.relative_to(self.output)), "sha256": sha}
                    with self.byte_lock:
                        self.pending_image_bytes -= len(value.data)
                    kind, value = (
                        "frame_objects",
                        {
                            **value.public(),
                            "path": str(path.relative_to(self.output)),
                            "sha256": sha,
                            "capture_metadata": value.metadata,
                        },
                    )
                if kind not in streams:
                    streams[kind] = (self.output / (kind + ".jsonl")).open("a")
                streams[kind].write(json.dumps(value, allow_nan=False) + "\n")
                streams[kind].flush()
        except Exception as error:
            self.error = type(error).__name__
        finally:
            for stream in streams.values():
                stream.close()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.queue.put(None, timeout=10)
            except queue.Full:
                self.error = "journal_close_queue_full"
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                self.error = "journal_close_timeout"
