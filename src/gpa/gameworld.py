"""Pinned public GameWorld assets and packaged task resolution."""

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import urllib.request

from gpa.evidence.files import dump
from gpa.config import DEFAULT_BROWSER_MEMORY_MIB


def catalog_file(name):
    return files("gpa.catalog").joinpath(name).read_bytes()


def locked():
    return json.loads(catalog_file("gameworld-lock.json"))


def tasks():
    raw = catalog_file("tasks.json")
    if hashlib.sha256(raw).hexdigest() != locked()["task_catalog_sha256"]:
        raise ValueError("task_catalog_checksum_mismatch")
    contracts_raw = catalog_file("task-contracts.json")
    if hashlib.sha256(contracts_raw).hexdigest() != locked()["task_contracts_sha256"]:
        raise ValueError("task_contracts_checksum_mismatch")
    contracts = json.loads(contracts_raw)
    return {
        row["task_id"]: {**row, "evaluator_config": contracts[row["task_id"]]}
        for row in json.loads(raw)
    }


def manifest():
    raw = catalog_file("gameworld-manifest.json")
    if hashlib.sha256(raw).hexdigest() != locked()["manifest_sha256"]:
        raise ValueError("asset_manifest_checksum_mismatch")
    return json.loads(raw)


def default_assets():
    return (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "rgbgame"
        / "gameworld"
        / locked()["commit"]
    )


def verify_assets(root):
    root = Path(root).resolve()
    rows = manifest()["files"]
    expected = {row["path"] for row in rows}
    actual = {str(p.relative_to(root)) for p in (root / "benchmark").rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("game_asset_file_set_mismatch")
    for row in rows:
        path = root / row["path"]
        if path.is_symlink() or path.stat().st_size != row["bytes"]:
            raise ValueError("game_asset_size_or_type_mismatch:" + row["path"])
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != row["sha256"]:
            raise ValueError("game_asset_checksum_mismatch:" + row["path"])
    return {"status": "PASS", "root": str(root), "commit": locked()["commit"], "files": len(rows)}


def prepare(*, output=None, source=None):
    target = Path(output).resolve() if output else default_assets()
    if target.exists():
        return verify_assets(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".gameworld-", dir=target.parent))
    try:
        if source:
            verify_assets(source)
            shutil.copytree(Path(source) / "benchmark", temporary / "benchmark")
            readme = Path(source) / "README.md"
            if readme.exists():
                shutil.copyfile(readme, temporary / "UPSTREAM_README.md")
        else:
            archive = target.parent / (locked()["commit"] + ".tar.gz.partial")
            url = (
                "https://codeload.github.com/gameworld-project/GameWorld-Games/tar.gz/"
                + locked()["commit"]
            )
            for attempt in range(3):
                try:
                    with (
                        urllib.request.urlopen(url, timeout=45) as response,
                        archive.open("wb") as stream,
                    ):
                        while chunk := response.read(1024 * 1024):
                            stream.write(chunk)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
            expected = {row["path"] for row in manifest()["files"]}
            total = 0
            with tarfile.open(archive, "r:gz") as stream:
                for member in stream:
                    parts = Path(member.name).parts
                    relative = Path(*parts[1:])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("unsafe_game_archive_path")
                    name = str(relative)
                    if name not in expected:
                        continue
                    if not member.isfile():
                        raise ValueError("non_regular_game_archive_member")
                    total += member.size
                    if total > 2 * 1024**3:
                        raise ValueError("game_archive_exceeds_size_budget")
                    path = temporary / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as reader, path.open("wb") as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
            archive.unlink(missing_ok=True)
        verify_assets(temporary)
        dump(temporary / "provenance.json", locked())
        temporary.replace(target)
        return verify_assets(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def task_config(config, task_id):
    row = tasks().get(task_id)
    if row is None:
        raise ValueError("unknown_gameworld_task:" + task_id)
    game_id = row["game_id"]
    game = {
        "game_id": game_id,
        "task_id": task_id,
        "evaluator_config": row["evaluator_config"],
        "protocol": "rgbgame-realtime-v1",
    }
    result = {
        **config,
        "backend": "gameworld",
        "task_id": task_id,
        "task": row["task"],
        "gameworld": game,
        "url": "http://game:8000/benchmark/" + game_id + "/index.html" + row["game_url_suffix"],
        "task_source_sha256": row["source_task_sha256"],
        "game_commit": locked()["commit"],
    }
    if game_id in {"06_captaincallisto", "18_minecraft-clone-glm", "19_minesweeper"}:
        result["extended_inputs"] = True
    profiles = {
        "18_minecraft-clone-glm": "minecraft_mouse_hold_v1",
        "19_minesweeper": "ctrl_click_right_v1",
    }
    if game_id in profiles:
        result["input_profile"] = profiles[game_id]
    result.setdefault("browser_memory_mib", DEFAULT_BROWSER_MEMORY_MIB)
    return result
