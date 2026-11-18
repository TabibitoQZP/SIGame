"""Translate policy actions to Minecraft keyboard and mouse input."""

import asyncio
import json
import time

from .browser import key_payload
from .gameworld import GameWorldAdapter

PROFILE = "minecraft_mouse_hold_v1"
LOOK_PIXELS = 80
HOLD_GRACE_SECONDS = 0.05
INSTRUCTIONS = """
Minecraft input adapter override (takes precedence over generic click wording):
WASD moves; space jumps; number keys select inventory. While pointer-locked,
arrow keys LOOK, not move: each right/left/up/down tick sends one relative mouse
movement of 80 pixels (about 9.2 degrees). Opposite directions cancel.
click(x,y) targets the crosshair regardless of x,y while pointer-locked.
Consecutive adjacent ticks containing plain click keep LEFT held continuously:
repeat click every tick to mine; wait or any no-click tick releases immediately.
A single tick is usually too short to mine; do not alternate click and wait.
(press(ctrl), click(x,y)) sends a RIGHT click to place/use, consuming Ctrl.
Movement/look and click may be combined. Other keys retain their normal meaning.
When pointer lock is absent (e.g. menus), clicks retain normalized coordinates and
are ordinary clicks. A fresh submit/reset uses the official task initial state and
requests browser pointer lock. Held input is released on gaps, errors and run end.
The mouse hold watchdog allows at most 50ms scheduling grace past a tick boundary;
it is not an extra decision or an extension of the run budget.
"""
CAPABILITIES = {
    **GameWorldAdapter.capabilities,
    "adapter": "gameworld_minecraft_realtime_v1",
    "input_profile": PROFILE,
    "input_instructions": INSTRUCTIONS,
    "click": "crosshair_left_hold_across_adjacent_click_ticks_when_pointer_locked",
    "right_click": "ctrl+click_consumes_ctrl",
    "look": "arrow_keys_to_relative_mouse_80_pixels_per_tick",
    "mouse_hold_watchdog_grace_ms": 50,
    "relative_mouse_transport": "browser_MouseEvent_movementXY_after_pointer_lock",
}


