"""Record Messages traffic and account for output tokens."""

import base64
from contextlib import ExitStack
import http.server
import io
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from gpa.coding.claude_game_budget import BudgetClosed, ResponseUsage
from gpa.evidence.files import dump
from gpa.coding.realtime_http import Deadline


def lift_message_system(body):
    """Keep SDK notices in Anthropic's top-level system field, preserving their content."""
    messages = body.get("messages", [])
    indices = [i for i, message in enumerate(messages) if message.get("role") == "system"]
    if not indices:
        return []
    system = body.get("system", [])
    system = [{"type": "text", "text": system}] if isinstance(system, str) else list(system)
    for i in indices:
        content = messages[i]["content"]
        system.extend([{"type": "text", "text": content}] if isinstance(content, str) else content)
    body["system"] = system
    body["messages"] = [message for message in messages if message.get("role") != "system"]
    return indices


def normalize_images(value, path=""):
    """Convert inline non-RGB images without changing existing RGB image bytes."""
    changes = []
    if isinstance(value, list):
        for i, item in enumerate(value):
            changes.extend(normalize_images(item, path + "/" + str(i)))
    elif isinstance(value, dict):
        source = value.get("source")
        if (
            value.get("type") == "image"
            and isinstance(source, dict)
            and source.get("type") == "base64"
        ):
            from PIL import Image

            with Image.open(io.BytesIO(base64.b64decode(source["data"]))) as image:
                if image.mode != "RGB":
                    original_mode = image.mode
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="PNG")
                    source.update(
                        media_type="image/png",
                        data=base64.b64encode(output.getvalue()).decode("ascii"),
                    )
                    changes.append({"path": path, "from_mode": original_mode, "to_mode": "RGB"})
        else:
            for key, item in value.items():
                changes.extend(normalize_images(item, path + "/" + str(key)))
    return changes


