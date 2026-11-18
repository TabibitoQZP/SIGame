import json
from pathlib import Path

import pytest

from gpa.config import load_config
from gpa.coding.materials import snapshot
from gpa.coding.workflow import prepare_initial
from gpa.evidence.files import dump, load
from gpa.rtc.codec import parse_line

ROOT = Path(__file__).resolve().parents[1]


def test_shared_initial_validates_task_and_content(short_dir):
    config = load_config(ROOT / "configs/fake-single_agent.json")
    config["initial"] = "cold"
    source = short_dir / "initial"
    result = prepare_initial(config, source)
    assert result["initial_requests"] == 0
    imported = prepare_initial({**config, "initial_bundle": str(source)}, short_dir / "copy")
    assert imported == result
    with pytest.raises(ValueError, match="identity"):
        prepare_initial(
            {**config, "task": "other task", "initial_bundle": str(source)}, short_dir / "wrong"
        )
    (source / "result.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        prepare_initial({**config, "initial_bundle": str(source)}, short_dir / "corrupt")


@pytest.mark.parametrize("legacy", [False, True])
def test_warm_bundle_checks_effective_exploration_settings(short_dir, monkeypatch, legacy):
    from gpa.operation import Operation

    config = {
        **load_config(ROOT / "configs/fake-single_agent.json"),
        "initial_model": "deepseek-flash",
        "initial_max_tokens": 2048,
        "engineering_validation": True,
        "endpoints": {"rtc": {"url": "http://localhost"}},
    }

    def record_initial(self, policy, output):
        dump(output.parent / "requests/r0001/request.json", {
            "model": policy.model,
            "max_tokens": policy.max_tokens,
            "thinking": {"type": "enabled" if policy.thinking else "disabled"},
        })
        return {"error": None}

    monkeypatch.setattr(Operation, "run", record_initial)
    source = short_dir / "initial"
    result = prepare_initial(config, source)
    manifest = load(source / "bundle.json")
    assert manifest["exploration"] == {"max_tokens": 2048, "thinking": False}
    if legacy:
        manifest.pop("exploration")
        dump(source / "bundle.json", manifest)
    config["initial_bundle"] = str(source)
    assert prepare_initial(config, short_dir / "copy") == result
    for index, override in enumerate(({"initial_max_tokens": 8192}, {"engineering_validation": False})):
        target = short_dir / f"mismatch-{index}"
        with pytest.raises(ValueError, match="exploration settings"):
            prepare_initial({**config, **override}, target)
        assert not target.exists()


def test_snapshot_freezes_and_rejects_symlinks(short_dir):
    source = short_dir / "policy"
    source.mkdir()
    (source / "policy.py").write_text("def step(f,s): return '(wait())',s\n")
    hashes = snapshot(source, short_dir / "copy")
    assert "policy.py" in hashes
    (source / "secret").symlink_to(short_dir / "copy/policy.py")
    with pytest.raises(ValueError, match="symlinks"):
        snapshot(source, short_dir / "invalid")


def test_gameworld_right_click_is_explicit_and_public(short_dir):
    from gpa.adapters.gameworld170.gameworld import GameWorldAdapter

    config = {
        "backend": "gameworld",
        "method": "rtc",
        "task": "Reveal the board.",
        "url": "http://localhost:8000/?task=19_05",
        "extended_inputs": True,
        "input_profile": "ctrl_click_right_v1",
        "gameworld": {
            "game_id": "19_minesweeper",
            "task_id": "19_05",
            "evaluator_config": {},
            "protocol": {},
        },
    }
    path = short_dir / "config.json"
    path.write_text(json.dumps(config))
    checked = load_config(path)
    assert "RIGHT click" in checked["task"]
    adapter = GameWorldAdapter(config["url"], input_profile=config["input_profile"])
    plan = adapter.input_plan(parse_line("(press(ctrl),click(.2,.3))"))
    assert plan == {"keys": (), "click": (0.2, 0.3), "button": "right"}
    config["gameworld"]["game_id"] = "01_2048"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="mismatch"):
        load_config(path)


def test_fake_cannot_be_used_as_a_real_browser_escape(short_dir):
    config = {"method": "single_agent", "backend": "browser", "fake": True, "task": "x"}
    path = short_dir / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="fake models"):
        load_config(path)
