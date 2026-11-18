"""Assemble SSE completions and collect usage reported by the server."""

import asyncio
import json
import time
from contextlib import nullcontext


class StreamCompletion:
    def __init__(self):
        self.metadata = {}
        self.choices = {}
        self.usage = None
        self.done = False
        self.error = None

    def add(self, chunk):
        if not isinstance(chunk, dict):
            raise ValueError("invalid_stream_chunk")
        if chunk.get("error"):
            self.error = "upstream_stream_error"
            if chunk.get("usage") is not None:
                self.usage = chunk["usage"]
            return
        for key in ("id", "model", "created", "system_fingerprint", "weight_version"):
            value = chunk.get(key)
            if value is not None:
                if key in {"id", "model"} and key in self.metadata and self.metadata[key] != value:
                    raise ValueError("stream_identity_changed")
                self.metadata[key] = value
        if chunk.get("usage") is not None:
            self.usage = chunk["usage"]
        for part in chunk.get("choices", []):
            if part.get("index") != 0:
                raise ValueError("unexpected_stream_choice")
            choice = self.choices.setdefault(
                0, {"message": {"role": "assistant", "content": ""}, "tools": {}, "finish": None}
            )
            if part.get("finish_reason") is not None:
                choice["finish"] = part["finish_reason"]
            delta = part.get("delta") or {}
            if delta.get("role") not in (None, "assistant"):
                raise ValueError("unexpected_stream_role")
            for key in ("content", "reasoning_content", "reasoning", "refusal"):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("invalid_stream_text")
                    choice["message"][key] = choice["message"].get(key, "") + value
            for tool in delta.get("tool_calls") or []:
                index = tool.get("index")
                if type(index) is not int or index < 0:
                    raise ValueError("invalid_stream_tool_index")
                target = choice["tools"].setdefault(
                    index, {"function": {"name": "", "arguments": ""}}
                )
                for key in ("id", "type"):
                    if tool.get(key) is not None:
                        if key in target and target[key] != tool[key]:
                            raise ValueError("stream_tool_identity_changed")
                        target[key] = tool[key]
                for key in ("name", "arguments"):
                    value = (tool.get("function") or {}).get(key)
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("invalid_stream_tool_delta")
                        target["function"][key] += value

    def response(self):
        choices = []
        for index, choice in self.choices.items():
            message = dict(choice["message"])
            if choice["tools"]:
                indices = sorted(choice["tools"])
                if indices != list(range(len(indices))):
                    self.error = self.error or "incomplete_stream_tools"
                message["tool_calls"] = [choice["tools"][i] for i in indices]
                if any(
                    not t.get("id") or t.get("type") != "function" or not t["function"]["name"]
                    for t in message["tool_calls"]
                ):
                    self.error = self.error or "incomplete_stream_tools"
            choices.append({"index": index, "message": message, "finish_reason": choice["finish"]})
        final_usage = (
            self.done
            and self.error in (None, "incomplete_stream_tools", "upstream_stream_error")
            and (
                self.error == "upstream_stream_error"
                or choices
                and all(c["finish_reason"] for c in choices)
            )
        )
        return {
            **self.metadata,
            "object": "chat.completion",
            "choices": choices,
            "usage": self.usage if final_usage else None,
            "partial_usage": self.usage if not final_usage else None,
            "_transport": {
                "mode": "sse",
                "assembled": True,
                "done": self.done,
                "error": self.error,
            },
        }


def collect_stream(response, journal=None):
    """Read a single-choice SSE response; retain incomplete evidence on disconnect."""
    result = StreamCompletion()
    context = journal.open("w") if journal is not None else nullcontext(None)
    try:
        with context as log:
            parts = []
            for line in response.iter_lines():
                if line.startswith(b"data:"):
                    parts.append(line[5:].lstrip().decode("utf-8"))
                if line or not parts:
                    continue
                data = "\n".join(parts)
                parts = []
                if data == "[DONE]":
                    event = {"observed_ns": time.monotonic_ns(), "done": True}
                else:
                    chunk = json.loads(data)
                    if isinstance(chunk, dict) and chunk.get("error"):
                        chunk = {
                            "error": {"type": "upstream_stream_error"},
                            "usage": chunk.get("usage"),
                        }
                    event = {"observed_ns": time.monotonic_ns(), "chunk": chunk}
                if log:
                    log.write(json.dumps(event, ensure_ascii=False) + "\n")
                    log.flush()
                if data == "[DONE]":
                    result.done = True
                    break
                result.add(chunk)
            if not result.done:
                result.error = "stream_ended_without_done"
    except Exception as error:
        result.error = type(error).__name__
        if journal is not None:
            with journal.open("a") as log:
                log.write(
                    json.dumps({"observed_ns": time.monotonic_ns(), "error": result.error}) + "\n"
                )
    finally:
        response.close()
    return result.response()


async def collect_async_stream(response, journal, result):
    """Cancellation preserves every received event and the partial assembly."""
    with journal.open("w") as log:
        parts = []
        try:
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    parts.append(line[5:].lstrip())
                if line or not parts:
                    continue
                data, parts = "\n".join(parts), []
                if data == "[DONE]":
                    event = {"observed_ns": time.monotonic_ns(), "done": True}
                else:
                    chunk = json.loads(data)
                    if isinstance(chunk, dict) and chunk.get("error"):
                        chunk = {
                            "error": {"type": "upstream_stream_error"},
                            "usage": chunk.get("usage"),
                        }
                    event = {"observed_ns": time.monotonic_ns(), "chunk": chunk}
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
                log.flush()
                if data == "[DONE]":
                    result.done = True
                    break
                result.add(chunk)
            if not result.done:
                result.error = "stream_ended_without_done"
        except (Exception, asyncio.CancelledError) as exc:
            result.error = type(exc).__name__
            log.write(
                json.dumps({"observed_ns": time.monotonic_ns(), "error": result.error}) + "\n"
            )
            log.flush()
            raise
