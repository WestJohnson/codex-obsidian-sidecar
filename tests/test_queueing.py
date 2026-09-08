import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obsidian_sidecar.config import Settings
from obsidian_sidecar.curator import StaticCurator
from obsidian_sidecar.queueing import (
    capture_hook,
    enqueue_event,
    load_event,
    ready_groups,
)
from obsidian_sidecar.worker import process_ready


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


def test_capture_hook_retains_missing_transcript_path_for_recovery(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "internal-session"})),
    )

    assert capture_hook(settings) == 0
    assert not list(settings.queue_dir.glob("*.json"))
    pending = list(settings.capture_pending_dir.glob("*.json"))
    assert len(pending) == 1
    assert load_event(pending[0])["reason"] == "missing-transcript-path"


def test_transcript_only_hooks_share_debounce_and_one_curation(
    settings: Settings, transcript_path: Path, valid_curation: dict, monkeypatch
) -> None:
    settings = replace(settings, debounce_seconds=60)
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "timestamp": "2026-07-14T08:02:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": "Second completed turn."}
                        ],
                    },
                }
            )
            + "\n"
        )
    cutoffs = ["2026-07-14T08:01:00Z", "2026-07-14T08:03:00Z", "2026-07-14T08:02:00Z"]
    paths = [
        enqueue_event(
            settings,
            {
                "transcript_path": str(transcript_path),
                "captured_at": cutoff,
                **({"session_id": "fixture-session-001"} if index == 1 else {}),
            },
        )
        for index, cutoff in enumerate(cutoffs)
    ]
    now = datetime.now(UTC).timestamp()
    for index, path in enumerate(paths):
        os.utime(path, (now - (10 if index == 2 else 120),) * 2)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]

    assert ready_groups(settings) == []
    assert ready_groups(settings, force=True) == [[paths[0], paths[2], paths[1]]]
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before
    for path in paths:
        os.utime(path, (now - 120,) * 2)
    packets = []

    class RecordingCurator:
        def curate(self, packet):
            packets.append(packet)
            return valid_curation

    monkeypatch.setattr(
        "obsidian_sidecar.maintenance._reindex_basic_memory", lambda *a, **k: "ok"
    )
    result = process_ready(settings, curator=RecordingCurator())
    assert result.groups_seen == 1
    assert result.failed == 0
    assert result.processed_events == 3
    assert len(packets) == 1
    assert packets[0]["captured_at"] == max(cutoffs)
    assert "Second completed turn." in [item["text"] for item in packets[0]["evidence"]]
    assert [
        load_event(settings.processed_dir / path.name)["captured_at"] for path in paths
    ] == cutoffs


@pytest.mark.parametrize(
    "metadata",
    [None, "{", "[]", '{"type":"event_msg"}', '{"type":"session_meta","payload":{}}'],
)
def test_unresolved_grouping_metadata_preserves_each_capture_for_retry(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
    metadata: str | None,
) -> None:
    damaged = tmp_path / "unavailable.jsonl"
    if metadata is not None:
        damaged.write_text(metadata, encoding="utf-8")
    cutoffs = ["2026-07-14T08:01:00Z", "2026-07-14T08:03:00Z"]
    paths = [
        enqueue_event(
            settings,
            {
                "transcript_path": str(damaged),
                "captured_at": cutoff,
            },
        )
        for cutoff in cutoffs
    ]
    originals = [path.read_bytes() for path in paths]
    assert {tuple(group) for group in ready_groups(settings)} == {
        (path,) for path in paths
    }
    assert [path.read_bytes() for path in paths] == originals

    class UncalledCurator:
        def curate(self, packet):
            pytest.fail("Unresolved metadata must not spend a curator call")

    failed = process_ready(settings, curator=UncalledCurator())
    assert failed.failed == 2
    assert failed.processed_events == 0
    assert [load_event(path)["attempts"] for path in paths] == [1, 1]
    assert [load_event(path)["captured_at"] for path in paths] == cutoffs

    damaged.write_bytes(transcript_path.read_bytes())
    assert ready_groups(settings) == [paths]
    monkeypatch.setattr(
        "obsidian_sidecar.maintenance._reindex_basic_memory", lambda *a, **k: "ok"
    )
    recovered = process_ready(settings, curator=StaticCurator(valid_curation))
    assert recovered.groups_seen == 1
    assert recovered.failed == 0
    assert recovered.processed_events == 2
