import threading
import time

import pytest

from gpa.realtime.runtime import Runtime, eligible_tick, select_frame, Frame
from gpa.realtime.direct_model import response_gate
from gpa.testing.game import FakeGame


def test_late_decision_does_not_pause_ticks_and_reset_invalidates_queue(short_dir):
    class Policy:
        def __init__(self):
            self.calls = 0

        def decide(self, frame, context, runtime):
            self.calls += 1
            if self.calls == 1:
                time.sleep(0.45)
                return ["(reset())", "(press(left))"]
            return ["(press(right))"]

        def close(self):
            pass

    game, policy = FakeGame(), Policy()
    runtime = Runtime(game, policy, short_dir / "play", ticks=9)
    result = runtime.run()
    assert not result["error"]
    sent = [e for e in runtime.events if e["event"] == "input_sent"]
    assert [e["action"]["kind"] for e in sent[:3]] == ["wait"] * 3
    assert all("left" not in e["action"]["keys"] for e in sent)
    assert any("right" in e["action"]["keys"] for e in sent)
    assert any(
        e["event"] == "reset_boundary" and e["invalidated_sequences"] for e in runtime.events
    )
    assert all(e["observed_ns"] < result["deadline_ns"] for e in sent)


def test_tick_boundary_is_strict_and_frames_cannot_come_from_future():
    assert eligible_tick(400, 0, 200) == 3
    frame = Frame(b"", 100, 0, 1, 110)
    assert select_frame([frame], 105, 0) is None
    assert select_frame([frame], 110, 1) is None
    assert select_frame([frame], 110, 0) is frame


def test_response_gate_never_grants_late_input():
    now = time.monotonic_ns()
    stop = threading.Event()
    stop.set()
    gate = response_gate(now, now, 3000, now + 10_000_000_000, stop)
    assert gate["cancelled"]


@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError, ValueError])
def test_failed_or_invalid_decisions_revoke_input_and_close_capture(short_dir, failure):
    class Policy:
        closed = False

        def decide(self, frame, context, runtime):
            if failure is ValueError:
                return [object()]
            raise failure("injected transport failure")

        def close(self):
            self.closed = True

    game, policy = FakeGame(), Policy()
    runtime = Runtime(game, policy, short_dir / "failed", ticks=20)
    result = runtime.run()
    assert result["error"] and result["error"]["origin"] == "policy"
    assert result["termination"] == "policy_error"
    assert policy.closed and not game.thread.is_alive()
    assert (short_dir / "failed/input_authority.signal").read_text() == "0"
    assert any(e["event"] == "input_released" for e in runtime.events)
    assert not any(e["event"] == "input_sent" and e["action"]["keys"] for e in runtime.events)


def test_cancel_revokes_input_even_if_policy_returns_after_stop(short_dir):
    entered = threading.Event()

    class Policy:
        def decide(self, frame, context, runtime):
            entered.set()
            runtime.stop.wait(2)
            return ["(press(right))"] * 10

        def close(self):
            pass

    game = FakeGame()
    runtime = Runtime(game, Policy(), short_dir / "cancelled", ticks=20)
    result = []
    thread = threading.Thread(target=lambda: result.append(runtime.run()))
    thread.start()
    assert entered.wait(3)
    with runtime.condition:
        runtime.stop.set()
        runtime.condition.notify_all()
    thread.join(5)
    assert not thread.is_alive() and not game.thread.is_alive()
    assert result[0]["termination"] == "stopped"
    assert (short_dir / "cancelled/input_authority.signal").read_text() == "0"
    assert not any(e["event"] == "input_sent" and e["action"]["keys"] for e in runtime.events)
