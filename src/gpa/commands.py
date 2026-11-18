"""Prepare games, run benchmarks and summarize results."""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import urllib.request

from gpa.config import load_config
from gpa.evidence.files import dump
from gpa.execution.docker import Container, preflight
from gpa.execution.resources import ResourceSession, SESSION_ENV, atomic_json, MIB
from gpa.execution.services import active_session, game_service
from gpa.gameworld import locked, tasks, task_config


def enter_session(stack, output):
    session = ResourceSession.current(required=False) or active_session()
    if session is None:
        raise RuntimeError(
            "start the shared resource supervisor first: rgbgame services up"
        )
    elif not os.environ.get(SESSION_ENV):
        os.environ[SESSION_ENV] = str(session.path)
        stack.callback(os.environ.pop, SESSION_ENV, None)
    return session


def wait_browser(container):
    address = "http://" + container.port("9222/tcp")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while True:
        try:
            with opener.open(address + "/json/version", timeout=1) as response:
                if response.status == 200:
                    return address
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("browser readiness timed out")
            container.session.check()
            time.sleep(0.1)


async def check_pages(address, selected, output, session, rows=None):
    from playwright.async_api import async_playwright
    from gpa.tasks.gameworld import READ_STATE, state_fields
    from gpa.tasks.scoring import replay
    from gpa.tasks.lifecycle import initialize, reset as official_reset, task_url_identity
    from gpa.vendor.gameworld.game_state_tracker import (
        PRESERVE_WEBGL_DRAWING_BUFFER_SCRIPT,
    )

    rows = [] if rows is None else rows
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(address)
        for task in selected:
            session.check()
            started = time.monotonic()
            config = task_config({}, task)
            row = {"task": task, "game_id": config["gameworld"]["game_id"], "status": "ERROR"}
            context = await browser.new_context(viewport={"width": 1280, "height": 720})
            try:
                page = await context.new_page()
                await page.bring_to_front()
                await page.add_init_script(PRESERVE_WEBGL_DRAWING_BUFFER_SCRIPT)
                errors = []
                network_errors = []
                page.on("pageerror", lambda error: errors.append(str(error)[:1000]))
                page.on(
                    "requestfailed",
                    lambda request: network_errors.append(
                        {"url": request.url, "failure": request.failure}
                    ),
                )
                await page.goto(config["url"], wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_function(
                    "window.gameAPI && typeof window.gameAPI.getState === 'function'", timeout=30000
                )
                await initialize(page, timeout=60)
                contract = config["gameworld"]
                samples = []
                for reset in (False, True):
                    if reset and await official_reset(page) is not True:
                        raise RuntimeError("official_reset_failed")
                    for attempt in range(30):
                        await asyncio.sleep(0.3)
                        state = await asyncio.wait_for(
                            page.evaluate(READ_STATE, state_fields(contract)), 15
                        )
                        state.update(observed_ns=time.monotonic_ns(), accepted=True)
                        samples.append(state)
                        score = replay([(int(reset), [state])], contract, time.monotonic_ns())
                        if score["valid_samples"] == 1:
                            break
                        if attempt in {5, 15, 25}:
                            await page.keyboard.press("Enter")
                            await page.keyboard.press("Space")
                            await page.mouse.click(640, 360)
                    if score["valid_samples"] != 1:
                        dump(
                            output / (task + ".unresolved.json"),
                            {
                                "state": state,
                                "score": score,
                                "page_errors": errors,
                                "network_errors": network_errors,
                            },
                        )
                        raise RuntimeError(
                            "unresolved_scoring_contract:" + json.dumps(score["sample_counts"])
                        )
                    if task_url_identity(page.url) != task_url_identity(config["url"]):
                        raise RuntimeError("reset_changed_task_url")
                if task.endswith("_05"):
                    if contract["game_id"] == "06_captaincallisto":
                        await screencast_image(page, output / (task + ".jpg"))
                    else:
                        await page.screenshot(path=str(output / (task + ".png")), timeout=15000)
                row.update(
                    status="OK",
                    valid_before_reset=True,
                    valid_after_reset=True,
                    page_errors=errors,
                    input_profile=config.get("input_profile"),
                )
                dump(output / (task + ".private.json"), {"samples": samples, "score": score})
            except Exception:
                row["error"] = traceback.format_exc()
                row["page_errors"] = errors
                try:
                    await page.screenshot(path=str(output / (task + "-error.png")), timeout=3000)
                except Exception:
                    pass
            finally:
                await context.close()
            row["seconds"] = time.monotonic() - started
            rows.append(row)
            atomic_json(output / "checks.json", rows)
            print(task, row["status"], flush=True)
        await browser.close()
    return rows


async def screencast_image(page, path):
    """Use the actual realtime capture route for the slow WebGL compositor."""
    cdp = await page.context.new_cdp_session(page)
    ready = asyncio.get_running_loop().create_future()

    async def received(event):
        await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        if not ready.done():
            ready.set_result(base64.b64decode(event["data"]))

    cdp.on("Page.screencastFrame", received)
    try:
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 80,
                "maxWidth": 1280,
                "maxHeight": 720,
                "everyNthFrame": 1,
            },
        )
        path.write_bytes(await asyncio.wait_for(ready, 30))
    finally:
        await cdp.send("Page.stopScreencast")
        await cdp.detach()


