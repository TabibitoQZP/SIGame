"""Capture browser frames and dispatch GameWorld input over CDP."""

import asyncio
import base64
import math
import threading
import time

from gpa.realtime.runtime import Frame
from gpa.realtime.liveness import watch_browser
from gpa.rtc.codec import KEYS

READ_SCORE = """() => {
 const getter = window.__RL_GET_SCORE__;
 if (typeof getter !== 'function') return {status:'missing'};
 let raw; try { raw=getter(); } catch(e) {return {status:'not_ready'};}
 if(raw==null)return {status:'not_ready'};
 const value=typeof raw==='number'?raw:
   typeof raw==='string'&&raw.trim()!==''?Number(raw):NaN;
 return Number.isFinite(value)?{status:'ok',value}:{status:'invalid'};
}"""


def capture_clock(metadata, received_ns, calibration):
    """CDP TimeSinceEpoch -> host monotonic; both processes share container clocks."""
    before, wall, after = calibration
    evidence = {
        "method": "same_container_realtime_to_monotonic",
        "host_mono_before_ns": before,
        "host_wall_ns": wall,
        "host_mono_after_ns": after,
        "uncertainty_ns": (after - before) // 2 + 1024,
    }
    timestamp = metadata.get("timestamp")
    if type(timestamp) not in {int, float} or not math.isfinite(timestamp) or timestamp <= 0:
        return None, {**evidence, "status": "receive_only_missing_or_invalid_timestamp"}
    captured = round(timestamp * 1e9) - (wall - (before + after) // 2)
    if captured < 0 or captured > received_ns:
        return None, {**evidence, "status": "receive_only_clock_mapping_not_causal"}
    return captured, {**evidence, "status": "mapped"}


def key_event(name):
    key = KEYS[name]
    if name in "abcdefghijklmnopqrstuvwxyz" and len(name) == 1:
        return {
            "key": name,
            "code": "Key" + name.upper(),
            "windowsVirtualKeyCode": ord(name.upper()),
            "nativeVirtualKeyCode": ord(name.upper()),
            "text": name,
        }
    if len(name) == 1 and name.isdigit():
        return {
            "key": name,
            "code": "Digit" + name,
            "windowsVirtualKeyCode": ord(name),
            "nativeVirtualKeyCode": ord(name),
            "text": name,
        }
    codes = {
        "up": 38,
        "down": 40,
        "left": 37,
        "right": 39,
        "space": 32,
        "enter": 13,
        "esc": 27,
        "shift": 16,
        "ctrl": 17,
        "alt": 18,
        "tab": 9,
        "backspace": 8,
        "delete": 46,
    }
    code = {"shift": "ShiftLeft", "ctrl": "ControlLeft", "alt": "AltLeft"}.get(name, key)
    return {
        "key": " " if name == "space" else key,
        "code": code,
        "windowsVirtualKeyCode": codes[name],
        "nativeVirtualKeyCode": codes[name],
        **({"text": " "} if name == "space" else {}),
    }


def key_payload(name, held, keydown):
    event = key_event(name)
    event["modifiers"] = sum(
        bit for key, bit in {"alt": 1, "ctrl": 2, "shift": 8}.items() if key in held
    )
    if "shift" in held and len(name) == 1:
        value = name.upper() if name.isalpha() else dict(zip("1234567890", "!@#$%^&*()"))[name]
        event.update(key=value, text=value)
    if not keydown or "ctrl" in held or "alt" in held:
        event.pop("text", None)
    return event


class BrowserAdapter:
    capabilities = {
        "adapter": "browser_cdp_stream_v1",
        "width": 1280,
        "height": 720,
        "coordinate_system": "normalized_0_1",
        "keys": list(KEYS),
        "coordinate_conversion": "x=min(width-1,x*width), y=min(height-1,y*height)",
        "actions": ["input", "wait", "reset"],
        "max_keys": 4,
        "supports_reset": True,
        "key_hold": "one_tick_then_release",
        "click": "left_press_then_immediate_release",
        "encoding": "jpeg",
        "jpeg_quality": 80,
        "device_scale_factor": 1,
        "timestamp_quality": "mapped_capture_when_available_else_receive_only",
    }

    def __init__(self, url, *, evaluator=None):
        self.url, self.evaluator = url, evaluator
        self.loop = None
        self.thread = None
        self.ready = threading.Event()
        self.busy_lock = threading.Lock()
        self.busy = False
        self.closing = False
        self.error = None
        self.epoch, self.sequence, self.generation = 0, 0, 0
        self.keys = set()
        self.mouse_down = None
        self.pending = set()
        self.receive = self.emit = None
        self.stream = None
        self.accept_frames = False
        self.last_input = None
        self.release_timer = None
        self.remote_cdp = None

    def start(self, receive_frame, emit):
        self.receive, self.emit = receive_frame, emit
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()

    def _thread(self):
        try:
            asyncio.run(self._main())
        except Exception as error:
            self.error = self.error or type(error).__name__
            self.emit("adapter_error", error_type=self.error)
        finally:
            self.ready.set()

    async def _main(self):
        from playwright.async_api import async_playwright

        self.loop = asyncio.get_running_loop()
        self.finished = asyncio.Event()
        self.input_lock = asyncio.Lock()
        async with async_playwright() as playwright:
            if not self.remote_cdp:
                raise RuntimeError("managed_docker_browser_required")
            browser = await playwright.chromium.connect_over_cdp(self.remote_cdp, timeout=20000)
            self.context = await browser.new_context(
                viewport={"width": 1280, "height": 720}, device_scale_factor=1
            )
            self.page = await self.context.new_page()
            watch_browser(browser, self.page, self)
            await self.page.bring_to_front()
            self.page.on("console", lambda msg: self.emit("browser_console", level=msg.type))
            self.page.on(
                "pageerror",
                lambda error: self.emit("browser_page_error", message=str(error)[:1200]),
            )
            await self.prepare_page()
            self.navigation_started_ns = time.monotonic_ns()
            self.emit("navigation_started", epoch=self.epoch, started_ns=self.navigation_started_ns)
            await self.page.goto(self.url, wait_until="domcontentloaded", timeout=20000)
            self.emit("navigation_dom_ready", epoch=self.epoch)
            await self.initialize_page()
            self.input_cdp = await self.context.new_cdp_session(self.page)
            await self._stream_start()
            self.emit(
                "browser_identity",
                version=browser.version,
                clock_mapping="same_container_realtime_to_monotonic_with_recorded_calibration",
                capture_clock="CDP timestamp optional; causal receive_only fallback",
            )
            self.ready.set()
            score_task = (
                asyncio.create_task(self.evaluator.run(self.page, lambda: self.epoch))
                if self.evaluator
                else None
            )
            try:
                await self.finished.wait()
            finally:
                self.accept_frames = False
                if score_task:
                    score_task.cancel()
                    await asyncio.gather(score_task, return_exceptions=True)
                self._take_releases("adapter_close")
                if self.pending:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*list(self.pending), return_exceptions=True), timeout=5
                        )
                    except asyncio.TimeoutError:
                        self.emit("input_acknowledgements_unknown_at_close")
                await asyncio.wait_for(browser.close(), timeout=5)

    async def prepare_page(self):
        """Optional device/task initialization, before the first navigation."""

    async def initialize_page(self):
        """Optional task initialization before any RGB frame reaches the policy."""

    async def reset_page(self):
        await self.page.reload(wait_until="domcontentloaded", timeout=15000)

    async def _stream_start(self):
        self.generation += 1
        generation = self.generation
        if self.stream:
            try:
                await self.stream.detach()
            except Exception:
                pass
        stream = await self.context.new_cdp_session(self.page)
        self.stream = stream
        epoch = self.epoch
        first_frame = True

        def receive(event):
            nonlocal first_frame
            observed = time.monotonic_ns()
            asyncio.create_task(
                stream.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            )
            if self.closing or not self.accept_frames or generation != self.generation:
                return
            self.sequence += 1
            before = time.monotonic_ns()
            wall = time.time_ns()
            after = time.monotonic_ns()
            captured, clock_map = capture_clock(
                event.get("metadata", {}), observed, (before, wall, after)
            )
            image = Frame(
                base64.b64decode(event["data"]),
                observed,
                epoch,
                self.sequence,
                captured,
                metadata={
                    "cdp": event.get("metadata", {}),
                    "clock_map": clock_map,
                    "cdp_ack_session_id": event["sessionId"],
                    "stream_generation": generation,
                },
            )
            if first_frame:
                first_frame = False
                self.emit(
                    "navigation_first_frame",
                    epoch=epoch,
                    received_ns=observed,
                    navigation_started_ns=self.navigation_started_ns,
                    navigation_to_first_frame_seconds=(observed - self.navigation_started_ns) / 1e9,
                )
            self.receive(image)

        stream.on("Page.screencastFrame", receive)
        self.accept_frames = True
        await stream.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": 80,
                "maxWidth": 1280,
                "maxHeight": 720,
                "everyNthFrame": 1,
            },
        )

    def dispatch(self, action, receipt):
        if not self.ready.is_set() or self.error or self.closing or self.loop is None:
            return False
        with self.busy_lock:
            if self.busy:
                return False
            self.busy = True
        asyncio.run_coroutine_threadsafe(self._dispatch(action, dict(receipt)), self.loop)
        return True

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    def _mark_issued(self, tracker, component, sent):
        tracker["finished"] += 1
        if sent:
            tracker["issued"].append(component)
        if tracker["finished"] != tracker["expected"]:
            return
        receipt, action = tracker["receipt"], tracker["action"]
        if tracker["expected"] and not tracker["issued"]:
            self.emit("input_not_sent", **receipt, action=action.record(), reason="missed_window")
        else:
            self.emit(
                "input_sent",
                **receipt,
                action=action.record(),
                issued_components=list(tracker["issued"]),
                partial=len(tracker["issued"]) != tracker["expected"],
                sent_ns=time.monotonic_ns(),
                sent_semantics=(
                    "wait window; no key/mouse command"
                    if action.kind == "wait"
                    else "CDP send invoked; acknowledgements and game consumption are separate"
                ),
            )

    async def _send_key_down(self, key, event, receipt, tracker):
        now = time.monotonic_ns()
        if self.closing or now >= receipt["window_end_ns"]:
            if self.last_input is receipt:
                self.keys.discard(key)
            self.emit(
                "input_component_not_sent",
                **receipt,
                component="key:" + key,
                reason="expired_or_closed",
            )
            self._mark_issued(tracker, "key:" + key, False)
            return
        self.emit("key_down_send", key=key, cdp_event=event, **receipt, sent_ns=now)
        self._mark_issued(tracker, "key:" + key, True)
        try:
            await self.input_cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **event})
            self.emit("key_down_ack", key=key, **receipt, acknowledged_ns=time.monotonic_ns())
        except asyncio.CancelledError:
            self.emit("key_down_ack_unknown", key=key, **receipt, reason="adapter_closed")
            raise

    def input_plan(self, action):
        """Translate public input without exposing any game observation."""
        return {"keys": action.keys, "click": action.click, "button": "left"}

    async def _send_mouse_down(self, state, tracker):
        receipt = state["receipt"]
        now = time.monotonic_ns()
        if self.closing or now >= receipt["window_end_ns"]:
            if self.mouse_down is state:
                self.mouse_down = None
            self.emit(
                "input_component_not_sent", **receipt, component="click", reason="expired_or_closed"
            )
            self._mark_issued(tracker, "click", False)
            return
        x, y, modifiers = state["x"], state["y"], state["modifiers"]
        self.emit(
            "mouse_down_send",
            x=x,
            y=y,
            button=state.get("button", "left"),
            modifiers=modifiers,
            **receipt,
            sent_ns=now,
        )
        self._mark_issued(tracker, "click", True)
        try:
            await self.input_cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": state.get("button", "left"),
                    "buttons": 2 if state.get("button") == "right" else 1,
                    "clickCount": 1,
                    "modifiers": modifiers,
                },
            )
            self.emit("mouse_down_ack", **receipt, acknowledged_ns=time.monotonic_ns())
            if self.mouse_down is state:
                self.mouse_down = None
                await self._send_mouse_up(state, "click_completed")
        except asyncio.CancelledError:
            self.emit("mouse_down_ack_unknown", **receipt, reason="adapter_closed")
            raise

    async def _dispatch(self, action, receipt):
        release_busy = True
        try:
            if self.release_timer:
                self.release_timer.cancel()
            self._take_releases("next_tick")
            now = time.monotonic_ns()
            if self.closing or now >= receipt["window_end_ns"]:
                self.emit(
                    "input_not_sent",
                    **receipt,
                    action=action.record(),
                    reason="closed" if self.closing else "missed_window",
                )
                return
            if action.kind == "reset":
                self.accept_frames = False
                self.epoch = receipt["epoch"]
                self.emit(
                    "input_sent",
                    **receipt,
                    action=action.record(),
                    sent_ns=now,
                    sent_semantics="task reset requested; completion is separate",
                )
                self.navigation_started_ns = time.monotonic_ns()
                self.emit(
                    "navigation_started", epoch=self.epoch, started_ns=self.navigation_started_ns
                )
                await asyncio.sleep(0)
                await self.reset_page()
                self.emit("navigation_dom_ready", epoch=self.epoch)
                await self._stream_start()
                return
            plan = self.input_plan(action)
            self.last_input = receipt
            self.emit("input_send_started", **receipt, action=action.record(), send_started_ns=now)
            tracker = {
                "receipt": receipt,
                "action": action,
                "expected": len(plan["keys"]) + (plan["click"] is not None),
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
            if plan["click"] is not None:
                state = {
                    "x": min(1279, plan["click"][0] * 1280),
                    "y": min(719, plan["click"][1] * 720),
                    "button": plan["button"],
                    "modifiers": sum(
                        bit
                        for key, bit in {"alt": 1, "ctrl": 2, "shift": 8}.items()
                        if key in self.keys
                    ),
                    "receipt": receipt,
                }
                self.mouse_down = state
                tasks.append(self._spawn(self._send_mouse_down(state, tracker)))
            delay = max(0, (receipt["window_end_ns"] - time.monotonic_ns()) / 1e9)
            self.release_timer = self.loop.call_later(delay, self._take_releases, "tick_expired")
            with self.busy_lock:
                self.busy = False
            release_busy = False
            if not tasks:
                self._mark_issued({**tracker, "expected": 1}, "wait", True)
            else:
                await asyncio.gather(*tasks)
                self.emit(
                    "input_acknowledged",
                    **receipt,
                    acknowledged_ns=time.monotonic_ns(),
                    semantics="CDP acknowledgements, not proof of game consumption",
                )
        except Exception as error:
            if self.closing:
                self.emit("input_cancelled_at_close", **receipt, error_type=type(error).__name__)
                return
            self.error = type(error).__name__
            self.emit("input_error", **receipt, action=action.record(), error_type=self.error)
        finally:
            if release_busy:
                with self.busy_lock:
                    self.busy = False

    def _take_releases(self, reason):
        keys, self.keys = list(self.keys), set()
        receipt = self.last_input
        remaining = set(keys)
        releases = []
        for key in sorted(keys, key=lambda k: k in {"ctrl", "shift", "alt"}):
            remaining.discard(key)
            releases.append(
                self._spawn(
                    self._send_key_up(key, key_payload(key, remaining, False), reason, receipt)
                )
            )
        state, self.mouse_down = self.mouse_down, None
        if state is not None:
            if not isinstance(state, dict):
                state = {"x": state[0], "y": state[1], "modifiers": state[2], "receipt": receipt}
            releases.append(self._spawn(self._send_mouse_up(state, reason)))
        return releases

    async def _send_key_up(self, key, event, reason, receipt):
        started = time.monotonic_ns()
        self.emit(
            "key_up_send",
            key=key,
            reason=reason,
            sent_ns=started,
            input_receipt=receipt,
            cdp_event=event,
        )
        try:
            await self.input_cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **event})
            self.emit(
                "input_released",
                key=key,
                reason=reason,
                send_started_ns=started,
                acknowledged_ns=time.monotonic_ns(),
                input_receipt=receipt,
                cdp_event=event,
            )
        except Exception as error:
            self.emit(
                "input_release_error", key=key, reason=reason, error_type=type(error).__name__
            )
            if not self.closing:
                self.error = type(error).__name__
                self.emit("input_error", error_type=self.error, phase="key_release")

    async def _send_mouse_up(self, state, reason):
        x, y, modifiers = state["x"], state["y"], state["modifiers"]
        started = time.monotonic_ns()
        self.emit(
            "mouse_release_send",
            x=x,
            y=y,
            button=state.get("button", "left"),
            reason=reason,
            sent_ns=started,
            input_receipt=state["receipt"],
        )
        try:
            await self.input_cdp.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": state.get("button", "left"),
                    "buttons": 0,
                    "clickCount": 1,
                    "modifiers": modifiers,
                },
            )
            self.emit(
                "mouse_release_ack",
                x=x,
                y=y,
                reason=reason,
                send_started_ns=started,
                acknowledged_ns=time.monotonic_ns(),
                input_receipt=state["receipt"],
            )
        except Exception as error:
            self.emit("mouse_release_error", reason=reason, error_type=type(error).__name__)
            if not self.closing:
                self.error = type(error).__name__
                self.emit("input_error", error_type=self.error, phase="mouse_release")

    async def _release_locked(self, reason):
        tasks = self._take_releases(reason)
        if tasks:
            await asyncio.gather(*tasks)

    async def _release(self, reason):
        if hasattr(self, "input_cdp"):
            await self._release_locked(reason)

    def release(self, reason):
        if reason == "run_end":
            self.closing = True
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._take_releases, reason)

    def close(self):
        self.closing = True
        if self.loop and self.loop.is_running() and hasattr(self, "finished"):
            self.loop.call_soon_threadsafe(self.finished.set)
        if self.thread:
            self.thread.join(timeout=20)
        if self.thread and self.thread.is_alive():
            raise TimeoutError("browser_adapter_close_timeout")
        if self.error:
            raise RuntimeError("browser_adapter_error:" + self.error)
