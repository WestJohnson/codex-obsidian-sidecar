import io
import json
from pathlib import Path

import pytest

from obsidian_sidecar.config import Settings
from obsidian_sidecar.queueing import (
    capture_hook,
    enqueue_event,
    load_event,
    ready_groups,
)


def test_enqueue_is_idempotent(settings: Settings, transcript_path: Path) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": "/tmp/example",
        "hook_event_name": "Stop",
    }
    first = enqueue_event(settings, event)
    second = enqueue_event(settings, event)
    assert first == second
    assert len(list(settings.queue_dir.glob("*.json"))) == 1
    assert load_event(first)["session_id"] == "fixture-session-001"


def test_ready_groups_batches_one_session(
    settings: Settings, transcript_path: Path
) -> None:
    for turn in ("turn-1", "turn-2"):
        enqueue_event(
            settings,
            {
                "session_id": "same-session",
                "turn_id": turn,
                "transcript_path": str(transcript_path),
                "cwd": "/tmp/example",
            },
        )
    groups = ready_groups(settings, force=True)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_enqueue_rejects_missing_transcript_path(settings: Settings) -> None:
    with pytest.raises(ValueError, match="no transcript_path"):
        enqueue_event(settings, {"session_id": "internal-session"})


def test_capture_hook_skips_missing_transcript_path(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "internal-session"})),
    )

    assert capture_hook(settings) == 0
    assert not list(settings.queue_dir.glob("*.json"))
    assert "missing-transcript-path" in (
        settings.log_dir / "capture-skips.log"
    ).read_text(encoding="utf-8")
