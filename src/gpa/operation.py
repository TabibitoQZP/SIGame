"""Backend selection; each submit constructs and closes an independent game."""

from gpa.realtime.runtime import Runtime
from gpa.execution.docker import Container
from gpa.execution.resources import ResourceSession
from gpa.config import DEFAULT_BROWSER_MEMORY_MIB
import time
import urllib.request


class Operation:
    def __init__(self, config, *, seconds=None):
        self.config = config
        self.seconds = config["game_seconds"] if seconds is None else seconds
        from gpa.realtime.browser import BrowserAdapter

        self.capabilities = dict(BrowserAdapter.capabilities)
        if config["backend"] == "gameworld":
            if config.get("extended_inputs", False):
                from gpa.adapters.gameworld170.gameworld import input_capabilities

                self.capabilities = input_capabilities(
                    {**config["gameworld"], "input_profile": config.get("input_profile")}
                )
            else:
                from gpa.realtime.gameworld import GameWorldAdapter

                self.capabilities = dict(GameWorldAdapter.capabilities)
        if config["backend"] == "fake":
            self.capabilities["adapter"] = "synthetic_fixture_not_a_benchmark"

    def run(self, policy, output, *, prepaid=None):
        evaluator = None
        if self.config["backend"] == "fake":
            from gpa.testing.game import FakeGame

            adapter = FakeGame()
        elif self.config["backend"] == "gameworld":
            from gpa.realtime.gameworld import GameWorldAdapter, GameWorldEvaluator

            if self.config.get("extended_inputs", False):
                from gpa.adapters.gameworld170.gameworld import GameWorldAdapter, GameWorldEvaluator
            evaluator = GameWorldEvaluator(output, self.config["gameworld"])
            kwargs = {"evaluator": evaluator, "seed": self.config.get("random_seed", 42)}
            profile = self.config.get("input_profile")
            if profile == "minecraft_mouse_hold_v1":
                from gpa.adapters.gameworld170.minecraft import MinecraftAdapter

                adapter = MinecraftAdapter(self.config["url"], **kwargs)
            else:
                if profile:
                    kwargs["input_profile"] = profile
                adapter = GameWorldAdapter(self.config["url"], **kwargs)
        else:
            from gpa.realtime.browser import BrowserAdapter

            adapter = BrowserAdapter(self.config["url"])
        browser = None
        try:
            if self.config["backend"] != "fake":
                session = ResourceSession.current()
                browser = Container(
                    session,
                    self.config["_resource_group"],
                    "browser",
                    self.config.get("browser_image", "rgbgame-browser:0.1.0"),
                    ["/opt/venv/bin/python", "/opt/rgbgame/browser_server.py"],
                    network=self.config.get("browser_network", "bridge"),
                    ports=["9222/tcp"],
                    prepaid=prepaid,
                    memory=self.config.get("browser_memory_mib", DEFAULT_BROWSER_MEMORY_MIB) * 1024**2,
                ).start()
                adapter.remote_cdp = "http://" + browser.port("9222/tcp")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 30
                while True:
                    try:
                        with opener.open(
                            adapter.remote_cdp + "/json/version", timeout=1
                        ) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Docker browser did not become ready")
                        session.check()
                        time.sleep(0.1)
            runtime = Runtime(adapter, policy, output, ticks=int(self.seconds * 5))
            result = runtime.run()
        finally:
            if browser:
                browser.close()
        result["host_score"] = (
            evaluator.summary(result["deadline_ns"] or result["ended_ns"])
            if evaluator
            else {"raw_score": None, "classification": "evaluator_unavailable"}
        )
        return result
