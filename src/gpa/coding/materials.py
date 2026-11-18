"""Prepare submitted policies and host game assets."""

import functools
import http.server
import shutil
import threading
import uuid
from gpa.evidence.files import digest


def own_tree(path, uid, writable=True):
    for p in [path, *path.rglob("*")]:
        if p.is_symlink():
            raise ValueError("symlink_in_public_workspace")
        p.chmod(0o755 if p.is_dir() else (0o644 if writable else 0o444))


def snapshot(source, target):
    total = 0
    target.mkdir()
    for p in sorted(source.rglob("*")):
        if p.is_symlink():
            raise ValueError("policy_symlinks_forbidden")
        if "__pycache__" in p.parts:
            continue
        dest = target / p.relative_to(source)
        if p.is_dir():
            dest.mkdir(exist_ok=True)
        elif p.is_file():
            total += p.stat().st_size
            if total > 64 * 1024 * 1024:
                raise ValueError("policy_bundle_exceeds_64MiB")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
        else:
            raise ValueError("policy_special_file_forbidden")
    if not (target / "policy.py").is_file():
        raise ValueError("missing_policy_py")
    own_tree(target, 0, writable=False)
    return {str(p.relative_to(target)): digest(p) for p in target.rglob("*") if p.is_file()}


class ProtectedGame:
    """Random URL prefix prevents developer tools discovering the local game source."""

    def __init__(self, directory):
        prefix = "/" + uuid.uuid4().hex + "/"

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                self.path = "/" + self.path[len(prefix) :]
                super().do_GET()

            def do_HEAD(self):
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                self.path = "/" + self.path[len(prefix) :]
                super().do_HEAD()

        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(Handler, directory=str(directory))
        )
        self.url = f"http://127.0.0.1:{self.server.server_port}" + prefix

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
