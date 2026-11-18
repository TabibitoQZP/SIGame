"""One permanent per-branch ledger with bounded planned review phases."""

from gpa.coding.claude_game_budget import BudgetClosed
from gpa.coding.realtime_observer import ObservedUsage


class PhaseUsage(ObservedUsage):
    def __init__(self, *args, **kwargs):
        self.phase = "first"
        self.phase_reason = None
        self.phase_baseline = 0
        self.phase_cap = None
        super().__init__(*args, **kwargs)

    def snapshot(self):
        with self.lock:
            value = super().snapshot()
            phase_used = value["used"] - self.phase_baseline
            phase_remaining = (
                None if self.phase_cap is None else max(0, self.phase_cap - phase_used)
            )
            return {
                **value,
                "stop_reason": self.reason or self.phase_reason,
                "global_stop_reason": self.reason,
                "phase": self.phase,
                "phase_used": phase_used,
                "phase_remaining": phase_remaining,
                "phase_limit": self.phase_cap,
                "phase_stop_reason": self.phase_reason,
            }

    def begin(self, request_id, requested=32768):
        with self.lock:
            if self.reason or self.phase_reason:
                raise BudgetClosed(self.reason or self.phase_reason)
            if self.phase_cap is not None:
                remaining = self.snapshot()["phase_remaining"]
                if remaining < self.minimum_request_tokens:
                    self.pause("review_output_limit")
                    raise BudgetClosed(self.phase_reason)
                requested = min(requested, remaining)
            return super().begin(request_id, requested)

    def observe(self, *args, **kwargs):
        with self.lock:
            super().observe(*args, **kwargs)
            if (
                self.phase_cap is not None
                and self.snapshot()["phase_remaining"] < self.minimum_request_tokens
            ):
                self.pause("review_output_limit")

    def pause(self, reason):
        with self.lock:
            self.phase_reason = self.phase_reason or reason
            self.persist()
            self.lock.notify_all()

    def enter(self, phase, cap=None):
        with self.lock:
            if self.pending:
                raise RuntimeError("phase_change_with_inflight_request")
            if self.reason:
                raise BudgetClosed(self.reason)
            if phase not in {"first", "review", "second"} or (cap is not None and cap < 16):
                raise ValueError("invalid_review_phase")
            self.phase, self.phase_cap = phase, cap
            self.phase_baseline = super().snapshot()["used"]
            self.phase_reason = None
            self.persist()
