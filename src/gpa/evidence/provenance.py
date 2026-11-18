"""Record the controller version and source digests."""

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def controller_provenance():
    root = Path(__file__).resolve().parents[1]
    files = []
    tree = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json"}:
            name = str(path.relative_to(root))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            tree.update(name.encode() + b"\0" + digest.encode() + b"\n")
            files.append({"path": name, "sha256": digest})
    try:
        package_version = version("sigame")
    except PackageNotFoundError:
        package_version = None
    return {"package_version": package_version, "source_sha256": tree.hexdigest(), "files": files}
