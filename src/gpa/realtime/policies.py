"""Policy process and initial exploration clients; both use the same Runtime."""

import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time

from gpa.evidence.files import append, dump
from gpa.rtc.codec import parse_chunk
from gpa.execution.docker import Container


class ProcessPolicy:
    def __init__(self, workflow, execution, output, *, prepaid=None):
        execution, output = Path(execution).resolve(), Path(output).resolve()
        self.workflow, self.execution, self.output = workflow, execution, output
        self.home = execution / "scratch"
        self.home.mkdir()
        self.stderr = (output / "policy.stderr").open("w")
        self.container = self.process = None
        self.closed = False
        try:
            self._start(workflow, execution, prepaid)
        except BaseException:
            self.close()
            raise

    def _start(self, workflow, execution, prepaid):
        env = workflow.env(workflow.policy_token, self.home)
        self.container = Container(
            workflow.session,
            workflow.resource_group,
            "policy",
            workflow.query.get("agent_image", "rgbgame-agent:0.1.0"),
            [workflow.python, str(workflow.sdk / "claude_game_worker.py")],
            mounts=[
                (execution, execution, False),
                (workflow.sdk, workflow.sdk, False),
                (workflow.transport, workflow.transport, False),
            ],
            env=env,
            workdir=execution,
            prepaid=prepaid,
            tmpfs=[(self.home, 64 * 1024**2)],
        ).start()
        self.process = subprocess.Popen(
            ["/usr/bin/docker", "attach", "--sig-proxy=false", self.container.name],
            cwd=execution,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.number = 0
        self.process.stdin.write(json.dumps({"hello": True}) + "\n")
        self.process.stdin.flush()
        try:
            ready, _, _ = select.select([self.process.stdout], [], [], 45)
            if not ready or json.loads(self.process.stdout.readline()) != {"ready": True}:
                raise RuntimeError("policy_dependency_handshake_failed")
        except BaseException:
            self.close()
            raise

    def decide(self, frame, context, runtime):
        self.number += 1
        request = {"jpeg_base64": base64.b64encode(frame.data).decode(), "context": context}
        append(
            self.output / "policy_calls.jsonl",
            {
                "event": "request",
                "call_id": self.number,
                "frame_id": frame.identity,
                "context": context,
                "observed_ns": time.monotonic_ns(),
            },
        )
        self.process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
        self.process.stdin.flush()
        while not runtime.stop.is_set():
            ready, _, _ = select.select([self.process.stdout], [], [], 0.05)
            if not ready:
                if self.process.poll() is not None:
                    raise RuntimeError("policy_process_exit_" + str(self.process.returncode))
                continue
            line = self.process.stdout.readline(1024 * 1024)
            if not line:
                raise RuntimeError("policy_process_eof")
            response = json.loads(line)
            append(
                self.output / "policy_calls.jsonl",
                {
                    "event": "response",
                    "call_id": self.number,
                    "response": response,
                    "observed_ns": time.monotonic_ns(),
                },
            )
            if response.get("error"):
                raise RuntimeError(response["error"])
            return [response["action"]]
        append(
            self.output / "policy_calls.jsonl",
            {
                "event": "cancelled_at_run_end",
                "call_id": self.number,
                "observed_ns": time.monotonic_ns(),
            },
        )
        return None

    def read_scratch(self, name, limit=8 * 1024**2):
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("invalid scratch filename")
        script = "import os,stat,sys; fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW); info=os.fstat(fd); assert stat.S_ISREG(info.st_mode) and info.st_size<=int(sys.argv[2]); sys.stdout.buffer.write(os.read(fd,int(sys.argv[2])+1))"
        result = self.container.run(
            [self.workflow.python, "-c", script, str(self.home / name), str(limit)], timeout=5
        )
        return result.stdout if result.returncode == 0 else None

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.container:
            try:
                receipts = self.read_scratch(".game_tool_receipts.jsonl")
                if receipts:
                    (self.output / "sdk_receipts.jsonl").write_text(receipts)
            except Exception:
                dump(self.output / "sdk-receipt-export.json", {"status": "unavailable"})
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        self.stderr.close()
        if self.container:
            self.container.close()


class ExplorePolicy:
    request_period = 15

    def __init__(
        self,
        output,
        *,
        base_url,
        key,
        model,
        max_requests=20,
        task=None,
        max_tokens=8192,
        thinking=True,
    ):
        self.output = Path(output)
        self.endpoint = base_url.rstrip("/").removesuffix("/v1") + "/v1/chat/completions"
        self.key, self.model, self.max_requests = key, model, max_requests
        self.number = 0
        self.task = task
        self.rows = []
        self.thread = None
        self.finished = threading.Event()
        self.http = None
        self.max_tokens = max_tokens
        self.thinking = thinking

    def can_request(self):
        return self.number < self.max_requests

    def decide(self, frame, context, runtime):
        import httpx
        from gpa.model.stream import StreamCompletion

        self.thread = threading.current_thread()
        self.number += 1
        folder = self.output / "requests" / f"r{self.number:04d}"
        folder.mkdir(parents=True)
        offsets = [-90, -45, -15, -5, -2, -1, 0]
        by_tick = {o["tick"]: o for o in runtime.observations if o["epoch"] == frame.epoch}
        selected = [
            by_tick[context["tick"] + delta]
            for delta in offsets
            if context["tick"] + delta in by_tick
        ]
        content = []
        with runtime.condition:
            for observation in selected:
                image = runtime.images[observation["frame_id"]]
                content += [
                    {"type": "text", "text": "Observation timing: " + json.dumps(observation)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(image.data).decode()
                        },
                    },
                ]
            public_events = [
                e
                for e in runtime.events
                if e["event"]
                in {"input_sent", "input_released", "reset_boundary", "input_not_sent"}
            ][-120:]
        content.append(
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "request": self.number,
                        "request_limit": self.max_requests,
                        "current_tick": context["tick"],
                        "remaining_ticks": max(0, 300 - context["tick"]),
                        "committed_queue": context["queue"],
                        "actual_inputs": public_events,
                        "now_ns": context["now_ns"],
                        "deadline_ns": context["run_deadline_ns"],
                    }
                ),
            }
        )
        system = (
            "Play the browser game from RGB screenshots. Discover instructions, controls and objectives visually. "
            "Enter gameplay, make progress and visually recover from death or menus. "
            "The game keeps running while you think. Inputs are scheduled every 200ms in real time. "
            "Your response joins a FIFO queue at the strictly next tick after receipt; latency is real and variable. "
            "Only RGB and your own inputs, time and budget are provided. "
            "Output exactly 15 tick lines, with no explanations, numbering or code fences. "
            "Each line is (press(key1,key2)), (click(.3,.5)), (press(space),click(.3,.5)), (wait()) or (reset()). "
            "Use lowercase a-z, digits, up/down/left/right/space/enter/esc/shift/ctrl/alt/tab/backspace/delete, at most four keys. "
            "Click coordinates are normalized [0,1]. Keys release at the next tick. "
            "Reset reloads the game but does not replenish the 60-second budget; reset only on the last line."
        )
        if self.task:
            system = system.replace(
                "Reset reloads the game", "Reset uses the official same-task gameAPI.reset"
            )
            system += "\n\nOfficial task and controls:\n" + self.task
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.max_tokens,
            "temperature": 0.6,
            "top_p": 0.95,
            "chat_template_kwargs": {"enable_thinking": True},
        }
        if self.model.startswith("deepseek"):
            body.pop("chat_template_kwargs", None)
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
            if self.thinking:
                body["reasoning_effort"] = "medium"
        dump(folder / "request.json", body)
        dump(
            folder / "context.json",
            {
                "selected": selected,
                "source": frame.public(),
                "timing": context,
                "purpose": "initial_exploration",
            },
        )
        collector = StreamCompletion()
        started = time.monotonic_ns()
        error = None
        stack, ticket = ExitStack(), None
        try:
            from gpa.execution.resources import ResourceSession

            session = ResourceSession.current(required=False)
            if session:
                ticket = stack.enter_context(
                    session.model_request(body["max_tokens"], deadline_ns=runtime.deadline_ns)
                )
            from gpa.model.http import client

            self.http = client(self.endpoint, timeout=httpx.Timeout(30, connect=15))
            with self.http.stream(
                "POST", self.endpoint, json=body, headers={"Authorization": "Bearer " + self.key}
            ) as response:
                status = response.status_code
                if status != 200:
                    raise RuntimeError("initial_http_" + str(status))
                with (folder / "response.raw").open("wb") as wire:
                    for line in response.iter_lines():
                        wire.write((line + "\n").encode())
                        wire.flush()
                        if time.monotonic_ns() - started > 180_000_000_000:
                            raise TimeoutError("initial_absolute_request_timeout")
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            collector.done = True
                            break
                        if raw:
                            event = json.loads(raw)
                            append(
                                folder / "stream.jsonl",
                                {"observed_ns": time.monotonic_ns(), "chunk": event},
                            )
                            collector.add(event)
            result = collector.response()
            dump(folder / "response.json", result)
            if not collector.done or collector.error:
                raise RuntimeError("initial_incomplete_response")
            choice = result["choices"][0]
            actions = parse_chunk(
                choice["message"]["content"], 15, finish_reason=choice["finish_reason"]
            )
            return [a.text() for a in actions]
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            dump(folder / "error.json", error)
            return None
        finally:
            if ticket is not None and collector.done and collector.usage:
                ticket["output_tokens"] = collector.usage.get("completion_tokens")
            stack.close()
            if self.http:
                self.http.close()
            usage = getattr(collector, "usage", None)
            row = {
                "request_id": self.number,
                "started_ns": started,
                "finished_ns": time.monotonic_ns(),
                "usage": usage,
                "usage_complete": bool(collector.done and not collector.error and usage),
                "error": error,
            }
            dump(folder / "meta.json", row)
            self.rows.append(row)
            self.finished.set()

    def close(self):
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=185)
        if self.thread and self.thread.is_alive():
            dump(
                self.output / "initial_cleanup.json",
                {"status": "incomplete_request", "usage": None},
            )
            raise TimeoutError("initial_request_cleanup_timeout")
