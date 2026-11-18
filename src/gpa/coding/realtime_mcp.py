"""Expose policy submission and peer review as MCP tools."""

import json
import sys


def serve(source=None, destination=None, rpc=None, *, review_mode=False, review_path=None):
    if rpc is None:
        from claude_game_tools import rpc
    source, destination = source or sys.stdin, destination or sys.stdout
    for line in source:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            response["result"] = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "realtime-game", "version": "1.0.0"},
            }
        elif method == "ping":
            response["result"] = {}
        elif method == "tools/list":
            actions = [
                (
                    "status",
                    "Return remaining submits and the shared cumulative output-token hard budget.",
                )
            ]
            if not review_mode:
                actions.insert(
                    0,
                    (
                        "submit",
                        "Freeze workspace/policy and play a fresh 60-second game. Returns RGB evidence, remaining submits and observed token usage.",
                    ),
                )
            tools = [
                {
                    "name": name,
                    "description": description,
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
                for name, description in actions
            ]
            if review_mode:
                tools.append(
                    {
                        "name": "write_review",
                        "description": "Save or replace peer_review.md in your own workspace. Write a concise draft early; finish after delivery.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string", "minLength": 1, "maxLength": 131072}
                            },
                            "required": ["content"],
                            "additionalProperties": False,
                        },
                    }
                )
            response["result"] = {"tools": tools}
        elif method == "tools/call":
            params = request.get("params", {})
            name = params.get("name")
            try:
                arguments = params.get("arguments", {})
                if review_mode and name == "write_review":
                    from pathlib import Path
                    import os
                    import tempfile

                    if (
                        not isinstance(arguments, dict)
                        or set(arguments) != {"content"}
                        or not isinstance(arguments["content"], str)
                    ):
                        raise ValueError("review_content_required_without_extra_arguments")
                    data = arguments["content"].encode("utf-8")
                    if not data.strip() or len(data) > 131072:
                        raise ValueError("review_content_size_invalid")
                    target = (
                        Path(review_path)
                        if review_path is not None
                        else Path.cwd() / "peer_review.md"
                    )
                    if target.is_symlink():
                        raise ValueError("review_target_symlink")
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=".peer-review-", dir=target.parent
                    )
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(data)
                        os.replace(temporary, target)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                    value = {"saved": "peer_review.md", "bytes": len(data)}
                else:
                    if (
                        name not in ({"status"} if review_mode else {"submit", "status"})
                        or arguments
                    ):
                        raise ValueError("unknown_tool_or_unexpected_arguments")
                    value = rpc(name)
                response["result"] = {
                    "content": [{"type": "text", "text": json.dumps(value)}],
                    "isError": bool(value.get("error")),
                }
            except Exception as error:
                response["result"] = {
                    "content": [{"type": "text", "text": str(error)[:1000]}],
                    "isError": True,
                }
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        destination.write(json.dumps(response) + "\n")
        destination.flush()


