"""Explicit valid/missing scores, fixed denominators, and offline CSV exports."""

import csv
import json
from pathlib import Path

from .files import dump


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def csv_file(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def usage(root):
    calls, output, unknown = 0, 0, 0
    initial = root / "shared-initial/requests"
    if not initial.exists():
        initial = root / "private/initial/requests"
    folders = [root / "requests", initial]
    for folder in folders:
        for path in folder.glob("*/meta.json"):
            value = read(path)
            tokens = (value.get("usage") or {}).get("completion_tokens")
            attempted = value.get(
                "http_attempts", int(tokens is not None or path.with_name("response.raw").exists())
            )
            calls += attempted
            output += tokens if type(tokens) is int else 0
            unknown += int(attempted > 0 and tokens is None)
    for path in root.glob("**/private/claude-http/*.meta.json"):
        value = read(path)
        attempted = value.get("upstream_attempts", 0)
        tokens = (value.get("usage") or {}).get("output_tokens")
        calls += attempted
        output += tokens if type(tokens) is int else 0
        unknown += int(attempted > 0 and value.get("path") == "/v1/messages" and tokens is None)
    return {
        "model_requests": calls,
        "known_output_tokens": output,
        "unknown_usage_requests": unknown,
    }


def write_tables(root, rows):
    root = Path(root)
    trials = []
    for row in rows:
        folder = root / row["id"]
        scores = [row.get("score")] if row["method"] == "rtc" else row.get("scores", [])
        scores = [
            score
            for score in scores
            if isinstance(score, dict) and score.get("classification") == "valid_score"
        ]
        valid = row["status"] == "OK" and bool(scores)
        progress = max((score["progress"] for score in scores), default=None)
        clock = read(folder / "run-clock.json")
        result = read(folder / "result.json")
        trials.append(
            {
                "run_id": row["id"],
                "task_id": row["task"],
                "method": row["method"],
                "status": row["status"],
                "model_outcomes": "; ".join(row.get("acceptance", {}).get("model_outcomes", [])),
                "valid": valid,
                "pg": progress if valid else None,
                "strict_success": any(s["success"] for s in scores) if valid else None,
                "observed_partial_pg": progress if not valid else None,
                "submissions": row.get("submissions", 0),
                "wall_seconds": clock.get("elapsed_seconds", result.get("real_seconds")),
                **usage(folder),
            }
        )
    summary = []
    for method in sorted({row["method"] for row in trials}):
        group = [row for row in trials if row["method"] == method]
        valid = [row for row in group if row["valid"]]
        summary.append(
            {
                "method": method,
                "total": len(group),
                "valid": len(valid),
                "invalid_or_missing": len(group) - len(valid),
                "mean_pg_valid": sum(row["pg"] for row in valid) / len(valid) if valid else None,
                "mean_pg_fixed_denominator": sum(row["pg"] for row in valid) / len(group),
                "strict_success_fixed_denominator": sum(row["strict_success"] for row in valid)
                / len(group),
                "known_output_tokens": sum(row["known_output_tokens"] for row in group),
                "model_requests": sum(row["model_requests"] for row in group),
            }
        )
    csv_file(root / "trials.csv", trials)
    csv_file(root / "summary.csv", summary)
    value = {
        "methods": summary,
        "score_policy": "Best valid submitted policy per developer or team; initial exploration excluded. Invalid/missing trials remain null and contribute zero only to explicitly named fixed-denominator aggregates.",
        "multi_agent_unit": "one team of four branches",
        "trials": trials,
    }
    dump(root / "summary.json", value)
    return value
