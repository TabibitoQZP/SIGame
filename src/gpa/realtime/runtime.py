"""Schedule observations and FIFO actions on a fixed game clock."""

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
from typing import Protocol

from gpa.rtc.codec import Action, parse_line
from .recording import Journal, FrameStore

PERIOD_NS = 200_000_000


@dataclass(frozen=True)
class Frame:
    data: bytes
    received_ns: int
    epoch: int
    sequence: int
    captured_ns: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def identity(self):
        return f"e{self.epoch}-f{self.sequence}"

    def public(self):
        return {
            "frame_id": self.identity,
            "epoch": self.epoch,
            "received_ns": self.received_ns,
            "captured_ns": self.captured_ns,
            "timestamp_quality": "mapped_capture"
            if self.captured_ns is not None
            else "receive_only",
        }


def parse_action(value):
    if isinstance(value, str):
        return parse_line(value)
    if not isinstance(value, dict) or set(value) - {"kind", "keys", "click"}:
        raise ValueError("action_requires_tick_string_or_kind_keys_click_object")
    return Action(
        value.get("kind", "wait"),
        tuple(value.get("keys", ())),
        tuple(value["click"]) if value.get("click") is not None else None,
    )


class Adapter(Protocol):
    capabilities: dict

    def start(self, receive_frame, emit): ...
    def dispatch(self, action: Action, receipt: dict) -> bool: ...
    def release(self, reason: str): ...
    def close(self): ...


