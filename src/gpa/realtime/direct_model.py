"""Schedule RTC model requests from game observations."""

from contextlib import ExitStack
import base64
import json
import math
import threading
import time
from pathlib import Path

from gpa.evidence.files import append, dump
from gpa.model.stream import StreamCompletion
from gpa.rtc.codec import parse_chunk


def rtc_timing(latency_ms, tick_ms=200):
    """Size both refill and request lookahead from measured latency, rounded up."""
    if type(latency_ms) is not int or latency_ms <= 0 or tick_ms != 200:
        raise ValueError("invalid_rtc_latency_budget")
    chunk_ticks = math.ceil(latency_ms / tick_ms)
    if chunk_ticks > 120:
        raise ValueError("rtc_latency_exceeds_chunk_contract")
    return chunk_ticks, chunk_ticks + 1


def system_prompt(task, chunk_ticks, lead_ticks=None, *, game_seconds=60, response_floor_ms=0):
    common = (
        "Play this browser game from RGB screenshots and your own keyboard/mouse input history. "
        f"The game runs continuously during inference; there is no pause. You have {game_seconds} real seconds "
        "from the first observation, including all thinking and action time. Reset preserves this "
        "task's initial level and does not restore time. No private state or score is provided. "
    )
    if response_floor_ms:
        common += (
            f"A response is held until at least {response_floor_ms / 1000:g} seconds after the request starts. "
            "If inference takes longer, it becomes available only when finished. "
            "The game continues during both inference and this additional hold. "
        )
    lead_ticks = chunk_ticks + 1 if lead_ticks is None else lead_ticks
    specific = (
        f"Output exactly {chunk_ticks} tick lines, no explanations, numbering or code fences. "
        f"This appends {chunk_ticks * 0.2:g} seconds of actions. "
        "Each line is (press(key1,key2)), (click(.3,.5)), (press(space),click(.3,.5)), "
        "(wait()) or (reset()). Use lowercase a-z, digits, up/down/left/right/space/enter/esc/"
        "shift/ctrl/alt/tab/backspace/delete, at most four keys. Click coordinates are normalized0-1. "
        "Each tick is200ms: press keys at tick start and release at tick end; clicks immediately release. "
        "Your reply appends to a FIFO queue, eligible at the strictly next tick after receipt. "
        "One request is in flight at a time. The committed queue keeps executing while you think. "
        f"The host requests a refill when at most {lead_ticks} ticks "
        f"({lead_ticks * 0.2:g} seconds) remain queued. "
        f"Plan for an estimated response delay of {chunk_ticks * 0.2:g} seconds; actual delay varies. "
        "Plan for the state AFTER the shown committed queue, accounting for real inference delay. "
        "Reset is allowed only on the last line. Mixed history contains dense recent and sparse old frames."
    )
    return common + specific + "\n\nOfficial task and controls:\n" + task


def request_body(
    model, task, content, chunk_ticks, lead_ticks=None, *, game_seconds=60, response_floor_ms=0
):
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt(
                    task,
                    chunk_ticks,
                    lead_ticks,
                    game_seconds=game_seconds,
                    response_floor_ms=response_floor_ms,
                ),
            },
            {"role": "user", "content": content},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 8192,
        "temperature": 0.6,
        "top_p": 0.95,
        "reasoning_effort": "medium",
        "chat_template_kwargs": {"enable_thinking": True},
    }
    return body


def compact_inputs(events):
    """Retain actual tick/action evidence without repeating CDP receipts and source frames."""
    rows = []
    for e in events:
        if e["event"] == "input_sent":
            row = {
                "epoch": e["epoch"],
                "from_tick": e.get("tick"),
                "to_tick": e.get("tick"),
                "action": e["action"],
            }
            if (
                rows
                and type(row["from_tick"]) is int
                and type(rows[-1].get("to_tick")) is int
                and row["from_tick"] == rows[-1]["to_tick"] + 1
                and row["epoch"] == rows[-1].get("epoch")
                and row["action"] == rows[-1].get("action")
            ):
                rows[-1]["to_tick"] = row["to_tick"]
            else:
                rows.append(row)
        elif e["event"] in {"reset_boundary", "input_not_sent"}:
            rows.append({k: e[k] for k in ("event", "epoch", "tick", "reason") if k in e})
    return rows