class ObservedUsage:
    def __init__(self, reminder_tokens, directory, *, enforce=False, minimum_request_tokens=1):
        self.limit, self.directory = reminder_tokens, Path(directory)
        if type(self.limit) is not int or self.limit < 1:
            raise ValueError("invalid_output_budget")
        self.enforce = enforce
        if type(minimum_request_tokens) is not int or minimum_request_tokens < 1:
            raise ValueError("invalid_minimum_request_tokens")
        self.minimum_request_tokens = minimum_request_tokens
        self.accounting_error = None
        self.lock = threading.Condition(threading.RLock())
        self.rows, self.pending, self.baseline = {}, {}, 0
        self.reason = None

    def snapshot(self):
        with self.lock:
            known = [
                r["output_tokens"]
                for r in self.rows.values()
                if type(r.get("output_tokens")) is int
                and r["output_tokens"] >= 0
                and (not self.enforce or r["usage_complete"])
            ]
            used = sum(known)
            reserved = sum(
                max(
                    0,
                    p.get("reserved", 0)
                    - (
                        self.rows.get(i, {}).get("output_tokens", 0)
                        if self.rows.get(i, {}).get("usage_complete")
                        else 0
                    ),
                )
                for i, p in self.pending.items()
            )
            return {
                "accounting_mode": "hard_cumulative" if self.enforce else "observe_only",
                "limit_is_advisory": not self.enforce,
                "reserved": reserved,
                "available": max(0, self.limit - used - reserved),
                "limit": self.limit,
                "used": used,
                "remaining": max(0, self.limit - used),
                "since_previous_submit": used - self.baseline,
                "unknown_or_incomplete_requests": sum(
                    not r["usage_complete"] for r in self.rows.values()
                ),
                "active_requests": len(self.pending),
                "accounting_error": self.accounting_error,
                "minimum_request_tokens": self.minimum_request_tokens,
                "unspendable_tail": max(0, self.limit - used)
                if 0 < self.limit - used < self.minimum_request_tokens
                else 0,
                "stop_reason": self.reason,
            }

    def persist(self):
        with self.lock:
            dump(
                self.directory / "budget.json",
                {
                    **self.snapshot(),
                    "requests": list(self.rows.values()),
                    "pending": dict(self.pending),
                },
            )

    def begin(self, request_id, requested=32768):
        with self.lock:
            if type(requested) is not int or requested < 1:
                raise ValueError("invalid_request_output_limit")
            if self.enforce:
                while True:
                    if self.reason:
                        raise BudgetClosed(self.reason)
                    state = self.snapshot()
                    if state["remaining"] < self.minimum_request_tokens:
                        self.reason = "output_token_budget"
                        self.persist()
                        raise BudgetClosed(self.reason)
                    available = state["available"]
                    if available >= self.minimum_request_tokens:
                        break
                    self.lock.wait(timeout=0.2)
                requested = min(max(requested, self.minimum_request_tokens), available)
            self.pending[request_id] = {
                "started": time.time(),
                "reserved": requested if self.enforce else 0,
            }
            self.persist()
            return requested

    def observe(self, request_id, usage, complete, submitted_rounds):
        with self.lock:
            n = usage.get("output_tokens") if isinstance(usage, dict) else None
            self.rows[request_id] = {
                "request_id": request_id,
                "round": submitted_rounds + 1,
                "output_tokens": n,
                "usage": usage,
                "usage_complete": bool(complete and type(n) is int and n >= 0),
            }
            if self.enforce:
                successful = self.rows[request_id]["usage_complete"]
                self.rows[request_id].update(
                    reserved=self.pending.get(request_id, {}).get("reserved", 0),
                    successful=successful,
                    charged_output_tokens=n if successful else 0,
                )
                if successful and n > self.rows[request_id]["reserved"]:
                    self.accounting_error = "backend_exceeded_reserved_output_tokens:" + request_id
                    self.reason = "output_cap_violation"
            if self.enforce and self.limit - self.snapshot()["used"] < self.minimum_request_tokens:
                self.reason = self.reason or "output_token_budget"
            self.lock.notify_all()

    def finish(self, request_id):
        with self.lock:
            self.pending.pop(request_id, None)
            self.persist()
            self.lock.notify_all()

    def mark_submit(self):
        with self.lock:
            value = self.snapshot()
            self.baseline = value["used"]
            self.persist()
            return value

    def close(self, reason):
        with self.lock:
            self.reason = self.reason or reason
            self.persist()
            self.lock.notify_all()


def validate_capabilities(body, expected_model=None):
    if expected_model is not None and body.get("model") != expected_model:
        raise ValueError("model_not_allowed")
    for tool in body.get("tools", []):
        if tool.get("type") not in (None, "custom"):
            raise ValueError("server_tools_not_allowed")
        if tool.get("name") in {
            "deepseek_web_search",
            "deepseek_read_url",
            "WebSearch",
            "WebFetch",
        }:
            raise ValueError("web_tools_not_allowed")

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "image" and value.get("source", {}).get("type") != "base64":
                raise ValueError("external_image_sources_not_allowed")
            if value.get("type") == "document":
                raise ValueError("document_sources_not_allowed")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(body.get("messages", []))