def check_games(selected, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    selection = sorted(tasks()) if selected == "all" else selected.split(",")
    if not selection or any(task not in tasks() for task in selection):
        raise ValueError("unknown or empty task selection")
    preflight()
    with ExitStack() as stack:
        session = enter_session(stack, output)
        stack.enter_context(session.activity("probe"))
        game = game_service(session)
        rows, games = [], {}
        for task in selection:
            games.setdefault(task.split("_", 1)[0], []).append(task)
        for group in games.values():
            container = Container(
                session,
                None,
                "browser",
                "rgbgame-browser:0.1.0",
                ["/opt/venv/bin/python", "/opt/rgbgame/browser_server.py"],
                network=game["network"],
                ports=["9222/tcp"],
                memory=task_config({}, group[0])["browser_memory_mib"] * MIB,
            ).start()
            try:
                rows = asyncio.run(
                    check_pages(wait_browser(container), group, output, session, rows)
                )
            finally:
                container.close()
        from gpa.operation import Operation

        class InputProbe:
            reset_done = False

            def decide(self, frame, context, runtime):
                tick = context["tick"]
                if tick >= 12 and not self.reset_done:
                    self.reset_done = True
                    return ["(reset())"]
                if tick % 10 == 9:
                    return ["(press(ctrl),click(.5,.5))"]
                return ["(press(right),click(.5,.5))"]

            def close(self):
                pass

        probes = []
        for task in ("06_05", "18_05", "19_05"):
            if task not in selection:
                continue
            config = task_config(
                {"game_seconds": 6, "browser_network": game["network"], "_resource_group": None},
                task,
            )
            folder = output / (task + "-inputs")
            result = Operation(config).run(InputProbe(), folder)
            events = [
                json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()
            ]
            sent = [row for row in events if row.get("event") == "input_sent"]
            reset = any(row.get("event") == "reset_boundary" for row in events)
            passed = (
                not result["error"]
                and bool(sent)
                and reset
                and result["host_score"]["classification"] == "valid_score"
            )
            probes.append(
                {
                    "task": task,
                    "status": "OK" if passed else "ERROR",
                    "input_events": len(sent),
                    "reset": reset,
                    "result": result,
                }
            )
        dump(output / "input-checks.json", probes)
    value = {
        "status": "OK" if all(r["status"] == "OK" for r in rows + probes) else "ERROR",
        "checked": len(rows),
        "failed": [r["task"] for r in rows if r["status"] != "OK"],
        "game_lock": locked(),
        "model_calls": 0,
        "input_probes": [{"task": p["task"], "status": p["status"]} for p in probes],
    }
    dump(output / "result.json", value)
    return value


def report(root):
    root = Path(root)
    index = json.loads((root / "suite.json").read_text())
    rows = []
    for entry in index["rows"]:
        path = root / entry["id"] / "result.json"
        result = json.loads(path.read_text()) if path.exists() else {"status": "MISSING"}
        row = {
            "id": entry["id"],
            "task": entry["task"],
            "method": entry["method"],
            "status": result.get("status"),
            "fake": result.get("fake"),
            "termination": result.get("termination"),
        }
        if result.get("status") == "OK" and entry["config"].get("engineering_validation"):
            from gpa.acceptance import inspect_run

            row["acceptance"] = inspect_run(root / entry["id"], entry["config"])
            if row["acceptance"]["status"] != "OK":
                row["status"] = "ERROR"
        if entry["method"] == "rtc":
            row["score"] = result.get("host_score")
        else:
            rounds = list((root / entry["id"]).glob("**/private/round-*/result.json"))
            row["submissions"] = len(rounds)
            row["scores"] = [json.loads(p.read_text()).get("host_score") for p in sorted(rounds)]
        rows.append(row)
    value = {
        "status": "OK" if all(r["status"] == "OK" for r in rows) else "ERROR",
        "status_meaning": "Harness execution; model outcomes and game success are recorded separately.",
        "runs": rows,
        "total": len(rows),
    }
    dump(root / "report.json", value)
    lines = [
        "# Benchmark report",
        "",
        "Status describes harness execution, not achievement of the game objective. Model outcomes (including no submission, wait-only policies and missing model reviews) are preserved separately.",
        "",
        "| Run | Task | Method | Status | Submissions |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['id']} | {r['task']} | {r['method']} | {r['status']} | {r.get('submissions', '—')} |"
        for r in rows
    ]
    lines.append("")
    for row in rows:
        for outcome in row.get("acceptance", {}).get("model_outcomes", []):
            lines.append(f"- {row['id']}: {outcome}")
    (root / "report.md").write_text("\n".join(lines) + "\n")
    from gpa.evidence.reporting import write_tables

    write_tables(root, rows)
    return value


def benchmark(
    suite_path, output, *, coding_concurrency=2, model_concurrency=4, resume=False, only=None
):
    from gpa.cli import run

    if not 1 <= coding_concurrency <= 4 or not 1 <= model_concurrency <= 4:
        raise ValueError("concurrency must be 1..4")
    suite_path, output = Path(suite_path).resolve(), Path(output).resolve()
    suite = json.loads(suite_path.read_text())
    rows = []
    for i, entry in enumerate(suite["rows"]):
        config_path = (suite_path.parent / entry["config"]).resolve()
        config = load_config(config_path, task_id=entry["task"])
        if config["method"] != entry["method"]:
            raise ValueError("suite method disagrees with configuration")
        rows.append({**entry, "id": f"{i:02d}-{entry['method']}-{entry['task']}", "config": config})
    if not rows:
        raise ValueError("suite must explicitly list at least one row")
    selected = set(only.split(",")) if only else {row["id"] for row in rows}
    if not selected <= {row["id"] for row in rows}:
        raise ValueError("--only contains unknown suite run IDs")
    signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    if resume:
        previous = json.loads((output / "suite.json").read_text())
        if previous["signature"] != signature:
            raise ValueError("cannot resume a changed suite")
    else:
        output.mkdir(parents=True, exist_ok=False)
        dump(
            output / "suite.json", {"rows": rows, "signature": signature, "source": str(suite_path)}
        )
    preflight()
    with ExitStack() as stack:
        session = enter_session(stack, output)
        if session.config["model_concurrency"] != model_concurrency:
            raise ValueError("model concurrency differs from shared resource session")
        if session.config["output_limit"] > int(suite.get("output_token_limit", 512 * 1024)):
            raise ValueError("shared output budget exceeds suite limit")
        game_service(session)
        receipt = output / "resource-session.json"
        if resume:
            previous_session = json.loads(receipt.read_text())["session"]
            if previous_session != str(session.path):
                raise ValueError(
                    "resume must retain its original global usage ledger; restore the service session before resuming"
                )
        else:
            dump(receipt, {"session": str(session.path), "before": session.snapshot()})

        def execute(entry):
            target = output / entry["id"]
            if target.exists():
                saved = target / "result.json"
                if resume and saved.exists():
                    return json.loads(saved.read_text())
                raise FileExistsError("existing incomplete evidence: " + str(target))
            try:
                return run(entry["config"], target)
            except Exception:
                value = {
                    "status": "ERROR",
                    "method": entry["method"],
                    "error": traceback.format_exc(),
                }
                target.mkdir(exist_ok=True)
                dump(target / "result.json", value)
                return value

        with (
            ThreadPoolExecutor(max_workers=coding_concurrency) as coding,
            ThreadPoolExecutor(max_workers=1) as rtc,
        ):
            futures = {}
            for entry in rows:
                if entry["id"] not in selected:
                    continue
                pool = rtc if entry["method"] == "rtc" else coding
                futures[pool.submit(execute, entry)] = entry["id"]
            for future in as_completed(futures):
                value = future.result()
                print(futures[future], value.get("status"), flush=True)
        dump(output / "resource-usage.json", session.snapshot())
    return report(output)


def dispatch(args):
    if args.command == "gameworld":
        if args.game_command == "check":
            return check_games(args.tasks, args.output)
        from gpa.gameworld import prepare

        if args.lock and json.loads(args.lock.read_text()) != locked():
            raise ValueError("asset lock differs from packaged catalog")
        value = prepare(output=args.output, source=args.source)
        return {**value, "status": "OK"}
    if args.command == "services":
        from gpa.execution.services import up, down

        if args.service_command == "up":
            return up(
                resume_session=args.resume_session,
                output_limit=args.output_token_limit,
                model_concurrency=args.model_concurrency,
            )
        if args.service_command == "down":
            return down()
        session = active_session()
        return {
            "status": "OK",
            "session": str(session.path) if session else None,
            "resources": session.snapshot() if session else None,
        }
    if args.command == "benchmark":
        return benchmark(
            args.suite,
            args.output,
            coding_concurrency=args.coding_concurrency,
            model_concurrency=args.model_concurrency,
            resume=args.resume,
            only=args.only,
        )
    if args.command == "report":
        return report(args.input)
    raise ValueError("unknown command")