def compact_queue(queue):
    return [{"sequence": item["sequence"], "action": item["action"]} for item in queue]


def request(endpoint, key, body, folder, *, deadline_ns=None):
    import httpx

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    dump(folder / "request.json", body)
    collector, started, first, error = StreamCompletion(), time.monotonic_ns(), None, None
    admission_started = started
    stack, ticket = ExitStack(), None
    try:
        from gpa.execution.resources import ResourceSession

        session = ResourceSession.current(required=False)
        if session:
            ticket = stack.enter_context(
                session.model_request(
                    body["max_tokens"], deadline_ns=deadline_ns, priority="realtime"
                )
            )
        started = time.monotonic_ns()
        from gpa.model.http import client

        with client(endpoint, timeout=httpx.Timeout(60, connect=15)) as http:
            with http.stream(
                "POST", endpoint, json=body, headers={"Authorization": "Bearer " + key}
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError("model_http_" + str(response.status_code))
                with (folder / "response.raw").open("wb") as wire:
                    for line in response.iter_lines():
                        now = time.monotonic_ns()
                        wire.write((line + "\n").encode())
                        wire.flush()
                        if now - started > 180_000_000_000:
                            raise TimeoutError("absolute_request_timeout")
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            collector.done = True
                            break
                        if raw:
                            event = json.loads(raw)
                            append(folder / "stream.jsonl", {"observed_ns": now, "chunk": event})
                            collector.add(event)
                            if first is None and any(
                                c.get("delta", {}).get(k)
                                for c in event.get("choices", [])
                                for k in ("content", "reasoning_content")
                            ):
                                first = now
        result = collector.response()
        dump(folder / "response.json", result)
        if (
            not collector.done
            or collector.error
            or not collector.usage
            or type(collector.usage.get("completion_tokens")) is not int
        ):
            raise RuntimeError("incomplete_response_or_usage")
        return result
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        if ticket is not None and collector.done and collector.usage:
            ticket["output_tokens"] = collector.usage.get("completion_tokens")
        stack.close()
        dump(
            folder / "meta.json",
            {
                "started_ns": started,
                "finished_ns": time.monotonic_ns(),
                "first_token_ns": first,
                "admission_wait_ns": started - admission_started,
                "usage": collector.usage,
                "usage_complete": bool(collector.done and not collector.error and collector.usage),
                "error": error,
            },
        )


def response_gate(started_ns, finished_ns, floor_ms, deadline_ns, stop):
    """Delay availability, not the game clock. Cancellation never grants action rights."""
    if type(floor_ms) is not int or not 0 <= floor_ms <= 60000:
        raise ValueError("invalid_response_floor")
    target = started_ns + floor_ms * 1_000_000
    wait_started = time.monotonic_ns()
    while not stop.is_set():
        remaining = min(target, deadline_ns) - time.monotonic_ns()
        if remaining <= 0:
            break
        stop.wait(remaining / 1e9)
    released = time.monotonic_ns()
    return {
        "request_started_ns": started_ns,
        "http_finished_ns": finished_ns,
        "floor_ms": floor_ms,
        "target_release_ns": target,
        "released_ns": released,
        "wait_ns": released - wait_started,
        "effective_latency_ns": released - started_ns,
        "http_over_floor": finished_ns > target,
        "cancelled": stop.is_set() or released >= deadline_ns,
    }


class ModelPolicy:
    request_period = 1

    def __init__(
        self,
        output,
        *,
        base_url,
        key,
        model,
        task,
        chunk_ticks=30,
        lead_ticks=15,
        game_seconds=60,
        response_floor_ms=0,
        max_requests=300,
        max_tokens=8192,
        thinking=True,
    ):
        self.output, self.key, self.model, self.task = Path(output), key, model, task
        self.endpoint = base_url.rstrip("/").removesuffix("/v1") + "/v1/chat/completions"
        self.chunk_ticks, self.lead_ticks = chunk_ticks, lead_ticks
        self.game_seconds, self.response_floor_ms = game_seconds, response_floor_ms
        self.number, self.rows, self.last_error = 0, [], None
        self.thread = None
        self.max_requests, self.max_tokens = max_requests, max_tokens
        self.thinking = thinking

    def can_request(self):
        return self.number < self.max_requests

    def ready(self, context):
        return context["reset_pending_sequence"] is None and len(context["queue"]) <= self.lead_ticks

    def decide(self, frame, context, runtime):
        self.thread = threading.current_thread()
        folder = self.output / "requests" / f"r{self.number + 1:04d}"
        content, selected = [], []
        with runtime.condition:
            by_tick = {o["tick"]: o for o in runtime.observations if o["epoch"] == frame.epoch}
            selected = [
                by_tick[context["tick"] + d]
                for d in (-90, -45, -15, -5, -2, -1, 0)
                if context["tick"] + d in by_tick
            ]
            for o in selected:
                content += image_blocks(runtime.images[o["frame_id"]], o)
            inputs = [
                e
                for e in runtime.events
                if e["event"] in {"input_sent", "reset_boundary", "input_not_sent"}
            ][-120:]
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "committed_queue": compact_queue(context["queue"]),
                        "actual_inputs": compact_inputs(inputs),
                    }
                ),
            }
        )
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "request": self.number + 1,
                        "current_tick": context["tick"],
                        "now_ns": context["now_ns"],
                        "deadline_ns": context["run_deadline_ns"],
                        "last_protocol_error": self.last_error,
                    }
                ),
            }
        )
        body = request_body(
            self.model,
            self.task,
            content,
            self.chunk_ticks,
            self.lead_ticks,
            game_seconds=self.game_seconds,
            response_floor_ms=self.response_floor_ms,
        )
        body["max_tokens"] = self.max_tokens
        if self.model.startswith("deepseek"):
            body.pop("chat_template_kwargs", None)
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
            if not self.thinking:
                body.pop("reasoning_effort", None)
        if not runtime.active or time.monotonic_ns() >= runtime.deadline_ns:
            return None
        self.number += 1
        try:
            result = request(self.endpoint, self.key, body, folder, deadline_ns=runtime.deadline_ns)
        finally:
            dump(
                folder / "context.json",
                {"selected": selected, "source": frame.public(), "timing": context},
            )
            meta = json.loads((folder / "meta.json").read_text())
            self.rows.append(meta)
            gate = response_gate(
                meta["started_ns"],
                meta["finished_ns"],
                self.response_floor_ms,
                runtime.deadline_ns,
                runtime.stop,
            )
            dump(folder / "gate.json", gate)
        choice = result["choices"][0]
        try:
            actions = [
                action.text()
                for action in parse_chunk(
                    choice["message"]["content"],
                    self.chunk_ticks,
                    finish_reason=choice["finish_reason"],
                    max_count=120,
                )
            ]
        except (ValueError, TypeError, KeyError) as exc:
            self.last_error = str(exc)[:300]
            dump(folder / "protocol_error.json", {"error": self.last_error})
            return None
        self.last_error = None
        dump(folder / "parsed.json", actions)
        if gate["cancelled"] or not runtime.active or time.monotonic_ns() >= runtime.deadline_ns:
            dump(folder / "execution.json", {"status": "expired_at_deadline"})
            return None
        return actions

    def close(self):
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=245)
        if self.thread and self.thread.is_alive():
            raise TimeoutError("request_writer_not_closed")


def image_blocks(frame, timing):
    return [
        {"type": "text", "text": "RGB observation timing: " + json.dumps(timing)},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame.data).decode()},
        },
    ]
