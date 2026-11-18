"""Enforce a wall-clock deadline on streaming HTTP responses."""

import socket
import threading
import time
import urllib.request

from gpa.coding.systemone_gateway_verify import HTTPConnection, HTTPSConnection


class Deadline:
    def __init__(self, seconds, expired):
        if seconds <= 0:
            raise ValueError("positive_request_deadline_required")
        self.end = time.monotonic() + seconds
        self.callback = expired
        self.lock = threading.Lock()
        self.sockets = []
        self.expired = False
        self.finished = False
        self.timer = threading.Timer(seconds, self.cancel)
        self.timer.daemon = True
        self.timer.start()

    @staticmethod
    def shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def register(self, sock):
        with self.lock:
            self.sockets.append(sock)
            expired = self.expired
        if expired:
            self.shutdown(sock)
            raise TimeoutError("absolute_upstream_deadline")

    def cancel(self):
        with self.lock:
            if self.finished or self.expired:
                return
            self.expired = True
            sockets = list(self.sockets)
        self.callback()
        for sock in sockets:
            self.shutdown(sock)

    def finish(self):
        with self.lock:
            self.finished = True
        self.timer.cancel()
        self.timer.join(timeout=5)
        if self.timer.is_alive():
            raise RuntimeError("deadline_cleanup_incomplete")

    def opener(self):
        deadline = self

        class Capture:
            def connect(self):
                super().connect()
                deadline.register(self.sock)

        class HTTP(Capture, HTTPConnection):
            pass

        class HTTPS(Capture, HTTPSConnection):
            pass

        class Plain(urllib.request.HTTPHandler):
            def http_open(self, request):
                return self.do_open(HTTP, request)

        class TLS(urllib.request.HTTPSHandler):
            def https_open(self, request):
                return self.do_open(HTTPS, request, context=self._context)

        return urllib.request.build_opener(Plain(), TLS())
