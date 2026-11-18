"""Manage GameWorld tasks with game-specific input profiles."""

import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import time

from gpa.tasks.gameworld import READ_STATE, state_fields
from gpa.tasks.scoring import replay
from gpa.tasks.lifecycle import initialize, reset
from gpa.vendor.gameworld.game_state_tracker import (
    PRESERVE_WEBGL_DRAWING_BUFFER_SCRIPT,
)
from .browser import BrowserAdapter


def task_contract(query):
    keys = ("game_id", "task_id", "evaluator_config", "protocol")
    return {key: query[key] for key in keys}


def task_url(url, query):
    suffix = query["game_url_suffix"]
    if not isinstance(suffix, str) or (suffix and not suffix.startswith(("?", "#"))):
        raise ValueError("invalid_gameworld_task_suffix")
    return url + suffix


INPUT_PROFILES = {
    "19_minesweeper": "ctrl_click_right_v1",
    "18_minecraft-clone-glm": "minecraft_mouse_hold_v1",
}


def input_profile(query):
    profile = query.get("input_profile")
    if profile is not None and INPUT_PROFILES.get(query["game_id"]) != profile:
        raise ValueError("gameworld_input_profile_mismatch")
    return profile


def input_instructions(query):
    profile = input_profile(query)
    if profile == "minecraft_mouse_hold_v1":
        from .minecraft import INSTRUCTIONS

        return INSTRUCTIONS
    if profile == "ctrl_click_right_v1":
        return (
            "\nInput adapter override: click(x,y) reveals a cell; "
            "(press(ctrl), click(x,y)) sends a real RIGHT click to flag it. "
            "The Ctrl modifier is consumed by the adapter and is not sent to the game. "
            "Coordinates remain normalized 0..1.\n"
        )
    return ""


def input_capabilities(query):
    if input_profile(query) == "minecraft_mouse_hold_v1":
        from .minecraft import CAPABILITIES

        return dict(CAPABILITIES)
    caps = dict(GameWorldAdapter.capabilities)
    if input_profile(query):
        caps.update(
            input_profile=input_profile(query), input_instructions=input_instructions(query)
        )
    return caps


def task_description(query):
    from gpa.rtc.context import task_text

    return (
        task_text(query["original_task_text"])
        + input_instructions(query)
        + "\n\nPlay from RGB with the documented generic keyboard/mouse interface. "
        "Every submit starts this task in its official initial state. A reset uses "
        "the official gameAPI.reset and keeps the same task URL/level. "
        "There are 60 real seconds per run and 200ms input ticks, without pauses "
        "for policy computation, model calls or vision services. Determine progress "
        "and whether to stop development from visible evidence. "
        "Private PG, official success and terminal-state feedback are unavailable."
    )


class GameWorldAdapter(BrowserAdapter):
    capabilities = {
        **BrowserAdapter.capabilities,
        "adapter": "gameworld_realtime_cdp_v1",
        "reset": "official_gameAPI_reset_same_task",
    }

    def __init__(self, url, *, evaluator=None, seed=42, input_profile=None):
        super().__init__(url, evaluator=evaluator)
        if evaluator is not None:
            evaluator.authorized = lambda: self.accept_frames and not self.closing
        if seed is not None and type(seed) is not int:
            raise ValueError("invalid_gameworld_seed")
        self.seed = seed
        if input_profile not in {None, "ctrl_click_right_v1"}:
            raise ValueError("invalid_input_profile")
        self.input_profile = input_profile

    def input_plan(self, action):
        plan = super().input_plan(action)
        if (
            self.input_profile == "ctrl_click_right_v1"
            and action.click is not None
            and "ctrl" in action.keys
        ):
            plan.update(keys=tuple(k for k in action.keys if k != "ctrl"), button="right")
        return plan

    async def prepare_page(self):
        await self.page.add_init_script(script=PRESERVE_WEBGL_DRAWING_BUFFER_SCRIPT)
        if self.seed is not None:
            script = (
                Path(__file__).parents[2]
                / "vendor/gameworld/browser_scripts/deterministic_random.js"
            ).read_text()
            await self.page.add_init_script(
                script=script.replace("__RANDOM_SEED__", json.dumps(self.seed))
            )

    async def initialize_page(self):
        await self.page.wait_for_function(
            "window.gameAPI && typeof window.gameAPI.getState === 'function'", timeout=15000
        )
        await initialize(self.page)
        self.emit(
            "official_task_initialized",
            epoch=self.epoch,
            seed=self.seed,
            score_feedback=False,
            pause=False,
        )

    async def reset_page(self):
        success = await asyncio.wait_for(reset(self.page), timeout=60)
        if success is not True:
            raise RuntimeError("official_gameworld_reset_failed")
        self.emit("official_task_reset", epoch=self.epoch, pause=False)


def replay_samples(rows, contract, cutoff_ns):
    sessions = OrderedDict()
    for row in rows:
        sessions.setdefault(row.get("epoch", 0), []).append(row)
    return replay(sessions.items(), contract, cutoff_ns)


class GameWorldEvaluator:
    def __init__(self, output, contract):
        self.output, self.contract = Path(output), contract
        self.samples = []
        self.paths = state_fields(contract)
        self.sample_timeout_seconds = 5 if contract["game_id"] == "06_captaincallisto" else 1
        self.authorized = lambda: True

    async def run(self, page, epoch):
        origin, index = time.monotonic(), 0
        path = self.output / "private_score/samples.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            while True:
                await asyncio.sleep(max(0, origin + index * 0.2 - time.monotonic()))
                before, session = time.monotonic_ns(), epoch()
                authorized = self.authorized()
                try:
                    value = await asyncio.wait_for(
                        page.evaluate(READ_STATE, self.paths), timeout=self.sample_timeout_seconds
                    )
                    if not isinstance(value, dict):
                        value = {"status": "invalid_sample"}
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    value = {"status": "sample_error", "error_type": type(error).__name__}
                after = time.monotonic_ns()
                value.update(
                    started_ns=before,
                    observed_ns=after,
                    epoch=session,
                    accepted=authorized and self.authorized() and session == epoch(),
                )
                self.samples.append(value)
                stream.write(json.dumps(value, allow_nan=False) + "\n")
                stream.flush()
                index = max(index + 1, int((time.monotonic() - origin) / 0.2) + 1)

    def summary(self, deadline_ns):
        return replay_samples(self.samples, self.contract, deadline_ns)
