"""Replay scored episodes with the GameWorld evaluator."""

import json
import math
from collections import Counter
from pathlib import Path

from .upstream_evaluator import _finalize_task_evaluation, reset_task_evaluator_episode_metrics


def replay(sessions, contract, cutoff_ns):
    if type(cutoff_ns) is not int or cutoff_ns <= 0:
        raise ValueError("missing token cutoff")
    metrics, counts = {}, Counter()
    progress, success, peak, first_success = 0.0, False, None, None
    final_metrics, valid, boundaries = {}, 0, []
    for session_id, rows in sessions:
        metrics = reset_task_evaluator_episode_metrics(metrics)
        previous = None
        for row in rows:
            observed = row.get("observed_ns")
            if type(observed) is not int or observed > cutoff_ns or row.get("accepted") is not True:
                counts["excluded"] += 1
                continue
            if row.get("status") != "ok" or not isinstance(row.get("state"), dict):
                counts[str(row.get("status", "invalid"))] += 1
                continue
            state = row["state"]
            if state.get("gameId") != contract["game_id"]:
                counts["game_id_mismatch"] += 1
                continue
            boundary = None
            if previous:
                old = previous["state"]
                old_terminal = old.get("terminal") or {}
                terminal = state.get("terminal") or {}
                if row.get("document_id") != previous.get("document_id"):
                    boundary = "document_changed"
                elif old.get("episodeId") != state.get("episodeId"):
                    boundary = "episode_changed"
                elif old_terminal.get("isTerminal") and not terminal.get("isTerminal"):
                    boundary = "terminal_to_nonterminal"
                elif (
                    type(old.get("gameTimeMs")) in (int, float)
                    and type(state.get("gameTimeMs")) in (int, float)
                    and state["gameTimeMs"] < old["gameTimeMs"]
                ):
                    boundary = "game_time_restarted"
            if boundary:
                metrics = reset_task_evaluator_episode_metrics(metrics)
                boundaries.append(
                    {"session_id": session_id, "observed_ns": observed, "kind": boundary}
                )
            previous = row
            numeric = True
            for path in contract["evaluator_config"]["objective"]["reach"]:
                value = state
                for key in path.split("."):
                    value = value.get(key) if isinstance(value, dict) else None
                if type(value) not in (int, float) or not math.isfinite(value):
                    numeric = False
            if not numeric:
                counts["unresolved_fields"] += 1
                continue
            result = _finalize_task_evaluation(
                state, 0, metrics, contract["evaluator_config"], max_steps=2**63, finalized=True
            )
            if result.metrics.get("evaluation_config_errors"):
                raise ValueError("invalid upstream objective")
            if result.metrics.get("evaluation_runtime_issues"):
                counts["unresolved_fields"] += 1
                continue
            metrics, final_metrics = result.metrics, result.metrics
            valid += 1
            counts["ok"] += 1
            current = metrics.get("progress", 0.0)
            if current > progress:
                progress = current
                peak = {"session_id": session_id, "observed_ns": observed}
            if result.status == "success":
                success = True
                if first_success is None:
                    first_success = {"session_id": session_id, "observed_ns": observed}
    return {
        "game_id": contract["game_id"],
        "task_id": contract["task_id"],
        "progress": progress,
        "success": success,
        "valid_samples": valid,
        "classification": "valid_score" if valid else "no_valid_samples",
        "sample_counts": dict(counts),
        "cutoff_ns": cutoff_ns,
        "peak": peak,
        "first_success": first_success,
        "boundaries": boundaries,
        "last_metrics": final_metrics,
        "agent_score_feedback": False,
        "protocol": contract.get("protocol", "rgbgame-realtime-v1"),
    }


def rows(path):
    try:
        stream = Path(path).open()
    except OSError:
        yield {"status": "missing_samples", "accepted": True, "observed_ns": 1}
        return
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("invalid row")
                yield value
            except ValueError:
                yield {"status": "malformed", "accepted": True, "observed_ns": 1}


def summarize(output, contract, *, cutoff_ns, session_count):
    output = Path(output)
    result = replay(
        (
            (i, rows(output / "sessions" / f"session_{i:03d}" / "host_gameworld/samples.jsonl"))
            for i in range(session_count)
        ),
        contract,
        cutoff_ns,
    )
    result["session_count"] = session_count
    folder = output / "host_gameworld"
    folder.mkdir(exist_ok=True)
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result
