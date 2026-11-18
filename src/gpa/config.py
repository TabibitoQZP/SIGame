"""Explicit, portable run configuration. Credentials are environment references."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BROWSER_MEMORY_MIB = 4096

DEFAULTS = {
    "method": "rtc",
    "backend": "browser",
    "tick_ms": 200,
    "game_seconds": 180,
    "response_floor_ms": 3000,
    "development_rounds": 8,
    "developer_output_tokens": 262144,
    "branch_submissions": 8,
    "branch_output_tokens": 262144,
    "midpoint_submit": 4,
    "review_output_tokens": 8192,
    "development_timeout_seconds": 18000,
    "team_timeout_seconds": 55000,
    "request_timeout_seconds": 1800,
    "phase_wait_timeout_seconds": 20000,
    "resource_admission_timeout_seconds": 18000,
    "initial": "warm",
    "initial_seconds": 60,
    "initial_requests": 20,
    "developer_model": "Qwen3.8-27B",
    "model": "Qwen3.5-2B",
    "initial_model": "Qwen3.5-2B",
    "jev_model": "vev-4b",
    "fake": False,
    "browser_memory_mib": DEFAULT_BROWSER_MEMORY_MIB,
}


def load_config(path, *, task_id=None):
    path = Path(path).resolve()
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    config = {**DEFAULTS, **raw}
    for entry in config.get("endpoints", {}).values():
        if not isinstance(entry, dict) or set(entry) - {"url", "url_env", "key_env"}:
            raise ValueError(
                "endpoints accept only url/url_env/key_env; credentials must be environment references"
            )
        url = urlsplit(entry.get("url", ""))
        if url.username or url.password:
            raise ValueError("credential-bearing endpoint URLs are not permitted")
    if task_id or config.get("task_id"):
        from gpa.gameworld import task_config

        config = task_config(config, task_id or config["task_id"])
    if config.get("initial_bundle"):
        config["initial_bundle"] = str((path.parent / config["initial_bundle"]).resolve())
    if config["method"] not in {"rtc", "single_agent", "multi_agent"}:
        raise ValueError("method must be rtc, single_agent, or multi_agent")
    if config["backend"] not in {"fake", "browser", "gameworld"}:
        raise ValueError("unknown backend")
    if type(config["fake"]) is not bool or config["fake"] != (config["backend"] == "fake"):
        raise ValueError("fake models/CLI are allowed only with the fake game")
    if config["tick_ms"] != 200:
        raise ValueError("tick_ms must be 200")
    if config["initial"] not in {"warm", "cold"}:
        raise ValueError("initial must be warm or cold")
    if not isinstance(config.get("task"), str) or not config["task"].strip():
        raise ValueError("task must contain public objectives and controls")
    for name in (
        "game_seconds",
        "initial_seconds",
        "initial_requests",
        "developer_output_tokens",
        "branch_output_tokens",
        "review_output_tokens",
        "development_timeout_seconds",
        "request_timeout_seconds",
        "team_timeout_seconds",
        "phase_wait_timeout_seconds",
        "resource_admission_timeout_seconds",
        "rtc_max_requests",
        "rtc_max_tokens",
        "initial_max_tokens",
        "browser_memory_mib",
    ):
        if name not in config:
            continue
        value = config[name]
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if (
        type(config["response_floor_ms"]) is not int
        or not 0 <= config["response_floor_ms"] <= 60000
    ):
        raise ValueError("invalid response floor")
    if config["method"] != "rtc" and config["game_seconds"] != 60 and not config["fake"]:
        raise ValueError("game_seconds must be 60 for code policy submissions")
    for name in ("development_rounds", "branch_submissions"):
        if type(config[name]) is not int or not 1 <= config[name] <= 8:
            raise ValueError(f"{name} must be 1..8")
    if config["method"] == "multi_agent":
        if not 0 < config["midpoint_submit"] < config["branch_submissions"]:
            raise ValueError("midpoint must precede final submission")
        config["developer_output_tokens"] = 4 * config["branch_output_tokens"]
    if config["backend"] != "fake" and not config.get("url"):
        raise ValueError("browser/gameworld require url")
    if config["backend"] == "gameworld":
        contract = config.get("gameworld", {})
        if not {"game_id", "task_id", "evaluator_config", "protocol"} <= contract.keys():
            raise ValueError("gameworld requires the official task evaluator contract")
        if config.get("extended_inputs", False):
            from gpa.adapters.gameworld170.gameworld import input_instructions

            config["task"] += input_instructions(
                {**contract, "input_profile": config.get("input_profile")}
            )
        elif config.get("input_profile"):
            raise ValueError("input_profile requires extended_inputs=true")
    elif config.get("input_profile"):
        raise ValueError("input profiles only apply to GameWorld")
    return config


def endpoint(config, name, *, required=True):
    """URL can be configured, key can only be referenced by environment variable."""
    entry = config.get("endpoints", {}).get(name, {})
    url = entry.get("url") or os.environ.get(entry.get("url_env", name.upper() + "_BASE_URL"), "")
    key = os.environ.get(entry.get("key_env", name.upper() + "_API_KEY"), "EMPTY")
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise ValueError("credential-bearing endpoint URLs are not permitted")
    if required and not url:
        raise ValueError(f"missing {name} endpoint")
    return url.rstrip("/"), key