def eligible_tick(received_ns, origin_ns, period_ns=PERIOD_NS):
    return max(1, (received_ns - origin_ns) // period_ns + 1)


def select_frame(frames, cutoff_ns, epoch):
    return next(
        (
            f
            for f in reversed(frames)
            if f.epoch == epoch
            and f.received_ns <= cutoff_ns
            and (f.captured_ns is None or f.captured_ns <= cutoff_ns)
        ),
        None,
    )


class Runtime:
    """One clock, one latest-observation mailbox, and at most one policy call in flight."""

    def __init__(self, adapter, policy, output, *, ticks=300, period_ns=PERIOD_NS):
        self.adapter, self.policy, self.output = adapter, policy, Path(output)
        self.ticks, self.period_ns = ticks, period_ns
        self.condition = threading.Condition(threading.RLock())
        self.frames = deque(maxlen=64)
        self.actions = deque()
        self.inflight = {}
        self.reset_pending = None
        self.events, self.observations, self.used = [], [], []
        self.images = FrameStore(self.output)
        self.epoch, self.sequence, self.received_frames = 0, 0, 0
        self.origin_ns = self.deadline_ns = self.ended_ns = None
        self.latest = None
        self.active = False
        self.error = None
        self.stop = threading.Event()
        self.policy_thread = None
        self._last_frame = None
        self.started_ns = None
        self.journal = None

    def emit(self, kind, **details):
        with self.condition:
            record = {"event": kind, "observed_ns": time.monotonic_ns(), **details}
            self.events.append(record)
            if self.journal and not self.journal.closed:
                self.journal.put("events", record)
            if kind == "input_not_sent":
                entry = self.inflight.pop(details.get("action_sequence"), None)
                if details.get("action_sequence") == self.reset_pending:
                    self.reset_pending = None
                if entry and self.active and details.get("reason") == "missed_window":
                    if entry["epoch"] == self.epoch:
                        entry["eligible_tick"] = eligible_tick(
                            time.monotonic_ns(), self.origin_ns, self.period_ns
                        )
                        self.actions.appendleft(entry)
            elif kind == "input_sent":
                entry = self.inflight.pop(details.get("action_sequence"), None)
                if entry and entry["action"].kind == "reset" and self.active:
                    self.epoch = details["epoch"]
                    self.reset_pending = None
                    self.emit(
                        "reset_boundary",
                        **details,
                        invalidated_sequences=[a["sequence"] for a in self.actions],
                    )
                    self.actions.clear()
                    self.frames.clear()
                    self.latest = None
            elif kind in {"adapter_error", "input_error"}:
                self.error = {"origin": "host", "type": details.get("error_type", kind)}
                self.stop.set()

    def receive(self, frame):
        with self.condition:
            if self.stop.is_set() or frame.epoch != self.epoch:
                return
            self.frames.append(frame)
            self.received_frames += 1
            if self.origin_ns is None:
                self.origin_ns = frame.received_ns
                self.deadline_ns = self.origin_ns + self.ticks * self.period_ns
                self.active = True
            self.condition.notify_all()

    def retain(self, frame):
        if frame.identity not in self.images and self.journal:
            self.journal.put("image", frame)
        self.images[frame.identity] = frame

    def submit(self, values, source):
        actions = [parse_action(v) for v in values]
        received = time.monotonic_ns()
        with self.condition:
            if not self.active or received >= self.deadline_ns or source["epoch"] != self.epoch:
                self.emit("action_expired", source=source, actions=[a.record() for a in actions])
                return
            first = eligible_tick(received, self.origin_ns, self.period_ns)
            for action in actions:
                self.sequence += 1
                entry = {
                    "sequence": self.sequence,
                    "received_ns": received,
                    "eligible_tick": first,
                    "source": dict(source),
                    "epoch": self.epoch,
                    "action": action,
                }
                self.actions.append(entry)
                self.emit(
                    "action_submitted",
                    **{k: v for k, v in entry.items() if k != "action"},
                    action=action.record(),
                    queue_length=len(self.actions),
                )

    def context(self, observation):
        with self.condition:
            return {
                **observation,
                "now_ns": time.monotonic_ns(),
                "run_origin_ns": self.origin_ns,
                "run_deadline_ns": self.deadline_ns,
                "tick_ms": self.period_ns / 1e6,
                "ticks_per_run": self.ticks,
                "reset_pending_sequence": self.reset_pending,
                "inflight_sequences": list(self.inflight),
                "queue": [
                    {
                        **{k: v for k, v in a.items() if k != "action"},
                        "action": a["action"].record(),
                    }
                    for a in self.actions
                ],
                "recent_events": list(self.events[-40:]),
                "capabilities": self.adapter.capabilities,
            }

    def _policy_loop(self):
        last_tick = -1
        last_request_slot = -1
        while not self.stop.is_set():
            with self.condition:
                self.condition.wait_for(
                    lambda: (
                        self.stop.is_set() or (self.latest and self.latest[0]["tick"] != last_tick)
                    ),
                    timeout=0.2,
                )
                if self.stop.is_set():
                    return
                if self.latest is None or self.latest[0]["tick"] == last_tick:
                    continue
                observation, frame = self.latest
                last_tick = observation["tick"]
                period = getattr(self.policy, "request_period", 1)
                slot = last_tick // period
                if slot == last_request_slot or last_tick % period:
                    continue
                if not getattr(self.policy, "can_request", lambda: True)():
                    continue
                context = self.context(observation)
                if not getattr(self.policy, "ready", lambda context: True)(context):
                    continue
                last_request_slot = slot
                self.retain(frame)
                self.used.append(dict(observation))
                self.journal.put("policy_observations", dict(observation))
            started = time.monotonic_ns()
            self.emit("policy_started", source=observation)
            try:
                actions = self.policy.decide(frame, context, self)
                if actions is not None:
                    self.submit(actions, observation)
                self.emit(
                    "policy_finished",
                    source=observation,
                    started_ns=started,
                    seconds=(time.monotonic_ns() - started) / 1e9,
                )
            except Exception as error:
                if not self.active:
                    self.emit("policy_cancelled_at_run_end", error_type=type(error).__name__)
                    return
                self.error = {
                    "origin": "policy",
                    "type": type(error).__name__,
                    "message": str(error)[:2000],
                }
                self.emit("policy_error", **self.error)
                self.stop.set()
                with self.condition:
                    self.condition.notify_all()

    def run(self):
        self.started_ns = time.monotonic_ns()
        self.output.mkdir(parents=True, exist_ok=False)
        self.journal = Journal(self.output)
        self.images.references = self.journal.references
        reason = "time_budget"
        try:
            self.policy_thread = threading.Thread(target=self._policy_loop, daemon=True)
            self.policy_thread.start()
            self.adapter.start(self.receive, self.emit)
            ready_until = time.monotonic() + 30
            with self.condition:
                while self.origin_ns is None:
                    if self.stop.is_set():
                        raise RuntimeError("adapter_stopped_before_first_frame:" + str(self.error))
                    if time.monotonic() >= ready_until:
                        raise TimeoutError("first_frame_timeout")
                    self.condition.wait(0.05)
            self.emit(
                "run_started",
                origin_ns=self.origin_ns,
                deadline_ns=self.deadline_ns,
                capabilities=self.adapter.capabilities,
            )
            for tick in range(self.ticks):
                due = self.origin_ns + tick * self.period_ns
                with self.condition:
                    while not self.stop.is_set() and time.monotonic_ns() < due:
                        self.condition.wait((due - time.monotonic_ns()) / 1e9)
                    if self.stop.is_set():
                        reason = (self.error["origin"] + "_error") if self.error else "stopped"
                        break
                    if self.journal.error:
                        raise RuntimeError("recording_failure:" + self.journal.error)
                    now = time.monotonic_ns()
                    if now >= due + self.period_ns:
                        self.emit("missed_tick", tick=tick, scheduled_ns=due, actual_ns=now)
                        self.adapter.release("missed_tick")
                        continue
                    frame = select_frame(self.frames, due, self.epoch)
                    observation = None
                    if frame:
                        self.retain(frame)
                        observation = {
                            **frame.public(),
                            "tick": tick,
                            "scheduled_ns": due,
                            "selected_ns": now,
                            "frame_age_ns": due
                            - (
                                frame.captured_ns
                                if frame.captured_ns is not None
                                else frame.received_ns
                            ),
                            "reused": frame.identity == self._last_frame,
                        }
                        self._last_frame = frame.identity
                        self.observations.append(observation)
                        self.journal.put("observations", dict(observation))
                        self.latest = observation, frame
                    selected = (
                        self.actions[0]
                        if self.reset_pending is None
                        and self.actions
                        and self.actions[0]["eligible_tick"] <= tick
                        else None
                    )
                    action = selected["action"] if selected else Action()
                    receipt = {
                        "tick": tick,
                        "scheduled_ns": due,
                        "window_end_ns": due + self.period_ns,
                        "epoch": self.epoch,
                        "action_sequence": selected["sequence"] if selected else None,
                        "source": selected["source"] if selected else None,
                        "queue_wait_ns": now - selected["received_ns"] if selected else None,
                        "source_age_ns": now
                        - (
                            selected["source"]["captured_ns"]
                            if selected["source"].get("captured_ns") is not None
                            else selected["source"]["received_ns"]
                        )
                        if selected
                        else None,
                    }
                    if action.kind == "reset":
                        receipt["epoch"] = self.epoch + 1
                    if selected:
                        self.actions.popleft()
                        self.inflight[selected["sequence"]] = selected
                        if action.kind == "reset":
                            self.reset_pending = selected["sequence"]
                    accepted = self.adapter.dispatch(action, receipt)
                    if accepted:
                        self.emit(
                            "tick_dispatched",
                            **receipt,
                            action=action.record(),
                            frame_id=observation["frame_id"] if observation else None,
                        )
                    else:
                        if selected:
                            self.inflight.pop(selected["sequence"], None)
                            self.actions.appendleft(selected)
                            if action.kind == "reset":
                                self.reset_pending = None
                        self.emit("adapter_busy", **receipt, action=action.record())
                    self.condition.notify_all()
            with self.condition:
                while not self.stop.is_set() and time.monotonic_ns() < self.deadline_ns:
                    self.condition.wait(min(0.05, (self.deadline_ns - time.monotonic_ns()) / 1e9))
        except Exception as error:
            self.error = {
                "origin": "host",
                "type": type(error).__name__,
                "message": str(error)[:1500],
            }
            reason = "host_error"
        finally:
            with self.condition:
                self.active = False
                self.ended_ns = time.monotonic_ns()
                self.stop.set()
                self.condition.notify_all()
                self.emit(
                    "run_ended",
                    reason=reason,
                    ended_ns=self.ended_ns,
                    pending_sequences=[a["sequence"] for a in self.actions],
                    inflight_unconfirmed_sequences=list(self.inflight),
                )
            self.adapter.release("run_end")
            try:
                self.adapter.close()
                self.policy.close()
                if self.policy_thread:
                    self.policy_thread.join(timeout=5)
                if self.policy_thread and self.policy_thread.is_alive():
                    self.emit("policy_cleanup_pending")
            except Exception as error:
                self.emit("cleanup_error", error_type=type(error).__name__)
                self.error = self.error or {"origin": "host", "type": "cleanup_error"}
            self.journal.close()
            if self.journal.error:
                self.error = self.error or {
                    "origin": "host",
                    "type": "recording_failure",
                    "message": self.journal.error,
                }
            self.persist()
        return {
            "ticks": sum(e["event"] == "tick_dispatched" for e in self.events),
            "selected_frames": len(self.observations),
            "received_frames": self.received_frames,
            "missed_ticks": sum(e["event"] == "missed_tick" for e in self.events),
            "origin_ns": self.origin_ns,
            "deadline_ns": self.deadline_ns,
            "started_ns": self.started_ns,
            "startup_seconds": (self.origin_ns - self.started_ns) / 1e9 if self.origin_ns else None,
            "ended_ns": self.ended_ns,
            "error": self.error,
            "error_origin": self.error["origin"] if self.error else None,
            "termination": reason,
            "pending_actions": len(self.actions),
            "inflight_unconfirmed_sequences": list(self.inflight),
            "real_seconds": ((self.ended_ns - self.origin_ns) / 1e9 if self.origin_ns else None),
        }

    def persist(self):
        frames = self.output / "frames"
        frames.mkdir(exist_ok=True)
        references = {}
        for identity, frame in self.images.metadata.items():
            reference = self.journal.references.get(identity)
            if reference is None:
                continue
            references[identity] = {
                **reference,
                **frame.public(),
            }
        for name, records in (
            ("frames.jsonl", [{**o, **references[o["frame_id"]]} for o in self.observations if o["frame_id"] in references]),
            ("policy_observations.jsonl", self.used),
            ("events.jsonl", self.events),
        ):
            with (self.output / name).open("w") as stream:
                for row in records:
                    stream.write(json.dumps(row, allow_nan=False) + "\n")
        (self.output / "input_authority.signal").write_text("0")
