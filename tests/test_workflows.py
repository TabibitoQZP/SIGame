import hashlib
import json
from pathlib import Path

import pytest

from gpa.cli import run
from gpa.config import load_config

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.docker


def rows(path):
    return [json.loads(s) for s in path.read_text().splitlines()]


@pytest.mark.parametrize("method", ["rtc", "single_agent", "multi_agent"])
def test_full_workflow_uses_real_transport_and_runtime(method, short_dir):
    config = load_config(ROOT / f"configs/fake-{method}.json")
    root = short_dir / method
    result = run(config, root)
    assert result["status"] == "OK", result
    assert result["fake"] is True
    if method == "rtc":
        assert result["model_requests"] > 0
        plays = [root / "play"]
        for path in root.glob("requests/*/gate.json"):
            gate = json.loads(path.read_text())
            if not gate["cancelled"]:
                assert gate["effective_latency_ns"] >= 200_000_000
        response = json.loads(next(root.glob("requests/*/response.json")).read_text())
        assert response["choices"][0]["message"]["reasoning_content"]
    else:
        branches = [root] if method == "single_agent" else [root / "branches" / b for b in "ABCD"]
        plays = []
        for branch in branches:
            result_files = list(branch.glob("private/round-*/result.json"))
            assert len(result_files) == 2
            hashes = []
            for path in result_files:
                result_row = json.loads(path.read_text())
                assert result_row["error"] is None, result_row
                hashes.append(result_row["policy_hashes"]["policy.py"])
                plays.append(path.parent / "play")
                feedback = json.loads((path.parent / "public_feedback.json").read_text())
                assert "host_score" not in feedback and feedback["success"] is None
            assert len(set(hashes)) == 2, (
                "fake developer must actually modify and submit a different policy"
            )
            budget = json.loads((branch / "private/accounting/budget.json").read_text())
            assert budget["active_requests"] == 0 and budget["used"] > 0
            assert budget["used"] == sum(r["charged_output_tokens"] for r in budget["requests"])
            assert not budget["accounting_error"]
            if method == "multi_agent":
                invocations = [
                    json.loads(p.read_text())
                    for p in sorted(branch.glob("private/development-*.json"))
                ]
                assert [i["phase"] for i in invocations] == ["first", "review", "second"]
                assert len({i["session"] for i in invocations}) == 1
                assert [i["resume"] for i in invocations] == [False, True, True]
                assert "fake" in (branch / "workspace/peer_review_received.md").read_text()
                assert not list((branch / "workspace/peers").rglob("private"))
                assert not list((branch / "workspace/peers").rglob("home"))
                assert not list((branch / "workspace/peers").rglob("requests"))
                for p in (branch / "workspace/peers").glob("*/manifest.json"):
                    manifest = json.loads(p.read_text())
                    assert not manifest["official_score_available"]
                    for name, digest in manifest["files"].items():
                        assert hashlib.sha256((p.parent / name).read_bytes()).hexdigest() == digest
        if method == "multi_agent":
            assert result["team_complete"] and not result["failed_branches"]
            assert result["output_budget"]["used"] == 4 * 3 * 48
    for play in plays:
        events = rows(play / "events.jsonl")
        active = [e for e in events if e["event"] == "input_sent" and e["action"]["keys"]]
        assert active, "Returning OK without any actual policy action is not sufficient"
        deadline = next(e["deadline_ns"] for e in events if e["event"] == "run_started")
        assert all(e["observed_ns"] < deadline for e in active)
        assert any(e["event"] == "input_released" and e["reason"] == "run_end" for e in events)
        for frame in rows(play / "frames.jsonl"):
            assert (
                hashlib.sha256((play / frame["path"]).read_bytes()).hexdigest() == frame["sha256"]
            )
        assert (play / "input_authority.signal").read_text() == "0"
    calls = json.loads((root / "fake_http_calls.json").read_text())
    assert any(c["path"] == "/v1/chat/completions" for c in calls)
    if method != "rtc":
        conversations = [c["body"]["messages"] for c in calls if c["path"] == "/v1/messages"]
        assert any(any(m["role"] == "assistant" for m in conv) for conv in conversations)


def test_refuse_overwriting_evidence(short_dir):
    config = load_config(ROOT / "configs/fake-rtc.json")
    with pytest.raises(FileExistsError):
        run(config, short_dir)
