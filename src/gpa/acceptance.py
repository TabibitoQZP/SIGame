"""Validate run outputs and summarize policy outcomes."""

import json
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def inspect_run(root, config):
    root = Path(root)
    issues = []
    outcomes = []

    def require(condition, message):
        if not condition:
            issues.append(message)

    def play(folder, result):
        policy_error = result.get("error_origin") == "policy" or (
            isinstance(result.get("error"), dict) and result["error"].get("origin") == "policy"
        )
        if policy_error:
            outcomes.append(str(folder) + ": submitted policy failed: " + str(result.get("error")))
        else:
            require(not result.get("error"), str(folder) + ": runtime error")
        if not config["fake"] and not policy_error:
            require(
                result.get("host_score", {}).get("classification") == "valid_score",
                str(folder) + ": no valid private score",
            )
        events = folder / "events.jsonl"
        rows = (
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        active = [
            r
            for r in rows
            if r.get("event") == "input_sent"
            and (r.get("action", {}).get("keys") or r.get("action", {}).get("click") is not None)
        ]
        if not active:
            outcomes.append(str(folder) + ": no non-wait keyboard or mouse action")
        require(
            (folder / "input_authority.signal").exists()
            and (folder / "input_authority.signal").read_text() == "0",
            str(folder) + ": input authority not revoked",
        )
        origin, deadline, ended = (
            result.get(key) for key in ("origin_ns", "deadline_ns", "ended_ns")
        )
        complete = (
            all(type(value) is int for value in (origin, deadline, ended))
            and deadline - origin == config["game_seconds"] * 10**9
            and ended >= deadline
            and result.get("termination") == "time_budget"
        )
        if not policy_error:
            require(complete, str(folder) + ": incomplete wall-clock budget")

    try:
        result = read(root / "result.json")
        require(result.get("status") == "OK", "method status is not OK")
        if config["method"] == "rtc":
            play(root / "play", result)
        else:
            branches = (
                [root]
                if config["method"] == "single_agent"
                else [root / "branches" / name for name in "ABCD"]
            )
            expected = (
                config["development_rounds"]
                if config["method"] == "single_agent"
                else config["branch_submissions"]
            )
            for branch in branches:
                rounds = sorted((branch / "private").glob("round-*/result.json"))
                require(len(rounds) <= expected, str(branch) + ": submission allowance exceeded")
                if len(rounds) < expected:
                    outcomes.append(str(branch) + f": submitted {len(rounds)}/{expected} allowed policies")
                hashes = []
                for path in rounds:
                    run = read(path)
                    hashes.append(run.get("policy_hashes", {}).get("policy.py"))
                    play(path.parent / "play", run)
                require(None not in hashes, str(branch) + ": submitted policy hash missing")
                if len(set(hashes)) < len(hashes):
                    outcomes.append(str(branch) + ": repeated an unchanged submitted policy")
                version = read(branch / "private/cli-version.json")
                require(
                    config["fake"] or version.get("real_claude") is True,
                    str(branch) + ": real Claude Code not verified",
                )
                if config["method"] == "multi_agent":
                    budget = read(branch / "private/accounting/budget.json")
                    state_path = root / "private/team-state" / (branch.name + ".json")
                    state = read(state_path) if state_path.exists() else {}
                    termination = (
                        state.get("termination")
                        or result.get("branches", {}).get(branch.name, {}).get("termination")
                        or budget.get("stop_reason")
                    )
                    exhausted = "output_token_budget" in {
                        termination, budget.get("stop_reason"), budget.get("global_stop_reason")
                    }
                    phases = [
                        read(path)
                        for path in sorted((branch / "private").glob("development-*.json"))
                    ]
                    expected_phases = ["first", "review", "second"]
                    actual_phases = [p["phase"] for p in phases]
                    if actual_phases == ["first"] and state.get("eligible") is not True and (
                        exhausted or (
                            termination == "developer_finished"
                            and len(rounds) < config.get("midpoint_submit", 4)
                        )
                    ):
                        reason = "output_token_budget" if exhausted else termination
                        outcomes.append(str(branch) + ": ended before team midpoint: " + reason)
                    elif actual_phases == ["first", "review"] and exhausted:
                        outcomes.append(str(branch) + ": budget exhausted during peer review")
                    else:
                        require(actual_phases == expected_phases, str(branch) + ": missing team phase")
                    require(
                        all(p["session"] for p in phases)
                        and len({p["session"] for p in phases}) == 1
                        and [p["resume"] for p in phases] == [False, True, True][:len(phases)],
                        str(branch) + ": native session not resumed",
                    )
                    if "second" in actual_phases:
                        delivery = read(branch / "private/review-delivery.json")
                        if delivery.get("status") == "review_missing":
                            outcomes.append(str(branch) + ": peer model did not produce a deliverable review")
                        else:
                            require(delivery.get("status") == "delivered", str(branch) + ": review delivery failed")
    except Exception as error:
        issues.append(type(error).__name__ + ": " + str(error))
    value = {
        "schema": "harness-acceptance-v2",
        "status": "OK" if not issues else "ERROR",
        "issues": issues,
        "model_outcomes": outcomes,
        "fake": config["fake"],
        "status_meaning": "Harness execution checks; not game success or model quality. Missing submissions, wait-only policies and unproduced peer reviews remain model outcomes.",
    }
    (root / "acceptance.json").write_text(json.dumps(value, indent=2) + "\n")
    return value
