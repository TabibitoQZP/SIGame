"""Host-only GameWorld initialization and continuous score evidence collection."""

import asyncio
import time

from gpa.evidence.files import append, dump

READ_STATE = """(paths) => {
  if (!window.gameAPI || typeof window.gameAPI.getState !== 'function')
    return {status: 'not_ready'};
  const state = window.gameAPI.getState();
  if (!state || typeof state !== 'object') return {status: 'invalid_state'};
  const result = {};
  for (const path of paths) {
    let value = state;
    const parts = path.split('.');
    for (const part of parts) value = value == null ? undefined : value[part];
    if (value === undefined) continue;
    let dst = result;
    for (const part of parts.slice(0, -1)) dst = dst[part] ??= {};
    dst[parts.at(-1)] = value;
  }
  return {status: 'ok', state: JSON.parse(JSON.stringify(result)), document_id: performance.timeOrigin};
}"""


def state_fields(contract):
    objective = contract["evaluator_config"]["objective"]
    return sorted(
        {
            "gameId",
            "gameTimeMs",
            "status",
            "terminal",
            "episodeId",
            *objective["reach"],
            *objective.get("require", {}),
            *contract["evaluator_config"].get("metrics_fields", []),
        }
    )


class GameWorldSampler:
    def __init__(self, contract, output, ledger):
        self.contract, self.output, self.ledger = contract, output, ledger
        self.paths = state_fields(contract)
        self.task = None
        self.page = self.folder = None

    async def initialize(self, page, attempt):
        await self.stop()
        self.page = page
        self.folder = self.output / "sessions" / f"session_{attempt:03d}" / "host_gameworld"
        self.folder.mkdir(parents=True, exist_ok=True)
        await page.wait_for_function(
            "window.gameAPI && typeof window.gameAPI.getState === 'function'", timeout=15000
        )
        value = await asyncio.wait_for(
            page.evaluate(
                "async () => typeof window.gameAPI.init === 'function' ? await window.gameAPI.init({}) : null"
            ),
            timeout=45,
        )
        dump(self.folder / "initialization.json", {"init": value})
        await self.sample()
        self.task = asyncio.create_task(self.poll())

    async def sample(self):
        if self.ledger.stopped.is_set():
            return
        started = time.monotonic_ns()
        try:
            value = await asyncio.wait_for(self.page.evaluate(READ_STATE, self.paths), timeout=4)
            if not isinstance(value, dict):
                value = {"status": "invalid_sample"}
        except Exception as exc:
            value = {"status": "sample_error", "error_type": type(exc).__name__}
        observed = time.monotonic_ns()
        append(
            self.folder / "samples.jsonl",
            {
                **value,
                "started_ns": started,
                "observed_ns": observed,
                "accepted": not self.ledger.stopped.is_set(),
            },
        )

    async def poll(self):
        while not self.ledger.stopped.is_set():
            await asyncio.sleep(0.1)
            await self.sample()

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
