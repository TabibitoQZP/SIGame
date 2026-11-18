import json
from pathlib import Path
import time

import pytest

from gpa.coding.realtime_observer import validate_capabilities
from gpa.config import load_config
from gpa.gameworld import tasks, locked, catalog_file
from gpa.realtime.recording import FrameStore, Journal
from gpa.realtime.runtime import Frame

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_catalog_resolves_all_tasks():
    catalog = tasks()
    assert len(catalog) == 170
    assert len({x["game_id"] for x in catalog.values()}) == 34
    assert locked()["commit"] == "e5ddb16df85b91a1d17bb0577621cc7c5e65d738"
    assert json.loads(catalog_file("gameworld-manifest.json"))["files"]
    for task_id in catalog:
        config = load_config(ROOT / "configs/single_agent.json", task_id=task_id)
        assert config["task_id"] == config["gameworld"]["task_id"] == task_id
        assert config["task"].startswith(catalog[task_id]["task"])
        assert config["developer_model"] == "Qwen3.8-27B"
        assert config["url"].startswith("http://game:8000/benchmark/")
    config = load_config(ROOT / "configs/single_agent.json", task_id="19_05")
    assert config["task"].count("Input adapter override:") == 1


def test_browser_defaults_and_development_profiles_match_prepared_limits():
    from gpa.config import DEFAULT_BROWSER_MEMORY_MIB
    from gpa.execution.docker import LIMITS
    from gpa.execution.resources import MIB

    assert DEFAULT_BROWSER_MEMORY_MIB == 4096
    assert LIMITS["browser"][0] == 4096 * MIB
    for task in ("01_05", "18_05", "22_01", "22_05"):
        for method in ("single_agent", "multi_agent"):
            config = load_config(ROOT / f"configs/{method}.json", task_id=task)
            assert config["browser_memory_mib"] == 4096
            assert config["branch_output_tokens"] == 256 * 1024
            assert config["developer_output_tokens"] == (4 if method == "multi_agent" else 1) * 256 * 1024
            assert config["review_output_tokens"] == 8192
            assert config["resource_admission_timeout_seconds"] == 18000


@pytest.mark.parametrize(
    "change",
    [
        {"model": "another-model"},
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"tools": [{"name": "WebFetch"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "http://game:8000/private"},
                        }
                    ],
                }
            ]
        },
        {
            "messages": [
                {"role": "user", "content": [{"type": "document", "source": {"type": "base64"}}]}
            ]
        },
    ],
)
def test_model_proxy_rejects_extra_capabilities(change):
    with pytest.raises(ValueError):
        validate_capabilities({"model": "deepseek-flash", **change}, "deepseek-flash")


def test_frame_history_keeps_mixed_window_and_cold_frames_reload(tmp_path):
    journal = Journal(tmp_path)
    frames = FrameStore(tmp_path)
    frames.references = journal.references
    for i in range(1000):
        frame = Frame(bytes([i % 256]) * 100, time.monotonic_ns(), 0, i)
        frames[frame.identity] = frame
        journal.put("image", frame)
    journal.close()
    assert journal.error is None
    assert len(frames.hot) == 96
    assert sum(len(f.data) for f in frames.metadata.values()) == 0
    for offset in (0, 1, 2, 5, 15, 45, 90):
        assert frames[f"e0-f{999 - offset}"].data == bytes([(999 - offset) % 256]) * 100
    assert frames["e0-f0"].data == bytes(100)


def test_oversized_frame_is_rejected_without_unbounded_journal_queue(tmp_path):
    journal = Journal(tmp_path)
    journal.put("image", Frame(b"x" * (33 * 1024**2), 1, 0, 0))
    journal.close()
    assert journal.pending_image_bytes == 0
    assert journal.error == "journal_image_memory_budget_exceeded"
