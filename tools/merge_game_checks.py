"""Merge ordered no-model browser checks; the latest attempt for each task wins."""

import argparse
import hashlib
import json
from pathlib import Path

from gpa.evidence.files import dump
from gpa.gameworld import locked, tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    latest, probes, sources = {}, {}, []
    for folder in args.inputs:
        result = json.loads((folder / "result.json").read_text())
        if result.get("model_calls") != 0 or result.get("game_lock") != locked():
            raise ValueError("checks must use the current game lock and no model calls")
        for filename, destination in (("checks.json", latest), ("input-checks.json", probes)):
            path = folder / filename
            raw = path.read_bytes()
            records = json.loads(raw)
            if len({row["task"] for row in records}) != len(records):
                raise ValueError("duplicate task within a check file")
            sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
            for row in records:
                if row["task"] not in tasks():
                    raise ValueError("unknown task in check file")
                destination[row["task"]] = {**row, "source": str(path.resolve())}
    missing = sorted(set(tasks()) - latest.keys())
    failures = sorted(task for task, row in latest.items() if row["status"] != "OK")
    missing_probes = sorted({"06_05", "18_05", "19_05"} - probes.keys())
    probe_failures = sorted(task for task, row in probes.items() if row["status"] != "OK")
    args.output.mkdir(parents=True, exist_ok=False)
    dump(args.output / "checks.json", [latest[key] for key in sorted(latest)])
    dump(args.output / "input-checks.json", [probes[key] for key in sorted(probes)])
    result = {
        "status": "OK" if not (missing or failures or missing_probes or probe_failures) else "ERROR",
        "checked": len(latest),
        "failed": failures,
        "missing": missing,
        "missing_input_probes": missing_probes,
        "failed_input_probes": probe_failures,
        "game_lock": locked(),
        "model_calls": 0,
        "merge_policy": "Inputs ordered oldest to newest; latest task record wins, including failures.",
        "sources": sources,
    }
    dump(args.output / "result.json", result)
    print(json.dumps({key: result[key] for key in ("status", "checked", "failed", "missing")}))
    if result["status"] != "OK":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