class MinecraftAdapter(GameWorldAdapter):
    capabilities = CAPABILITIES

    def __init__(self, url, *, evaluator=None, seed=42):
        super().__init__(url, evaluator=evaluator, seed=seed)
        self.input_profile = PROFILE
        self.pointer_locked = False
        self.hold_timer = None

    async def prepare_page(self):
        await super().prepare_page()

        def changed(source, locked):
            self.pointer_locked = bool(locked)
            self.emit("pointer_lock_changed", locked=self.pointer_locked)
            if not self.pointer_locked:
                self._take_releases("pointer_unlock")

        await self.page.expose_binding("__gpaPointerLockChanged", changed)
        await self.page.add_init_script("""document.addEventListener('pointerlockchange',()=>{
            window.__gpaPointerLockChanged(Boolean(document.pointerLockElement));
        });""")

    async def _request_pointer_lock(self):
        cdp = await self.page.context.new_cdp_session(self.page)
        try:
            value = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": "document.querySelector('canvas').requestPointerLock()",
                    "userGesture": True,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            )
            if value.get("exceptionDetails"):
                raise RuntimeError("minecraft_pointer_lock_failed")
            self.pointer_locked = await self.page.evaluate("Boolean(document.pointerLockElement)")
            if not self.pointer_locked:
                raise RuntimeError("minecraft_pointer_lock_missing")
            self.emit("pointer_lock_ready", epoch=self.epoch, transport="browser_input_mode")
        finally:
            await cdp.detach()

    async def initialize_page(self):
        await super().initialize_page()
        await self._request_pointer_lock()

    async def reset_page(self):
        await super().reset_page()
        await self._request_pointer_lock()

    def input_plan(self, action):
        keys = list(action.keys)
        right = action.click is not None and "ctrl" in keys
        if right:
            keys.remove("ctrl")
        dx = dy = 0
        if self.pointer_locked:
            dx = LOOK_PIXELS * (("right" in keys) - ("left" in keys))
            dy = LOOK_PIXELS * (("down" in keys) - ("up" in keys))
            keys = [k for k in keys if k not in {"up", "down", "left", "right"}]
        return {
            "keys": tuple(keys),
            "click": action.click,
            "button": "right" if right else "left",
            "look": (dx, dy),
            "hold": self.pointer_locked and action.click is not None and not right,
        }

    def _release_keys(self, reason):
        state, self.mouse_down = self.mouse_down, None
        tasks = super()._take_releases(reason)
        self.mouse_down = state
        return tasks

    def _take_releases(self, reason):
        if reason == "tick_expired" and self.mouse_down and self.mouse_down.get("hold"):
            return self._release_keys(reason)
        if self.hold_timer:
            self.hold_timer.cancel()
            self.hold_timer = None
        return super()._take_releases(reason)

    def dispatch(self, action, receipt):
        accepted = super().dispatch(action, receipt)
        if not accepted:
            self.release("dispatch_rejected")
        return accepted

    def _renew_hold(self, state, receipt):
        state["receipt"] = receipt
        if self.hold_timer:
            self.hold_timer.cancel()
        deadline = receipt["window_end_ns"] / 1e9 + HOLD_GRACE_SECONDS
        self.hold_timer = self.loop.call_later(
            max(0, deadline - time.monotonic()), self._take_releases, "mouse_lease_expired"
        )

    async def _send_look(self, dx, dy, receipt, tracker):
        if self.closing or time.monotonic_ns() >= receipt["window_end_ns"]:
            self._mark_issued(tracker, "look", False)
            return
        payload = {
            "bubbles": True,
            "clientX": 0,
            "clientY": 0,
            "movementX": dx,
            "movementY": dy,
            "buttons": 1 if self.mouse_down and self.mouse_down.get("hold") else 0,
        }
        expr = (
            "document.pointerLockElement && document.dispatchEvent(new MouseEvent('mousemove',"
            + json.dumps(payload)
            + "))"
        )
        self.emit(
            "relative_mouse_send",
            **receipt,
            delta=[dx, dy],
            transport="DOM_MouseEvent",
            sent_ns=time.monotonic_ns(),
        )
        self._mark_issued(tracker, "look", True)
        value = await self.input_cdp.send("Runtime.evaluate", {"expression": expr})
        if value.get("exceptionDetails"):
            raise RuntimeError("relative_mouse_event_failed")
        self.emit("relative_mouse_ack", **receipt, acknowledged_ns=time.monotonic_ns())

    async def _send_mouse_down(self, state, tracker):
        if not state.get("hold"):
            return await super()._send_mouse_down(state, tracker)
        receipt = state["receipt"]
        if (
            self.closing
            or self.mouse_down is not state
            or time.monotonic_ns() >= receipt["window_end_ns"]
        ):
            self._mark_issued(tracker, "click", False)
            return
        self.emit(
            "mouse_down_send",
            **receipt,
            x=state["x"],
            y=state["y"],
            button="left",
            modifiers=state["modifiers"],
            sent_ns=time.monotonic_ns(),
        )
        self._mark_issued(tracker, "click", True)
        await self.input_cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": state["x"],
                "y": state["y"],
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
                "modifiers": state["modifiers"],
            },
        )
        self.emit("mouse_down_ack", **receipt, acknowledged_ns=time.monotonic_ns())

    async def _dispatch(self, action, receipt):
        if action.kind == "reset":
            self._take_releases("reset")
            return await super()._dispatch(action, receipt)
        release_busy = True
        try:
            if self.release_timer:
                self.release_timer.cancel()
            plan = self.input_plan(action)
            previous = self.mouse_down
            valid = not self.closing and time.monotonic_ns() < receipt["window_end_ns"]
            continued = bool(
                valid
                and plan["hold"]
                and previous
                and previous.get("hold")
                and previous["receipt"]["epoch"] == receipt["epoch"]
                and previous["receipt"]["tick"] + 1 == receipt["tick"]
            )
            if continued:
                self._release_keys("next_tick")
            else:
                self._take_releases("next_tick")
            if not valid:
                self.emit(
                    "input_not_sent", **receipt, action=action.record(), reason="expired_or_closed"
                )
                return
            self.last_input = receipt
            self.emit(
                "input_send_started",
                **receipt,
                action=action.record(),
                send_started_ns=time.monotonic_ns(),
            )
            look = plan["look"] != (0, 0)
            tracker = {
                "receipt": receipt,
                "action": action,
                "expected": len(plan["keys"]) + (plan["click"] is not None) + look,
                "finished": 0,
                "issued": [],
            }
            tasks = []
            for key in sorted(plan["keys"], key=lambda k: k not in {"ctrl", "shift", "alt"}):
                self.keys.add(key)
                tasks.append(
                    self._spawn(
                        self._send_key_down(
                            key, key_payload(key, self.keys, True), receipt, tracker
                        )
                    )
                )
            if look:
                tasks.append(self._spawn(self._send_look(*plan["look"], receipt, tracker)))
            if continued:
                self._renew_hold(previous, receipt)
                self.emit(
                    "mouse_hold_renewed", **receipt, button="left", sent_ns=time.monotonic_ns()
                )
                self._mark_issued(tracker, "click_hold", True)
            elif plan["click"] is not None:
                state = {
                    "x": 0 if self.pointer_locked else min(1279, plan["click"][0] * 1280),
                    "y": 0 if self.pointer_locked else min(719, plan["click"][1] * 720),
                    "button": plan["button"],
                    "hold": plan["hold"],
                    "receipt": receipt,
                    "modifiers": sum(
                        bit
                        for key, bit in {"alt": 1, "ctrl": 2, "shift": 8}.items()
                        if key in self.keys
                    ),
                }
                self.mouse_down = state
                if state["hold"]:
                    self._renew_hold(state, receipt)
                tasks.append(self._spawn(self._send_mouse_down(state, tracker)))
            delay = max(0, (receipt["window_end_ns"] - time.monotonic_ns()) / 1e9)
            self.release_timer = self.loop.call_later(delay, self._take_releases, "tick_expired")
            with self.busy_lock:
                self.busy = False
            release_busy = False
            if tracker["expected"] == 0:
                self._mark_issued({**tracker, "expected": 1}, "wait", True)
            if tasks:
                await asyncio.gather(*tasks)
            self.emit(
                "input_acknowledged",
                **receipt,
                acknowledged_ns=time.monotonic_ns(),
                semantics="transport acknowledgements, not proof of game consumption",
            )
        except Exception as error:
            self._take_releases("input_error")
            self.error = type(error).__name__
            self.emit("input_error", **receipt, action=action.record(), error_type=self.error)
        finally:
            if release_busy:
                with self.busy_lock:
                    self.busy = False