def develop(
    workflow,
    *,
    phase="development",
    resume=False,
    prompt_override=None,
    prompt_suffix="",
    allow_submit=True,
    timeout_seconds=None,
):
    import os
    import queue
    import signal
    import subprocess
    import time
    import uuid
    from gpa.evidence.files import dump

    w = workflow
    if not resume:
        if getattr(w, "developer_session", None):
            raise ValueError("refuse_unplanned_new_developer_session")
        w.developer_session = str(uuid.uuid4())
    elif not getattr(w, "developer_session", None):
        raise ValueError("resume_requires_existing_session")
    w.invocations = getattr(w, "invocations", 0) + 1
    invocation_name = "%02d" % w.invocations
    w.termination = None
    config = w.workspace / "mcp.json"
    dump(
        config,
        {
            "mcpServers": {
                "game": {
                    "type": "stdio",
                    "command": w.python,
                    "args": [
                        str(w.sdk / "realtime_mcp.py"),
                        *([] if allow_submit else ["--review"]),
                    ],
                }
            }
        },
    )
    prompt = (
        "Read TASK.md, TOOLS.md and .claude/skills/game-policy/SKILL.md. "
        "Use the initial RGB exploration to edit policy/policy.py. Call mcp__game__submit to run it, "
        "inspect the actual feedback and frames, revise, and submit again. "
        "You have at most %d submits; use them to improve the policy, then finish. "
        % w.round_limit
        + "Each submit starts a fresh 60-second real-time game; input is scheduled every 200ms, without pause. "
        "Measure latency and queue behavior from the public evidence. Only submit runs games. "
        "Do not inspect private files, game source, DOM or CDP. Official score/success is unavailable. "
        "All development, auxiliary and compaction calls share a hard cumulative output budget of %d tokens. "
        "Submitting does not reset it. When exhausted, development ends even if submits remain. "
        "Tool feedback reports remaining budget; submit working improvements before it runs out. "
        "Failed or unknown-usage requests are recorded separately and do not consume this quota. "
        "Model service retries are managed by Claude Code. "
        % w.query.get("developer_output_tokens", 262144)
        + "Read frames selectively and prioritize a working policy over a large design. "
        "Initial evidence: " + json.dumps(w.initial)
    )
    if w.query.get("engineering_canary"):
        prompt = (
            "ENGINEERING SMOKE CHECK, not a game-solving benchmark. Exercise the interface promptly: "
            "read the policy contract and existing minimal policy, make a small legal action change, "
            "compile once and submit. Do not build a solver or repeatedly analyze frames. "
            "After feedback (and a scheduled peer review if present), change one policy parameter "
            "and submit once more, then finish. A low game score is acceptable in this smoke check. "
            + prompt
        )
    if w.query.get("engineering_validation") and allow_submit:
        prompt = (
            "This engineering validation requires two distinct working policy submissions. "
            "Implement a real strategy for the stated game, submit it, inspect RGB/input evidence, "
            "make an evidence-based change to policy.py, and submit the revised policy. "
            "In team mode the host pauses you after the first submit, schedules the one peer review, "
            "then resumes this same conversation for your second submit. Complete the submit "
            "required by the current phase before ending your answer. Low game progress is "
            "acceptable; missing submissions, invalid actions and missing peer review are not. "
            "Keep the first strategy concise and submit it early, within about one quarter of "
            "your output budget. Compile once, then learn from the actual 60-second run. "
            "The first policy should be a small baseline (at most about 80 lines) that sends "
            "keyboard/mouse input immediately. Your next step after writing and compiling that "
            "baseline must be mcp__game__submit, before running local analysis or simulation scripts. "
            "Reserve output for the second revision and the peer review. Avoid long offline "
            "simulation benchmarks and extensive test programs in this engineering profile. "
            + prompt
        )
    prompt = (prompt_override if prompt_override is not None else prompt) + prompt_suffix
    if w.query.get("engineering_validation") and not allow_submit:
        prompt = (
            "Engineering review: Read the peer's submitted code and one public RGB frame, then immediately use mcp__game__write_review. Save at most 80 words, with one file reference, one concrete suggestion and uncertainty. Bash/Edit/Write are disabled; only Read and the MCP review tool are available. Finish after a valid tool call. "
            + prompt
        )
    env = w.env(w.dev_token, w.workspace / "home")
    env.update(
        ANTHROPIC_BASE_URL="http://127.0.0.1:18080",
        ANTHROPIC_AUTH_TOKEN=w.proxy_token,
        ANTHROPIC_API_KEY=w.proxy_token,
        ANTHROPIC_MODEL=w.query["developer_model"],
        ANTHROPIC_DEFAULT_HAIKU_MODEL=w.query["developer_model"],
        ANTHROPIC_DEFAULT_SONNET_MODEL=w.query["developer_model"],
        ANTHROPIC_DEFAULT_OPUS_MODEL=w.query["developer_model"],
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        ENABLE_TOOL_SEARCH="false",
        CLAUDE_CODE_MAX_OUTPUT_TOKENS="32768",
        CLAUDE_CONFIG_DIR=str(w.workspace / "home/.claude"),
        API_TIMEOUT_MS=str(1000 * w.query.get("request_timeout_seconds", 1800)),
    )
    native_tools = ["Read", "Edit", "Write", "Bash"] if allow_submit else ["Read"]
    command = [
        str(w.sdk / "claude"),
        "--bare",
        "-p",
        "--model",
        w.query["developer_model"],
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--effort",
        "low" if w.query.get("engineering_validation") else "medium",
        "--setting-sources",
        "",
        "--tools",
        ",".join(native_tools),
        "--allowedTools",
        *native_tools,
        "mcp__game__status",
        *(["mcp__game__submit"] if allow_submit else ["mcp__game__write_review"]),
        "--strict-mcp-config",
        "--mcp-config",
        str(config),
        "--resume" if resume else "--session-id",
        w.developer_session,
        "--",
        prompt,
    ]
    dump(
        w.private / ("development-" + invocation_name + ".json"),
        {
            "prompt": prompt,
            "session": w.developer_session,
            "phase": phase,
            "resume": resume,
            "mode": "claude_mcp",
            "token_policy": "hard_cumulative",
            "submit_limit": w.round_limit,
            "effort": "low" if w.query.get("engineering_validation") else "medium",
            "output_token_limit": w.query.get("developer_output_tokens", 262144),
        },
    )
    started = time.monotonic()
    stop_at = None
    with (
        (w.private / ("claude-" + invocation_name + ".jsonl")).open("w") as stdout,
        (w.private / ("claude-" + invocation_name + ".stderr")).open("w") as stderr,
    ):
        process = subprocess.Popen(
            w.sandbox_command(command, [], [w.workspace], env=env),
            cwd=w.workspace,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                try:
                    event, response = w.queue.get(timeout=0.2)
                except queue.Empty:
                    pass
                else:
                    try:
                        response.update(
                            w.submit() if allow_submit else {"error": "review_phase_forbids_submit"}
                        )
                    except Exception as error:
                        response.update(error=type(error).__name__ + ":" + str(error)[:500])
                    finally:
                        event.set()
                state = w.budget.snapshot()
                if state["stop_reason"]:
                    stop_at = stop_at or time.monotonic()
                    if time.monotonic() - stop_at >= 2 and not state["active_requests"]:
                        w.termination = state["stop_reason"]
                        break
                if time.monotonic() - started > (
                    timeout_seconds
                    if timeout_seconds is not None
                    else w.query.get("development_timeout_seconds", 18000)
                ):
                    raise TimeoutError("workflow_infrastructure_timeout")
            w.termination = (
                w.termination
                or w.budget.snapshot()["stop_reason"]
                or ("submission_limit" if len(w.rounds) >= w.round_limit else "developer_finished")
            )
            stop_reason = w.budget.snapshot()["stop_reason"]
            if stop_reason and stop_reason not in {
                "output_token_budget", "planned_midpoint", "review_output_limit", "submission_limit"
            }:
                raise RuntimeError("development_infrastructure_stopped:" + stop_reason)
            if process.returncode and not stop_reason:
                raise RuntimeError("claude_process_exit_%d" % process.returncode)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            exit_record = {
                "returncode": process.returncode,
                "phase": phase,
                "session": w.developer_session,
                "elapsed_seconds": time.monotonic() - started,
                "termination": w.termination,
            }
            dump(w.private / ("claude-exit-" + invocation_name + ".json"), exit_record)
            dump(w.private / "claude-exit.json", exit_record)
    return w.termination


if __name__ == "__main__":
    serve(review_mode="--review" in sys.argv[1:])
