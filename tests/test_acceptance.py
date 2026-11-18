import csv
import json

from gpa.acceptance import inspect_run
from gpa.commands import report


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def branch(folder, rounds=0):
    save(folder / "private/cli-version.json", {"real_claude": True})
    for index in range(rounds):
        root = folder / "private" / f"round-{index + 1:02}"
        save(root / "result.json", {
            "error": None, "origin_ns": 1, "deadline_ns": 60_000_000_001,
            "ended_ns": 60_000_000_002, "termination": "time_budget",
            "host_score": {"classification": "valid_score", "progress": 0, "success": False},
            "policy_hashes": {"policy.py": "0" * 64},
        })
        play = root / "play"
        play.mkdir()
        (play / "events.jsonl").write_text(json.dumps({"event": "input_sent", "action": {"keys": [], "kind": "wait"}}) + "\n")
        (play / "input_authority.signal").write_text("0")


def test_no_submission_is_an_observed_model_outcome(tmp_path):
    save(tmp_path / "result.json", {"status": "OK", "termination": "output_token_budget"})
    branch(tmp_path)
    result = inspect_run(tmp_path, {"method": "single_agent", "fake": False, "development_rounds": 2})
    assert result["status"] == "OK", result
    assert any("submitted 0/2" in value for value in result["model_outcomes"])
    save(tmp_path / "result.json", {"status": "ERROR", "error": "controller failure"})
    assert inspect_run(tmp_path, {"method": "single_agent", "fake": False, "development_rounds": 2})["status"] == "ERROR"


def test_wait_and_duplicate_policies_are_allowed_but_host_failures_are_not(tmp_path):
    config = {"method": "single_agent", "fake": False, "development_rounds": 2, "game_seconds": 60}
    save(tmp_path / "result.json", {"status": "OK"})
    branch(tmp_path, rounds=2)
    result = inspect_run(tmp_path, config)
    assert result["status"] == "OK", result
    assert any("unchanged" in value for value in result["model_outcomes"])
    assert any("non-wait" in value for value in result["model_outcomes"])
    path = tmp_path / "private/round-01/result.json"
    value = json.loads(path.read_text())
    value.update(error={"origin": "host", "type": "browser_disconnected"}, error_origin="host")
    save(path, value)
    assert inspect_run(tmp_path, config)["status"] == "ERROR"


def test_generated_policy_failure_does_not_excuse_unreleased_input(tmp_path):
    config = {"method": "single_agent", "fake": False, "development_rounds": 2, "game_seconds": 60}
    save(tmp_path / "result.json", {"status": "OK"})
    branch(tmp_path, rounds=1)
    path = tmp_path / "private/round-01/result.json"
    value = json.loads(path.read_text())
    value.update(error={"origin": "policy", "type": "SyntaxError"}, error_origin="policy", ended_ns=2, termination="policy_error", host_score={"classification": "no_valid_samples"})
    save(path, value)
    result = inspect_run(tmp_path, config)
    assert result["status"] == "OK", result
    assert any("SyntaxError" in value for value in result["model_outcomes"])
    (tmp_path / "private/round-01/play/input_authority.signal").write_text("1")
    assert inspect_run(tmp_path, config)["status"] == "ERROR"


def test_multi_agent_missing_model_review_is_separate_from_broken_session_resume(tmp_path):
    config = {"method": "multi_agent", "fake": False, "branch_submissions": 2, "game_seconds": 60}
    save(tmp_path / "result.json", {"status": "OK"})
    for name in "ABCD":
        root = tmp_path / "branches" / name
        branch(root, rounds=2)
        save(root / "private/accounting/budget.json", {"stop_reason": "submission_limit"})
        save(root / "private/review-delivery.json", {"status": "review_missing"})
        for index, phase in enumerate(("first", "review", "second")):
            save(root / "private" / f"development-{index + 1:02}.json", {"phase": phase, "session": name, "resume": bool(index)})
    result = inspect_run(tmp_path, config)
    assert result["status"] == "OK", result
    assert any("deliverable review" in value for value in result["model_outcomes"])
    save(tmp_path / "branches/B/private/development-03.json", {"phase": "second", "session": "new-session", "resume": True})
    assert inspect_run(tmp_path, config)["status"] == "ERROR"


def test_multi_agent_early_completion_keeps_valid_score_in_summary(tmp_path):
    config = {
        "method": "multi_agent", "fake": False, "branch_submissions": 3, "midpoint_submit": 2,
        "game_seconds": 60, "engineering_validation": True,
    }
    root = tmp_path / "00-multi_agent-18_05"
    save(tmp_path / "suite.json", {"rows": [
        {"id": root.name, "task": "18_05", "method": "multi_agent", "config": config},
    ]})
    save(root / "result.json", {"status": "OK", "fake": False})
    for name, phases, stop, eligible, submits in (
        ("A", ["first"], "developer_finished", False, 1),
        ("B", ["first"], "output_token_budget", False, 0),
        ("C", ["first", "review"], "output_token_budget", True, 2),
        ("D", ["first", "review", "second"], "submission_limit", True, 3),
    ):
        folder = root / "branches" / name
        branch(folder, rounds=submits)
        save(root / "private/team-state" / f"{name}.json", {
            "eligible": eligible, "terminal": True, "status": "OK", "termination": stop,
        })
        save(folder / "private/accounting/budget.json", {"stop_reason": stop})
        for index, phase in enumerate(phases):
            save(folder / "private" / f"development-{index + 1:02}.json", {
                "phase": phase, "session": name, "resume": bool(index),
            })
        if "second" in phases:
            save(folder / "private/review-delivery.json", {"status": "delivered"})
    score = root / "branches/A/private/round-01/result.json"
    value = json.loads(score.read_text())
    value["host_score"].update(progress=1, success=True)
    save(score, value)

    result = report(tmp_path)
    assert result["status"] == "OK", result
    outcomes = result["runs"][0]["acceptance"]["model_outcomes"]
    assert sum("ended before team midpoint" in line for line in outcomes) == 2
    assert any("budget exhausted during peer review" in line for line in outcomes)
    with (tmp_path / "trials.csv").open() as trials:
        trial = next(csv.DictReader(trials))
    assert trial["valid"] == "True" and float(trial["pg"]) == 1

    (root / "branches/D/private/development-03.json").unlink()
    assert report(tmp_path)["status"] == "ERROR"
