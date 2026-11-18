"""Create a workspace for a single policy developer."""

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import tempfile
import uuid

from gpa.config import endpoint
from gpa.evidence.files import dump, load
from gpa.realtime.policies import ExplorePolicy
from gpa.execution.docker import Container, ModelBridge
from gpa.execution.resources import ResourceSession
from .materials import own_tree
from .realtime_observer import ObservedUsage, start_proxy
from .submission import SubmissionHost


class Workflow(SubmissionHost):
    def __init__(self, root, query, operation, *, budget_factory=None):
        gateway, key = endpoint(query, "anthropic")
        self.root, self.query, self.operation = Path(root).resolve(), query, operation
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.workspace, self.sdk = self.root / "workspace", self.root / "sdk"
        self.workspace.mkdir()
        self.sdk.mkdir()
        self.round_limit = query["development_rounds"]
        self.rounds, self.submitting, self.success = [], False, None
        self.mcp_agent, self.exploration_arm = True, None
        self.config = {"task": query["task"], "native_controls": operation.capabilities}
        self.budget = (budget_factory or ObservedUsage)(
            query["developer_output_tokens"], self.private / "accounting", enforce=True
        )
        self.budget.persist()
        self.termination = None
        self.queue = queue.Queue()
        self.dev_token, self.policy_token = uuid.uuid4().hex, None
        self.tool_lock = threading.Lock()
        self.tool_count = self.visual_inflight = self.runtime_calls = 0
        self.active_round = None
        self.vision_cache = {}
        self.session = ResourceSession.current()
        self.resource_group = query["_resource_group"]
        self.transport = Path(tempfile.mkdtemp(prefix="rgbgame-tools-"))
        self.python = "/opt/venv/bin/python"
        self.developer = self.model_bridge = None
        self.setup_workspace()
        self.start_tools()
        self.proxy_token = uuid.uuid4().hex
        try:
            self.proxy = start_proxy(
                self.private / "claude-http",
                gateway.removesuffix("/v1"),
                key,
                self.proxy_token,
                budget=self.budget,
                status=self.status,
                request_seconds=query["request_timeout_seconds"],
                expected_model=None if query["fake"] else query["developer_model"],
                engineering_profile=query.get("engineering_validation", False),
            )
            self.model_bridge = ModelBridge(self.transport / "model.sock", self.proxy.server_port)
            self.developer = Container(
                self.session,
                self.resource_group,
                "developer",
                query.get("agent_image", "rgbgame-agent:0.1.0"),
                [self.python, "/opt/rgbgame/bridge.py", str(self.transport / "model.sock")],
                mounts=[
                    (self.workspace, self.workspace, True),
                    (self.sdk, self.sdk, False),
                    (self.transport, self.transport, False),
                    *[
                        (self.workspace / name, self.workspace / name, False)
                        for name in ("evidence", "versions", "peers")
                    ],
                ],
                env=self.env(self.dev_token, self.workspace / "home"),
                workdir=self.workspace,
            ).start()
            if not self.query["fake"]:
                version = self.developer.run(["/usr/local/bin/claude", "--version"], timeout=30)
                if version.returncode:
                    raise RuntimeError("Claude Code is missing from the configured agent image")
                dump(
                    self.private / "cli-version.json",
                    {
                        "implementation": "claude_code",
                        "real_claude": True,
                        "version": version.stdout.strip(),
                    },
                )
            probe = self.developer.run(
                [
                    self.python,
                    "-c",
                    "import cv2,numpy,PIL,claude_game_tools; print('dependencies_ok')",
                ],
                env=self.env(self.dev_token, self.workspace / "home"),
                cwd=self.workspace,
                timeout=120,
            )
            dump(
                self.private / "dependency_probe.json",
                {"returncode": probe.returncode, "stdout": probe.stdout, "stderr": probe.stderr},
            )
            if probe.returncode:
                raise RuntimeError("developer_dependency_probe_failed:" + probe.stderr[-1000:])
        except BaseException:
            if self.developer:
                self.developer.close()
            if self.model_bridge:
                self.model_bridge.close()
            self.tools.shutdown()
            self.tools.server_close()
            if hasattr(self, "proxy"):
                self.proxy.shutdown()
                self.proxy.server_close()
            shutil.rmtree(self.transport, ignore_errors=True)
            raise

    def setup_workspace(self):
        source = Path(__file__).parent
        for src, dest in (
            ("realtime_tools.py", "claude_game_tools.py"),
            ("realtime_policy_worker.py", "claude_game_worker.py"),
            ("realtime_mcp.py", "realtime_mcp.py"),
        ):
            shutil.copyfile(source / src, self.sdk / dest)
        if self.query["fake"]:
            command = [self.python, "/opt/rgbgame/fake_claude.py"]
            dump(
                self.private / "cli-version.json",
                {"implementation": "bundled_fake_cli", "real_claude": False},
            )
        else:
            command = ["/usr/local/bin/claude"]
        wrapper = self.sdk / "claude"
        wrapper.write_text(
            f"#!{self.python}\nimport os, sys\nos.execv({command[0]!r}, {command!r} + sys.argv[1:])\n"
        )
        wrapper.chmod(0o755)
        shutil.copytree(
            source.parent / "skills/game-policy", self.workspace / ".claude/skills/game-policy"
        )
        (self.workspace / "policy/assets").mkdir(parents=True)
        (self.workspace / "home").mkdir()
        for name in ("evidence", "versions", "peers"):
            (self.workspace / name).mkdir()
        shutil.copyfile(
            source.parent / "skills/game-policy/policy_example.py",
            self.workspace / "policy/policy.py",
        )
        (self.workspace / "TASK.md").write_text(
            self.query["task"]
            + "\n\nAllowed controls:\n"
            + json.dumps(self.operation.capabilities, indent=2)
            + "\n"
        )
        (self.workspace / "TOOLS.md").write_text(
            "Read TASK.md and .claude/skills/game-policy/SKILL.md.\n"
            "Use MCP mcp__game__submit with no arguments to freeze policy/ and start a fresh run.\n"
            "Use mcp__game__status for remaining submits and the shared output-token budget.\n"
            "Python SDK: from claude_game_tools import vision, jev, crop, context.\n"
            "Configured neural services: "
            + json.dumps(
                {
                    name: bool(endpoint(self.query, name, required=False)[0])
                    for name in ("cv", "jev")
                }
            )
            + "\n"
        )
        if self.query["fake"]:
            (self.workspace / "FAKE_TEST.json").write_text(
                json.dumps(
                    {"midpoint": self.query.get("midpoint_submit"), "submits": self.round_limit}
                )
            )
        own_tree(self.sdk, os.getuid(), writable=False)
        wrapper.chmod(0o555)

    def sandbox_command(self, command, read_only, writable, *, env=None):
        if self.developer is None:
            raise RuntimeError("Docker developer container is not ready")
        return self.developer.exec_command(command, env=env, cwd=self.workspace)

    def env(self, token, home):
        temporary = home / "tmp"
        temporary.mkdir(exist_ok=True)
        env = {
            "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(self.sdk),
            "GAME_TOOL_SOCKET": str(self.transport / "tools.sock"),
            "GAME_TOOL_TOKEN": token,
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
        if self.query["fake"]:
            env["RGBGAME_TRUSTED_FAKE_FIXTURE"] = "1"
        return env

    def status(self):
        return {
            "success": None,
            "success_available": False,
            "developer_session_id": getattr(self, "developer_session", None),
            "submitted_rounds": len(self.rounds),
            "remaining_rounds": self.round_limit - len(self.rounds),
            "workspace": str(self.workspace),
            "tick_ms": 200,
            "ticks_per_run": self.query["game_seconds"] * 5,
            "submission_in_progress": self.submitting,
            "output_budget": self.budget.snapshot(),
        }

    def develop(self):
        from .realtime_mcp import develop

        result = develop(self)
        self.budget.close(result)
        return result

    def import_initial(self, source):
        run = self.private / "initial"
        shutil.copytree(source, run)
        result = load(run / "result.json")
        feedback = {
            key: result.get(key)
            for key in ("ticks", "real_seconds", "error", "termination", "initial_requests")
        }
        feedback.update(
            success=None,
            success_available=False,
            remaining_rounds=self.round_limit,
            shared_initial=True,
        )
        self.initial = self.publish_round(run, "initial", feedback)
        dump(self.workspace / "INITIAL.json", self.initial)
        return result

    def perceive(self, op, args, role):
        from .vision import perceive

        with self.tool_lock:
            self.tool_count += 1
            index = self.tool_count
            self.visual_inflight += 1
        started = time.monotonic_ns()
        folder = self.workspace / "evidence/visual_calls" / f"{index:06d}"
        folder.mkdir(parents=True)
        dump(
            folder / "request.json",
            {
                "operation": op,
                "role": role,
                "arguments": {k: v for k, v in args.items() if k != "image"},
            },
        )
        hit = False
        try:
            raw = base64.b64decode(args["image"], validate=True)
            cache_key = hashlib.sha256(
                raw
                + json.dumps(
                    [op, args.get("kind"), args.get("parameters", {})], sort_keys=True
                ).encode()
            ).hexdigest()
            if op == "vision" and cache_key in self.vision_cache:
                hit = True
                result = self.vision_cache[cache_key]
            else:
                result = perceive(self.query, op, args)
                if op == "vision":
                    if len(self.vision_cache) > 32:
                        self.vision_cache.clear()
                    self.vision_cache[cache_key] = result
            dump(folder / "result.json", result)
            return {
                **result,
                "_tool_receipt": {
                    "host_seconds": (time.monotonic_ns() - started) / 1e9,
                    "operation": op,
                    "cache_hit": hit,
                },
            }
        except Exception as error:
            dump(folder / "error.json", {"error_type": type(error).__name__})
            raise
        finally:
            dump(
                folder / "timing.json",
                {
                    "started_ns": started,
                    "finished_ns": time.monotonic_ns(),
                    "cache_hit": hit,
                    "role": role,
                },
            )
            with self.tool_lock:
                self.visual_inflight -= 1

    def close(self):
        self.budget.close(self.termination or "workflow_closed")
        self.proxy.close_admissions()
        try:
            self.proxy.wait_idle(self.query["request_timeout_seconds"] + 35)
        finally:
            if self.developer:
                self.developer.close()
            if self.model_bridge:
                self.model_bridge.close()
            self.tools.shutdown()
            self.tools.server_close()
            self.proxy.shutdown()
            self.proxy.server_close()
            shutil.rmtree(self.transport, ignore_errors=True)


def matches_recorded_exploration(source, model, expected):
    """Older warm bundles lack explicit settings; recover them from the recorded requests."""
    requests = sorted(source.glob("requests/r*/request.json"))
    if not requests:
        return False
    for path in requests:
        body = load(path)
        if model.startswith("deepseek"):
            thinking = {"enabled": True, "disabled": False}.get(body.get("thinking", {}).get("type"))
        else:
            thinking = body.get("chat_template_kwargs", {}).get("enable_thinking")
        if (
            body.get("model") != model
            or body.get("max_tokens") != expected["max_tokens"]
            or thinking is not expected["thinking"]
        ):
            return False
    return True


def prepare_initial(config, directory):
    """One shared exploration, before any branch starts. Its private scores stay outside workspaces."""
    from gpa.operation import Operation

    directory = Path(directory)
    exploration = None
    if config["initial"] == "warm":
        exploration = {
            "max_tokens": config.get("initial_max_tokens", 8192),
            "thinking": not (
                config["initial_model"].startswith("deepseek") and config.get("engineering_validation", False)
            ),
        }
    identity = hashlib.sha256(
        json.dumps(
            {
                k: config.get(k)
                for k in (
                    "task",
                    "url",
                    "backend",
                    "gameworld",
                    "input_profile",
                    "initial",
                    "initial_model",
                    "initial_seconds",
                    "initial_requests",
                    "random_seed",
                    "extended_inputs",
                )
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if config.get("initial_bundle"):
        source = Path(config["initial_bundle"])
        manifest = load(source / "bundle.json")
        if manifest["task_identity"] != identity:
            raise ValueError("initial bundle task identity mismatch")
        from gpa.evidence.files import digest

        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("initial bundle contains a symlink")
        actual = {
            str(p.relative_to(source)): digest(p)
            for p in source.rglob("*")
            if p.is_file() and p != source / "bundle.json"
        }
        if actual != manifest["files"]:
            raise ValueError("initial bundle checksum mismatch")
        if "exploration" in manifest:
            if manifest["exploration"] != exploration:
                raise ValueError("initial bundle exploration settings mismatch")
        elif exploration is not None and not matches_recorded_exploration(
            source, config["initial_model"], exploration
        ):
            raise ValueError("legacy initial bundle exploration settings do not match or cannot be verified")
        shutil.copytree(source, directory)
        return load(directory / "result.json")
    directory.mkdir()
    if exploration is not None:
        url, key = endpoint(config, "rtc")
        policy = ExplorePolicy(
            directory,
            base_url=url,
            key=key,
            model=config["initial_model"],
            task=config["task"],
            max_requests=config["initial_requests"],
            **exploration,
        )
        seconds = config["initial_seconds"]
    else:

        class Static:
            def can_request(self):
                return False

            def close(self):
                pass

        policy, seconds = Static(), 0.2
    result = Operation(config, seconds=seconds).run(policy, directory / "play")
    result["initial_requests"] = getattr(policy, "number", 0)
    dump(directory / "result.json", result)
    if result["error"]:
        raise RuntimeError("initial capture failed: " + str(result["error"]))
    from gpa.evidence.files import digest

    dump(
        directory / "bundle.json",
        {
            "task_identity": identity,
            "exploration": exploration,
            "files": {
                str(p.relative_to(directory)): digest(p)
                for p in directory.rglob("*")
                if p.is_file()
            },
        },
    )
    return result
