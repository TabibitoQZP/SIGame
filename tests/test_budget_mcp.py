import io
import json
import urllib.error
import urllib.request

import pytest

from gpa.coding.claude_game_budget import BudgetClosed, ResponseUsage
from gpa.coding.realtime_mcp import serve
from gpa.coding.realtime_observer import ObservedUsage, start_proxy
from gpa.coding.realtime_team_budget import PhaseUsage
from gpa.testing.models import FakeModels


def test_phase_caps_and_unknown_usage(short_dir):
    budget = PhaseUsage(100, short_dir, enforce=True)
    assert budget.begin("1", 200) == 100
    budget.observe("1", {"output_tokens": 30}, True, 0)
    budget.finish("1")
    budget.pause("planned_midpoint")
    with pytest.raises(BudgetClosed):
        budget.begin("2")
    budget.enter("review", 20)
    assert budget.begin("2", 200) == 20
    budget.observe("2", {}, False, 1)
    budget.finish("2")
    assert budget.snapshot()["used"] == 30
    assert budget.snapshot()["unknown_or_incomplete_requests"] == 1
    assert budget.begin("3", 200) == 20
    budget.observe("3", {"output_tokens": 20}, True, 1)
    budget.finish("3")
    assert budget.snapshot()["stop_reason"] == "review_output_limit"
    budget.enter("second")
    assert budget.begin("4", 200) == 50
    budget.observe("4", {"output_tokens": 51}, True, 1)
    budget.finish("4")
    assert budget.snapshot()["accounting_error"]


def test_review_tool_permissions(short_dir):
    requests = [
        {"id": 1, "method": "tools/list"},
        {"id": 2, "method": "tools/call", "params": {"name": "submit", "arguments": {}}},
        {
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "write_review",
                "arguments": {"content": "Evidence-based suggestion"},
            },
        },
    ]
    source, dest = io.StringIO("\n".join(json.dumps(r) for r in requests)), io.StringIO()
    serve(
        source,
        dest,
        rpc=lambda *args: pytest.fail("submit must not reach host"),
        review_mode=True,
        review_path=short_dir / "review.md",
    )
    replies = [json.loads(s)["result"] for s in dest.getvalue().splitlines()]
    assert {t["name"] for t in replies[0]["tools"]} == {"status", "write_review"}
    assert replies[1]["isError"]
    assert (short_dir / "review.md").read_text() == "Evidence-based suggestion"


def test_messages_proxy_preserves_thinking_and_records_budget(short_dir):
    budget = ObservedUsage(60, short_dir / "budget", enforce=True)
    with FakeModels() as models:
        proxy = start_proxy(
            short_dir / "http",
            models.url,
            "fake-secret",
            "local-secret",
            budget=budget,
            status=lambda: {"submitted_rounds": 0},
            request_seconds=5,
        )
        try:
            body = {
                "model": "fake",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "retain this", "signature": "fixture"}
                        ],
                    }
                ],
                "max_tokens": 500,
                "stream": False,
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/v1/messages",
                data=json.dumps(body).encode(),
                headers={"X-Api-Key": "local-secret"},
            )
            with urllib.request.urlopen(request) as response:
                assert json.load(response)["type"] == "message"
            proxy.wait_idle(5)
            assert models.calls[0]["body"]["messages"] == body["messages"]
            assert models.calls[0]["body"]["max_tokens"] == 60
            assert budget.snapshot()["used"] == 48
            assert "fake-secret" not in "".join(
                p.read_text() for p in (short_dir / "http").iterdir()
            )
        finally:
            proxy.close_admissions()
            proxy.shutdown()
            proxy.server_close()


def test_sse_usage_counts_cumulative_tokens_once():
    usage = ResponseUsage(True)
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "message_delta", "usage": {"output_tokens": 12}},
        {"type": "message_delta", "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]
    raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
    for i in range(0, len(raw), 7):
        usage.feed(raw[i : i + 7])
    usage.finish()
    assert usage.complete and usage.usage["output_tokens"] == 20
