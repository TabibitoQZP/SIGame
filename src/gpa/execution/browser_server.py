"""Trusted Chromium endpoint; reachable only by the controller and game network."""

import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import threading

os.environ["HOME"] = "/tmp"
os.environ["XDG_CACHE_HOME"] = "/tmp/chromium-cache"
os.environ["XDG_CONFIG_HOME"] = "/tmp/chromium-config"
executables = list(
    Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/ms-playwright")).glob(
        "chromium-*/chrome-linux*/chrome"
    )
)
if len(executables) != 1:
    raise RuntimeError("expected exactly one pinned Chromium installation")
executable = str(executables[0])
command = [
    executable,
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=9223",
    "--remote-allow-origins=*",
    "--user-data-dir=/tmp/chromium",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--autoplay-policy=no-user-gesture-required",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "about:blank",
]


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", 9223), timeout=10) as target:
            target.settimeout(None)
            peers = {self.request: target, target: self.request}
            while True:
                ready, _, _ = select.select(list(peers), [], [], 5)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


process = subprocess.Popen(command)
signal.signal(signal.SIGTERM, lambda *_: process.terminate())
with Server(("0.0.0.0", 9222), Handler) as server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        process.wait()
    finally:
        server.shutdown()
raise SystemExit(process.returncode)
