"""Render game objectives and controls for the RTC model."""

import json
import re

LEGACY_INTRO = (
    "Play this GameWorld task using Maa skills. You control all characters in this browser. "
    "The task starts at the configured level; a reset restarts this task."
)


def task_text(original):
    """Remove identified evaluator-interface metadata, keeping objectives and rules."""
    text = original.replace(LEGACY_INTRO, "")
    rows = []
    for line in text.splitlines():
        if line.startswith("Controls: "):
            controls = json.loads(line.removeprefix("Controls: "))
            if set(controls) - {"allowed_keys", "allow_clicks", "hold_duration", "key_durations"}:
                raise ValueError("unreviewed_control_metadata")
            continue
        rows.append(line.replace("ACTION SPACE (ONLY LEGAL ACTIONS):", "Game controls:"))
    text = "\n".join(rows).strip()
    for old, new in {
        "ArrowUp": "up",
        "ArrowDown": "down",
        "ArrowLeft": "left",
        "ArrowRight": "right",
        "Space": "space",
        "Enter": "enter",
    }.items():
        text = re.sub(r"\b" + old + r"\b", new, text)
    if re.search(r"\b(?:maa|skills?|hold_duration|allow_clicks)\b", text, re.I):
        raise ValueError("legacy_task_instructions_remain")
    return text
