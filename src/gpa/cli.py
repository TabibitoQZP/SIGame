"""Command-line interface for playing and evaluating games."""

import argparse
from contextlib import nullcontext
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import subprocess
import traceback
import time

from gpa.config import DEFAULT_BROWSER_MEMORY_MIB, endpoint, load_config
from gpa.evidence.files import dump, load
from gpa.operation import Operation


def rtc(config, root):
    from gpa.realtime.direct_model import ModelPolicy, rtc_timing

    url, key = endpoint(config, "rtc")
    chunk, lead = rtc_timing(config["response_floor_ms"])
    policy = ModelPolicy(
        root,
        base_url=url,
        key=key,
        model=config["model"],
        task=config["task"],
        chunk_ticks=chunk,
        lead_ticks=lead,
        game_seconds=config["game_seconds"],
        response_floor_ms=config["response_floor_ms"],
        max_requests=config.get("rtc_max_requests", 300),
        max_tokens=config.get("rtc_max_tokens", 8192),
        thinking=not config.get("engineering_validation", False),
    )
    result = Operation(config).run(policy, root / "play")
    result.update(
        method="rtc",
        status="ERROR" if result["error"] else "OK",
        model_requests=policy.number,
        fake=config["fake"],
        chunk_ticks=chunk,
        lead_ticks=lead,
    )
    dump(root / "result.json", result)
    return result


def single_agent(config, root):
    from gpa.coding.workflow import Workflow, prepare_initial

    prepare_initial(config, root / "shared-initial")
    workflow = None
    result = {"method": "single_agent", "status": "ERROR", "fake": config["fake"]}
    try:
        workflow = Workflow(root, config, Operation(config))
        workflow.import_initial(root / "shared-initial")
        workflow.develop()
        result.update(status="OK", termination=workflow.termination)
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if workflow:
            try:
                workflow.close()
            except Exception:
                result.update(status="ERROR", cleanup_error=traceback.format_exc())
            result.update(rounds=workflow.rounds, output_budget=workflow.budget.snapshot())
            if any(r.get("error_origin") == "host" for r in workflow.rounds):
                result["status"] = "ERROR"
        dump(root / "result.json", result)
    return result


def branch(root, name):
    from gpa.coding.team import BranchWorkflow, update

    config = load(root / "private/query.json")
    config.update(
        developer_output_tokens=config["branch_output_tokens"],
        development_rounds=config["branch_submissions"],
    )
    folder = root / "branches" / name
    folder.mkdir(parents=True)
    workflow = None
    result = {"branch": name, "status": "ERROR", "rounds": []}
    try:
        workflow = BranchWorkflow(folder, config, Operation(config), root, name)
        signal.signal(
            signal.SIGTERM, lambda *_: workflow.budget.close("team_infrastructure_shutdown")
        )
        workflow.execute(root / "private/initial")
        result.update(status="OK", termination=workflow.termination)
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if workflow:
            try:
                workflow.close()
            except Exception:
                result.update(status="ERROR", cleanup_error=traceback.format_exc())
            result.update(
                rounds=workflow.rounds,
                output_budget=workflow.budget.snapshot(),
                developer_session=getattr(workflow, "developer_session", None),
                development_seconds=workflow.development_seconds,
            )
            if any(r.get("error_origin") == "host" for r in workflow.rounds):
                result["status"] = "ERROR"
        dump(folder / "result.json", result)
        update(
            root,
            name,
            terminal=True,
            first_done=True,
            status=result["status"],
            termination=result.get("termination"),
        )
    return result


def multi_agent(config, root):
    from gpa.coding.team import run_team, seal_team
    from gpa.coding.workflow import prepare_initial

    (root / "private").mkdir(mode=0o700)
    dump(root / "private/query.json", config)
    prepare_initial(config, root / "private/initial")
    result = {"method": "multi_agent", "status": "OK", "fake": config["fake"]}
    try:
        run_team(root, config, config.get("url", "fake://game"))
        seal_team(root, config, result)
        if result.get("failed_branches"):
            result["status"] = "ERROR"
    except Exception:
        result.update(status="ERROR", error=traceback.format_exc())
    dump(root / "result.json", result)
    return result


