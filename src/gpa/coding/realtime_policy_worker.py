"""Run a submitted policy in a persistent worker process."""

import ctypes
import base64
import io
import importlib.util
import json
import os
import resource
import sys
import traceback


def restrict():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    class Arg(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_int),
            ("a", ctypes.c_uint64),
            ("b", ctypes.c_uint64),
        ]

    ctx = lib.seccomp_init(0x7FFF0000)
    if not ctx:
        raise RuntimeError("seccomp_init_failed")
    deny = 0x00050000 | 1
    for name in (b"execve", b"execveat", b"ptrace", b"process_vm_readv", b"process_vm_writev"):
        if lib.seccomp_rule_add(ctx, deny, lib.seccomp_syscall_resolve_name(name), 0) != 0:
            raise RuntimeError("seccomp_rule_failed")
    if (
        lib.seccomp_rule_add(
            ctx, deny, lib.seccomp_syscall_resolve_name(b"socket"), 1, Arg(0, 1, 1, 0)
        )
        != 0
    ):
        raise RuntimeError("seccomp_socket_rule_failed")
    if lib.seccomp_load(ctx) != 0:
        raise RuntimeError("seccomp_load_failed")
    lib.seccomp_release(ctx)
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    os.makedirs(os.environ["TMPDIR"], exist_ok=True)
    import cv2
    import numpy as np
    from PIL import Image

    cv2.setNumThreads(1)
    wire = sys.stdout
    sys.stdout = sys.stderr
    if os.environ.get("RGBGAME_TRUSTED_FAKE_FIXTURE") != "1":
        restrict()
    if json.loads(sys.stdin.readline()) != {"hello": True}:
        raise RuntimeError("invalid_policy_handshake")
    wire.write(json.dumps({"ready": True}) + "\n")
    wire.flush()
    module = None
    state = None
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if module is None:
                sys.path.insert(0, os.getcwd())
                spec = importlib.util.spec_from_file_location("policy", "policy.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
            import claude_game_tools

            claude_game_tools._context = req.get("context", {})
            with Image.open(
                io.BytesIO(base64.b64decode(req["jpeg_base64"], validate=True))
            ) as image:
                frame = np.array(image.convert("RGB"))
            action, state = module.step(frame, state)
            encoded = json.dumps({"action": action}, allow_nan=False)
        except Exception:
            encoded = json.dumps({"error": traceback.format_exc(limit=6)[-4000:]})
        wire.write(encoded + "\n")
        wire.flush()


if __name__ == "__main__":
    main()
