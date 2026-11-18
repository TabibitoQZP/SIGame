"""Execute submitted policy versions and collect feedback."""

import json
import os
import shutil
import socketserver
import threading
import time
import uuid
from gpa.evidence.files import append, dump
from gpa.config import DEFAULT_BROWSER_MEMORY_MIB
from gpa.realtime.policies import ProcessPolicy
from .materials import snapshot, own_tree

DEV_UID = os.getuid()


class SubmissionHost:
    def start_tools(self):
        workflow = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    body = json.loads(self.rfile.readline(24 * 1024 * 1024))
                    token, op = body.get("token"), body.get("operation")
                    is_dev = token == workflow.dev_token
                    is_policy = workflow.policy_token is not None and token == workflow.policy_token
                    if not is_dev and not is_policy:
                        raise ValueError("unauthorized")
                    args = body.get("arguments", {})
                    call_id = body.get("call_id", uuid.uuid4().hex)
                    append(
                        workflow.private / "tool_calls.jsonl",
                        {
                            "event": "request",
                            "call_id": call_id,
                            "operation": op,
                            "role": "developer" if is_dev else "policy",
                            "context": body.get("context"),
                            "arguments": args,
                            "active_round": workflow.active_round,
                            "observed_ns": time.monotonic_ns(),
                        },
                    )
                    if op == "status" and is_dev:
                        result = workflow.status()
                    elif op == "submit" and is_dev:
                        event, response = threading.Event(), {}
                        workflow.queue.put((event, response))
                        if not event.wait(1800):
                            raise TimeoutError("submission_timeout")
                        result = response
                    elif op in {"vision", "jev"}:
                        if is_policy:
                            workflow.runtime_calls += 1
                            if workflow.runtime_calls > 2400:
                                raise ValueError("per_run_visual_service_call_limit")
                        result = workflow.perceive(
                            op,
                            {**args, "_provenance": body.get("context"), "_call_id": call_id},
                            "policy" if is_policy else "developer",
                        )
                    else:
                        raise ValueError("operation_not_allowed")
                    value = {"ok": True, "result": result}
                    append(
                        workflow.private / "tool_calls.jsonl",
                        {
                            "event": "response",
                            "call_id": call_id,
                            "operation": op,
                            "result": result,
                            "observed_ns": time.monotonic_ns(),
                        },
                    )
                except Exception as error:
                    value = {"ok": False, "error": str(error)[:1000]}
                    if "call_id" in locals():
                        append(
                            workflow.private / "tool_calls.jsonl",
                            {
                                "event": "response",
                                "call_id": call_id,
                                "operation": op,
                                "error": value["error"],
                                "observed_ns": time.monotonic_ns(),
                            },
                        )
                self.wfile.write((json.dumps(value, allow_nan=False) + "\n").encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.tools = Server(str(self.transport / "tools.sock"), Handler)
        os.chmod(self.transport / "tools.sock", 0o600)
        threading.Thread(target=self.tools.serve_forever, daemon=True).start()

    def publish_round(self, run, name, feedback):
        target = self.workspace / "evidence" / name
        target.mkdir(parents=True)
        play = run / "play"
        for filename in [
            "frames",
            "frames.jsonl",
            "events.jsonl",
            "policy_observations.jsonl",
            "frame_objects.jsonl",
        ]:
            source = play / filename
            if source.is_dir():
                shutil.copytree(source, target / filename)
            elif source.is_file():
                shutil.copyfile(source, target / filename)
        dump(target / "feedback.json", feedback)
        frames = (
            [json.loads(s) for s in (target / "frames.jsonl").read_text().splitlines()]
            if (target / "frames.jsonl").exists()
            else []
        )
        preview = (
            [
                frames[i]["path"]
                for i in sorted(
                    {0, len(frames) // 4, len(frames) // 2, 3 * len(frames) // 4, len(frames) - 1}
                )
            ]
            if frames
            else []
        )
        own_tree(target, DEV_UID)
        return {
            **feedback,
            "directory": str(target),
            "frames": str(target / "frames.jsonl"),
            "events": str(target / "events.jsonl"),
            "preview_frames": [str(target / p) for p in preview],
        }

    def submit(self):
        if self.submitting or len(self.rounds) >= self.round_limit:
            return {"error": "submission_busy_or_closed", **self.status()}
        token_usage_before_submit = self.budget.mark_submit() if self.mcp_agent else None
        self.submitting = True
        number = len(self.rounds) + 1
        self.active_round = number
        self.policy_token = uuid.uuid4().hex
        self.runtime_calls = 0
        run = self.private / f"round-{number:02d}"
        run.mkdir()
        execution = self.root / f"execution-{number:02d}"
        append(
            self.private / "submissions.jsonl",
            {"event": "submitted", "round": number, "time_ns": time.monotonic_ns()},
        )
        try:
            hashes = snapshot(self.workspace / "policy", execution)
            dump(run / "policy_manifest.json", hashes)
            shutil.copytree(execution, run / "policy")
            public = self.workspace / "versions" / f"round-{number:02d}" / "policy"
            public.parent.mkdir(parents=True)
            shutil.copytree(execution, public)
            own_tree(public, DEV_UID, writable=False)
            with self.session.play(
                self.resource_group,
                browser=self.query["backend"] != "fake",
                browser_memory=self.query.get("browser_memory_mib", DEFAULT_BROWSER_MEMORY_MIB) * 1024**2,
            ) as slot:
                policy = None
                try:
                    policy = ProcessPolicy(self, execution, run, prepaid=slot)
                    result = self.operation.run(policy, run / "play", prepaid=slot)
                finally:
                    if policy:
                        policy.close()
            result.update(
                round=number,
                policy_version=f"round-{number:02d}",
                policy_hashes=hashes,
                visual_calls=self.runtime_calls,
            )
        except Exception as error:
            result = {
                "round": number,
                "error": type(error).__name__ + ":" + str(error)[:2000],
                "error_origin": "host",
                "ticks": 0,
                "termination": "submission_setup_error",
                "host_score": {"raw_score": None, "classification": "execution_error"},
            }
        finally:
            self.policy_token = None
            self.submitting = False
        self.rounds.append(result)
        dump(run / "result.json", result)
        if len(self.rounds) >= self.round_limit:
            self.budget.close("submission_limit")
        feedback = {
            k: result.get(k)
            for k in [
                "round",
                "ticks",
                "real_seconds",
                "error",
                "error_origin",
                "termination",
                "policy_version",
                "visual_calls",
                "pending_actions",
                "missed_ticks",
            ]
        }
        feedback.update(
            success=None,
            success_available=False,
            remaining_rounds=self.round_limit - len(self.rounds),
            output_budget=self.budget.snapshot(),
            policy_directory=str(public) if "public" in locals() else None,
        )
        if self.mcp_agent:
            feedback["token_usage_before_submit"] = token_usage_before_submit
            feedback["reminder"] = (
                f"{self.round_limit - len(self.rounds)} submits remain. "
                "The remaining cumulative output budget is a hard cap and does not reset on submit. "
                "Inspect the actual RGB feedback, revise and submit again if attempts remain."
            )
        published = self.publish_round(run, f"round-{number:02d}", feedback)
        dump(run / "public_feedback.json", published)
        return published