def _run(config, output):
    root = Path(output).resolve()
    from gpa.execution.docker import preflight
    from gpa.execution.resources import ResourceSession, MIB
    from gpa.execution.services import active_session, configure_run

    preflight()
    root.mkdir(parents=True, exist_ok=False)
    dump(root / "config.json", config)
    from gpa.evidence.provenance import controller_provenance

    dump(
        root / "environment.json",
        {
            "python": sys.version,
            "platform": sys.platform,
            "fake": config["fake"],
            "controller": controller_provenance(),
            "random_seed": config.get("random_seed"),
            "random_seed_note": "Recorded configuration only; browser game RNG and provider sampling are not forced deterministic.",
        },
    )
    if config["fake"]:
        from gpa.testing.models import FakeModels

        context = FakeModels()
    else:
        context = nullcontext(None)
    with ExitStack() as stack:
        session = ResourceSession.current(required=False)
        if session is None and not config["fake"]:
            session = active_session()
        if session is None:
            if not config["fake"]:
                raise RuntimeError(
                    "start the shared resource supervisor first: rgbgame services up"
                )
            session = ResourceSession.create(root / "resources").start()
            stack.callback(session.close)
        elif not os.environ.get("RGBGAME_RESOURCE_SESSION"):
            os.environ["RGBGAME_RESOURCE_SESSION"] = str(session.path)
            stack.callback(os.environ.pop, "RGBGAME_RESOURCE_SESSION", None)
        stack.enter_context(session.activity("evaluation"))
        config = configure_run(session, config)
        dump(
            root / "services.json",
            {
                "resource_session": str(session.path),
                "game_commit": config.get("game_commit"),
                "browser_network": config.get("browser_network"),
            },
        )
        if "_resource_group" not in config:
            browser_memory = config.get("browser_memory_mib", DEFAULT_BROWSER_MEMORY_MIB) * MIB
            minimum = {
                "rtc": browser_memory,
                "single_agent": browser_memory + 2304 * MIB,
                "multi_agent": browser_memory + 6912 * MIB,
            }[config["method"]]
            group = stack.enter_context(
                session.group(minimum, timeout=config["resource_admission_timeout_seconds"])
            )
            config = {**config, "_resource_group": group}
        fixture = stack.enter_context(context)
        if fixture:
            config = {
                **config,
                "endpoints": {"rtc": {"url": fixture.url}, "anthropic": {"url": fixture.url}},
            }
        result = {"status": "ERROR", "method": config["method"], "fake": config["fake"]}
        try:
            result = {"rtc": rtc, "single_agent": single_agent, "multi_agent": multi_agent}[config["method"]](config, root)
        except Exception:
            result["error"] = traceback.format_exc()
            dump(root / "result.json", result)
        if fixture:
            dump(root / "fake_http_calls.json", fixture.calls)
    return result


def run(config, output):
    root = Path(output).resolve()
    existed, started = root.exists(), time.monotonic_ns()
    try:
        return _run(config, output)
    except FileExistsError:
        raise
    except Exception:
        root = Path(output).resolve()
        if not root.exists():
            raise
        path = root / "result.json"
        result = (
            load(path) if path.exists() else {"method": config["method"], "fake": config["fake"]}
        )
        result.update(status="ERROR", infrastructure_error=traceback.format_exc())
        dump(path, result)
        return result
    finally:
        if not existed and root.exists():
            ended = time.monotonic_ns()
            dump(
                root / "run-clock.json",
                {
                    "started_ns": started,
                    "ended_ns": ended,
                    "elapsed_seconds": (ended - started) / 1e9,
                },
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run", help="Run RTC, single-agent or multi-agent development from an explicit configuration")
    command.add_argument("--config", required=True)
    command.add_argument("--task", help="Packaged GameWorld task ID, such as 01_05")
    command.add_argument(
        "--output", required=True, help="New, short directory; never overwrite evidence"
    )
    command = sub.add_parser("branch", help=argparse.SUPPRESS)
    command.add_argument("--branch", choices=list("ABCD"), required=True)
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--url")
    command = sub.add_parser(
        "gameworld", help="Prepare pinned public assets or check tasks without model calls"
    )
    games = command.add_subparsers(dest="game_command", required=True)
    command = games.add_parser("prepare")
    command.add_argument("--lock", type=Path)
    command.add_argument("--output", type=Path)
    command.add_argument("--source", type=Path)
    command = games.add_parser("check")
    command.add_argument("--tasks", default="all", help="all or comma-separated task IDs")
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("services", help="Manage shared game services and resource monitoring")
    services = command.add_subparsers(dest="service_command", required=True)
    command = services.add_parser("up")
    command.add_argument(
        "--resume-session",
        type=Path,
        help="Recover an ended supervisor without resetting its usage ledger",
    )
    command.add_argument("--output-token-limit", type=int, default=512 * 1024)
    command.add_argument("--model-concurrency", type=int, default=4)
    services.add_parser("down")
    services.add_parser("status")
    command = sub.add_parser(
        "benchmark", help="Run only explicit suite rows, with global resource admission"
    )
    command.add_argument("--suite", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--coding-concurrency", type=int, default=2)
    command.add_argument("--model-concurrency", type=int, default=4)
    command.add_argument("--resume", action="store_true")
    command.add_argument(
        "--only", help="Comma-separated run IDs from the suite; retain the full batch ledger"
    )
    command = sub.add_parser("report", help="Summarize saved results without model calls")
    command.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    bounded = args.command in {"run", "benchmark", "branch"} or (
        args.command == "gameworld" and args.game_command == "check"
    )
    if bounded and not os.environ.get("RGBGAME_CONTROLLER_BOUNDED"):
        return subprocess.call(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--property=MemoryMax=1920M",
                "--property=MemorySwapMax=0",
                sys.executable,
                "-m",
                "gpa.cli",
                *sys.argv[1:],
            ],
            env={**os.environ, "RGBGAME_CONTROLLER_BOUNDED": "1"},
        )
    if args.command == "branch":
        result = branch(args.root, args.branch)
    elif args.command == "run":
        result = run(load_config(args.config, task_id=args.task), args.output)
        result = {k: result.get(k) for k in ("method", "status", "fake", "termination", "error")}
    else:
        from gpa.commands import dispatch

        result = dispatch(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] != "OK")


if __name__ == "__main__":
    raise SystemExit(main())
