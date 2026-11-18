"""Parse the action strings returned by the RTC model."""

import math
import re
import string
from dataclasses import asdict, dataclass

KEYS = {c: c for c in string.ascii_lowercase + string.digits}
KEYS.update(
    up="ArrowUp",
    down="ArrowDown",
    left="ArrowLeft",
    right="ArrowRight",
    space="Space",
    enter="Enter",
    esc="Escape",
    shift="Shift",
    ctrl="Control",
    alt="Alt",
    tab="Tab",
    backspace="Backspace",
    delete="Delete",
)
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
OP = re.compile(r"(press|click|wait|reset)\s*\(([^()]*)\)")


@dataclass(frozen=True)
class Action:
    kind: str = "wait"
    keys: tuple[str, ...] = ()
    click: tuple[float, float] | None = None

    def __post_init__(self):
        if self.kind not in {"input", "wait", "reset"}:
            raise ValueError("unknown_action")
        if self.kind != "input" and (self.keys or self.click is not None):
            raise ValueError("standalone_control")
        if self.kind == "input" and not self.keys and self.click is None:
            raise ValueError("empty_input")
        if len(self.keys) > 4 or len(set(self.keys)) != len(self.keys):
            raise ValueError("keys_must_be_distinct_max_four")
        if any(k not in KEYS for k in self.keys):
            raise ValueError("unknown_key")
        if self.click is not None and (
            len(self.click) != 2
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1
                for v in self.click
            )
        ):
            raise ValueError("invalid_normalized_click")

    def text(self):
        if self.kind != "input":
            return f"({self.kind}())"
        parts = ["press(" + ",".join(self.keys) + ")"] if self.keys else []
        if self.click is not None:
            parts.append("click(" + ",".join(format(v, ".15g") for v in self.click) + ")")
        return "(" + ", ".join(parts) + ")"

    def record(self):
        return {**asdict(self), "text": self.text()}


def parse_line(line):
    line = line.strip()
    if not line.startswith("(") or not line.endswith(")"):
        raise ValueError("expected_parenthesized_tick")
    inside, position, operations = line[1:-1].strip(), 0, {}
    while position < len(inside):
        match = OP.match(inside, position)
        if not match:
            raise ValueError("invalid_operation_grammar")
        name, args = match.groups()
        if name in operations:
            raise ValueError("duplicate_operation")
        operations[name] = args.strip()
        position = match.end()
        suffix = inside[position:]
        if not suffix.strip():
            break
        separator = re.match(r"\s*,\s*", suffix)
        if not separator or position + separator.end() == len(inside):
            raise ValueError("expected_operation_separator")
        position += separator.end()
    if set(operations) & {"wait", "reset"}:
        if len(operations) != 1:
            raise ValueError("control_must_be_standalone")
        name, args = next(iter(operations.items()))
        if args:
            raise ValueError("control_takes_no_arguments")
        return Action(name)
    keys, click = (), None
    if "press" in operations:
        keys = tuple(p.strip() for p in operations["press"].split(","))
        if not keys or not all(keys):
            raise ValueError("press_requires_keys")
    if "click" in operations:
        args = [v.strip() for v in operations["click"].split(",")]
        if len(args) != 2 or not all(re.fullmatch(NUMBER, v) for v in args):
            raise ValueError("click_requires_two_numbers")
        click = tuple(float(v) for v in args)
    return Action("input", keys, click)


def parse_chunk(text, count, *, finish_reason="stop", max_count=30):
    if finish_reason != "stop":
        raise ValueError("incomplete_completion:" + str(finish_reason))
    if (
        not isinstance(text, str)
        or type(count) is not int
        or type(max_count) is not int
        or not 1 <= max_count <= 120
        or not 1 <= count <= max_count
    ):
        raise ValueError("invalid_chunk_contract")
    lines = text.strip().splitlines()
    if len(lines) != count or any(not line.strip() for line in lines):
        raise ValueError(f"expected_{count}_tick_lines")
    actions = tuple(parse_line(line) for line in lines)
    if any(a.kind == "reset" for a in actions[:-1]):
        raise ValueError("reset_must_be_last_tick")
    return actions