def start_proxy(
    directory,
    gateway,
    key,
    token,
    *,
    budget,
    status,
    request_seconds=1800,
    expected_model=None,
    engineering_profile=False,
):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock, sequence = threading.Lock(), [0]
    transport_slot = threading.Lock()
    closing = threading.Event()

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def do_POST(self):
            if (
                self.headers.get("Authorization") != "Bearer " + token
                and self.headers.get("X-Api-Key") != token
            ):
                self.send_error(401)
                return
            path = self.path.split("?", 1)[0]
            if path not in {"/v1/messages", "/v1/messages/count_tokens"}:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64 * 1024 * 1024:
                self.send_error(413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.send_error(400)
                return
            with lock:
                sequence[0] += 1
                request_id = "%06d" % sequence[0]
            prefix = directory / request_id
            Path(str(prefix) + ".request.json").write_bytes(raw)
            meta = {
                "path": path,
                "host_status": status(),
                "received_ns": time.monotonic_ns(),
                "accounting_mode": budget.snapshot()["accounting_mode"],
                "upstream_attempts": 0,
            }
            counted = path == "/v1/messages"
            tracker, sent = None, False
            deadline, acquired = None, False
            stack, ticket = ExitStack(), None
            try:
                while not transport_slot.acquire(timeout=0.2):
                    if closing.is_set() or budget.snapshot()["stop_reason"]:
                        raise BudgetClosed("request_admission_closed")
                acquired = True
                if closing.is_set():
                    raise BudgetClosed("proxy_closed")
                meta["transport_acquired_ns"] = time.monotonic_ns()
                meta["transport_inflight_limit"] = 1
                body = json.loads(raw)
                changed = False
                if counted:
                    requested = body.get("max_tokens", 32768)
                    cap = budget.begin(request_id, requested)
                    if budget.enforce:
                        body["max_tokens"] = cap
                        changed = True
                        meta["output_cap"] = {"requested": requested, "forwarded": cap}
                        thinking = body.get("thinking", {})
                        if (
                            thinking.get("type") == "enabled"
                            and thinking.get("budget_tokens", 0) >= cap
                        ):
                            body["thinking"] = (
                                {**thinking, "budget_tokens": cap - 1}
                                if cap > 1024
                                else {"type": "disabled"}
                            )
                if engineering_profile and meta["host_status"].get("phase") == "review":
                    original_thinking = body.get("thinking")
                    body["thinking"] = {"type": "disabled"}
                    if isinstance(body.get("output_config"), dict):
                        body["output_config"].pop("effort", None)
                    meta.setdefault("compatibility_changes", {})["engineering_review_thinking"] = {
                        "original": original_thinking,
                        "forwarded": "disabled",
                        "reason": "reserve the configured review allowance for a complete client tool call",
                    }
                    changed = True
                if body.get("thinking", {}).get("display") == "updates":
                    body["thinking"]["display"] = "summarized"
                    meta["compatibility_changes"] = {"thinking.display": ["updates", "summarized"]}
                lifted = lift_message_system(body)
                if lifted:
                    meta.setdefault("compatibility_changes", {})["message_system_to_top_level"] = (
                        lifted
                    )
                image_changes = normalize_images(body)
                validate_capabilities(body, expected_model)
                for tool in body.get("tools", []):
                    if tool.get("type") == "custom":
                        tool.pop("type")
                        changed = True
                        meta.setdefault("compatibility_changes", {})["custom_tool_type_removed"] = (
                            True
                        )
                if image_changes:
                    meta["image_conversions"] = image_changes
                if changed or image_changes or meta.get("compatibility_changes"):
                    raw = json.dumps(body).encode()
                Path(str(prefix) + ".forwarded.json").write_bytes(raw)
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower()
                    not in {
                        "authorization",
                        "x-api-key",
                        "host",
                        "content-length",
                        "connection",
                        "accept-encoding",
                    }
                }
                headers.update(
                    Authorization="Bearer " + key,
                    **{"X-Api-Key": key, "Accept-Encoding": "identity"},
                )
                request = urllib.request.Request(
                    gateway.rstrip("/") + self.path, data=raw, headers=headers
                )
                if counted:
                    from gpa.execution.resources import ResourceSession

                    session = ResourceSession.current(required=False)
                    if session:
                        try:
                            ticket = stack.enter_context(
                                session.model_request(
                                    body["max_tokens"],
                                    timeout=request_seconds,
                                    cancelled=closing.is_set,
                                )
                            )
                        except RuntimeError as error:
                            budget.close("global_admission_closed")
                            raise BudgetClosed(str(error)) from error
                deadline = Deadline(
                    request_seconds, lambda: budget.close("upstream_absolute_deadline")
                )
                opener = deadline.opener()
                meta["upstream_attempts"] = 1
                try:
                    response = opener.open(request, timeout=min(600, request_seconds))
                except urllib.error.HTTPError as error:
                    response = error
                ctype = response.headers.get("Content-Type", "application/json")
                meta["status"] = response.status
                tracker = ResponseUsage("text/event-stream" in ctype.lower())
                self.close_connection = True
                with response, Path(str(prefix) + ".response.raw").open("wb") as out:
                    disconnected = False
                    sent = True
                    try:
                        self.send_response(response.status)
                        self.send_header("Content-Type", ctype)
                        for name in ("Retry-After", "Retry-After-Ms", "Request-Id", "X-Request-Id"):
                            if response.headers.get(name):
                                self.send_header(name, response.headers[name])
                        self.send_header("Connection", "close")
                        self.end_headers()
                    except OSError:
                        disconnected = True
                        meta.update(client_disconnected=True, client_disconnect_stage="headers")
                    while chunk := response.read1(65536):
                        if deadline.expired:
                            raise TimeoutError("absolute_upstream_deadline")
                        out.write(chunk)
                        out.flush()
                        tracker.feed(chunk)
                        if counted and tracker.complete:
                            budget.observe(
                                request_id,
                                tracker.usage,
                                response.status == 200 and not tracker.protocol_error,
                                meta["host_status"]["submitted_rounds"],
                            )
                        if not disconnected:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except OSError:
                                disconnected = True
                                meta["client_disconnected"] = True
                                meta["client_disconnect_stage"] = "body"
            except BudgetClosed as error:
                meta.update(
                    error="BudgetClosed",
                    budget_stop_reason=str(error),
                    status=402,
                    host_rejected=True,
                )
                payload = json.dumps(
                    {"type": "error", "error": {"type": "budget_exhausted", "message": str(error)}}
                ).encode()
                Path(str(prefix) + ".response.raw").write_bytes(payload)
                self.close_connection = True
                try:
                    self.send_response(402)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass
            except Exception as error:
                meta["error"] = type(error).__name__
                if not sent:
                    self.close_connection = True
                    try:
                        self.send_error(
                            503, "Upstream request failed; retry is owned by Claude Code"
                        )
                    except OSError:
                        pass
            finally:
                try:
                    if deadline:
                        deadline.finish()
                        meta["absolute_deadline_seconds"] = request_seconds
                        meta["absolute_deadline_expired"] = deadline.expired
                        if deadline.expired:
                            meta["error"] = "absolute_upstream_deadline"
                            meta["remote_compute_cancellation_confirmed"] = False
                    if tracker:
                        tracker.finish()
                    meta.update(
                        usage=tracker.usage if tracker else None,
                        usage_complete=bool(
                            tracker
                            and tracker.complete
                            and not tracker.protocol_error
                            and meta.get("status") == 200
                            and not (deadline and deadline.expired)
                        ),
                        finished_ns=time.monotonic_ns(),
                    )
                    if counted and budget.enforce:
                        n = (meta["usage"] or {}).get("output_tokens")
                        meta["charged_output_tokens"] = (
                            n if meta["usage_complete"] and type(n) is int and n >= 0 else 0
                        )
                    if ticket is not None and meta["usage_complete"]:
                        ticket["output_tokens"] = (meta["usage"] or {}).get("output_tokens")
                    stack.close()
                    dump(Path(str(prefix) + ".meta.json"), meta)
                    if counted and request_id in budget.pending:
                        budget.observe(
                            request_id,
                            meta["usage"],
                            meta["usage_complete"],
                            meta["host_status"]["submitted_rounds"],
                        )
                        budget.finish(request_id)
                except Exception:
                    closing.set()
                    budget.close("request_evidence_cleanup_failure")
                    raise
                finally:
                    if acquired:
                        transport_slot.release()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    server.daemon_threads = False
    server.request_seconds = request_seconds
    server.close_admissions = closing.set

    def wait_idle(timeout):
        end = time.monotonic() + timeout
        while True:
            threads = getattr(server, "_threads", ())
            alive = isinstance(threads, list) and any(t.is_alive() for t in list(threads))
            if not alive:
                return
            if time.monotonic() >= end:
                raise TimeoutError("proxy_writers_not_idle")
            time.sleep(0.05)

    server.wait_idle = wait_idle
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
