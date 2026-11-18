"""Write run artifacts and verify exported files."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def fingerprint(path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns


def copy_verified(source, target, *, attempts=3):
    """Preserve v2's source-stability and destination-readback export contract."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.partial")
        try:
            before = fingerprint(source)
            expected = hashlib.sha256()
            with source.open("rb") as reader, temporary.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    expected.update(chunk)
                    writer.write(chunk)
            if fingerprint(source) != before or digest(source) != expected.hexdigest():
                raise OSError("source changed during export")
            if digest(temporary) != expected.hexdigest():
                raise OSError("export readback mismatch")
            temporary.replace(target)
            if digest(target) != expected.hexdigest():
                raise OSError("published export mismatch")
            return expected.hexdigest()
        except OSError:
            if attempt + 1 == attempts:
                raise
        finally:
            temporary.unlink(missing_ok=True)
    raise ValueError("attempts must be positive")
