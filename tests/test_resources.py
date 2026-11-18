from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import threading
import time
from unittest.mock import patch

import pytest

from gpa.execution.resources import GIB, ResourceSession, ResourceStopped, atomic_json


@pytest.fixture
def session(tmp_path):
    with patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 30 * GIB}):
        value = ResourceSession.create(tmp_path / "session", output_limit=100)
    atomic_json(value.path / "heartbeat.json", {"monotonic": time.monotonic()})
    return value


def test_group_reservation_includes_future_submit_and_borrows_only_free_memory(session):
    with patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 30 * GIB}):
        with session.group(8 * GIB) as group:
            allocation = session.allocate(group, "developer", 2 * GIB, "dev")
            with session.db() as db:
                assert session.used(db) == 8 * GIB
            extra = session.allocate(group, "browser", 7 * GIB, "browser")
            with session.db() as db:
                assert session.used(db) == 9 * GIB
            session.release(extra)
            session.release(allocation)
    assert session.snapshot()["allocations"] == []


def test_global_token_accounting_retains_unknown_usage(session):
    with session.model_request(60) as ticket:
        ticket["output_tokens"] = 20
    with session.model_request(50):
        pass
    with pytest.raises(ResourceStopped, match="budget_exhausted"):
        with session.model_request(31):
            pass
    assert [r["state"] for r in session.snapshot()["requests"]] == ["complete", "unknown_usage"]


def test_missing_or_stale_watchdog_fails_closed(session):
    atomic_json(session.path / "heartbeat.json", {"monotonic": time.monotonic() - 20})
    with pytest.raises(ResourceStopped, match="stale"):
        session.check()
    (session.path / "heartbeat.json").unlink()
    with pytest.raises(ResourceStopped, match="not_ready"):
        session.check()


@pytest.mark.parametrize("first_usage", [1, None])
def test_concurrent_reservation_waits_for_settlement(session, first_usage):
    waiting, settled = threading.Event(), threading.Event()

    def wait_for_settlement(_):
        waiting.set()
        assert settled.wait(2)

    def reserve():
        with session.model_request(60, timeout=3) as ticket:
            ticket["output_tokens"] = 2

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch("gpa.execution.resources.time.sleep", side_effect=wait_for_settlement),
    ):
        with session.model_request(60) as ticket:
            ticket["output_tokens"] = first_usage
            second = executor.submit(reserve)
            assert waiting.wait(2)
            assert len(session.snapshot()["requests"]) == 1
            assert not second.done()
        settled.set()
        if first_usage is None:
            with pytest.raises(ResourceStopped, match="global_model_output_budget_exhausted"):
                second.result(timeout=2)
            assert [r["state"] for r in session.snapshot()["requests"]] == ["unknown_usage"]
        else:
            second.result(timeout=2)
            rows = session.snapshot()["requests"]
            assert [r["output"] for r in rows] == [1, 2]


def test_abort_file_prevents_any_new_work(session):
    atomic_json(session.path / "abort.json", {"reason": "low_memory"})
    with pytest.raises(ResourceStopped, match="aborted"):
        with session.model_request(1):
            pass
    assert json.loads((session.path / "abort.json").read_text())["reason"] == "low_memory"


def test_api_reservation_leaves_a_slot_for_rtc(session):
    with ExitStack() as stack:
        for _ in range(3):
            ticket = stack.enter_context(session.model_request(1))
            ticket["output_tokens"] = 0
        realtime = stack.enter_context(session.model_request(1, priority="realtime", timeout=0))
        realtime["output_tokens"] = 0
        assert sum(r["state"] == "active" for r in session.snapshot()["requests"]) == 4


def test_low_memory_closes_container_admission_without_allocating(session):
    with patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 7 * GIB}):
        with pytest.raises(ResourceStopped, match="admission_timeout"):
            session.allocate("test", "browser", GIB, "blocked", timeout=0)
    assert session.snapshot()["allocations"] == []


def test_recovery_retains_confirmed_and_unknown_usage(session):
    from types import SimpleNamespace

    with session.model_request(20) as ticket:
        ticket["output_tokens"] = 5
    with session.model_request(10):
        pass
    with session.db() as db:
        db.execute("INSERT INTO requests VALUES('inflight',999999,30,NULL,'active',0)")
    atomic_json(session.path / "abort.json", {"reason": "container_oom"})
    with (
        patch("gpa.execution.resources.process_alive", return_value=False),
        patch("gpa.execution.docker.docker", return_value=SimpleNamespace(stdout="")),
        patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 30 * GIB}),
    ):
        recovered = ResourceSession.recover(session.path)
    rows = recovered.snapshot()["requests"]
    assert recovered.id == session.id and recovered.config["output_limit"] == 100
    assert sum(r["output"] if r["output"] is not None else r["reserved"] for r in rows) == 45
    assert [r["state"] for r in rows] == ["complete", "unknown_usage", "unknown_usage"]
    assert list((session.path / "recovery").glob("*/abort.json"))
    with pytest.raises(ResourceStopped, match="not_ready"):
        recovered.check()


def test_browser_override_is_charged_to_the_entire_play_pair(session):
    with patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 30 * GIB}):
        with session.group(6 * GIB) as group:
            with session.play(group, browser_memory=3 * GIB) as slot:
                allocation = next(r for r in session.snapshot()["allocations"] if r["id"] == slot)
                assert allocation["memory"] == 3 * GIB + 768 * 1024**2
                session.validate_play(slot, group, kind="browser", memory=3 * GIB)
                with pytest.raises(ResourceStopped, match="smaller_than_container_limits"):
                    session.validate_play(slot, group, kind="browser", memory=4 * GIB)


def test_probes_are_exclusive_but_multiple_evaluations_can_run_together(session):
    with session.activity("evaluation"), session.activity("evaluation"):
        with pytest.raises(ResourceStopped, match="must_run_separately"):
            with session.activity("probe"):
                pytest.fail("probe must not overlap a live evaluation")
    with session.activity("probe"):
        with pytest.raises(ResourceStopped, match="must_run_separately"):
            with session.activity("evaluation"):
                pytest.fail("evaluation must not overlap a compatibility probe")
    with session.activity("evaluation"):
        pass


def test_four_gib_browser_reservation_and_impossible_shared_service_combo(session):
    with patch("gpa.execution.resources.host_memory", return_value={"MemAvailable": 30 * GIB}):
        with session.group(8 * GIB) as group:
            with session.play(group) as slot:
                row = next(r for r in session.snapshot()["allocations"] if r["id"] == slot)
                assert row["memory"] == 4 * GIB + 768 * 1024**2
        vision = session.allocate("shared-vision", "vision", 6 * GIB, "vision")
        game = session.allocate("shared-game", "game", 512 * 1024**2, "game")
        try:
            with pytest.raises(ResourceStopped, match="plus_shared_services_exceeds"):
                with session.group(10 * GIB + 768 * 1024**2, timeout=0):
                    pytest.fail("an impossible combination must fail before queuing")
        finally:
            session.release(game)
            session.release(vision)
