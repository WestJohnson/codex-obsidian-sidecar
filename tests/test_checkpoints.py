from __future__ import annotations

import stat
from pathlib import Path

from obsidian_sidecar.checkpoints import (
    checkpoint_evidence,
    checkpoint_path,
    load_checkpoint,
    save_checkpoint,
    seed_checkpoint_from_vault,
)
from obsidian_sidecar.transcript import build_curation_packet
from obsidian_sidecar.vault import write_curation


def test_checkpoint_round_trip_is_private_and_strips_stale_evidence(
    settings, transcript_path: Path, valid_curation: dict, tmp_path: Path
) -> None:
    packet = build_curation_packet(
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:01:00Z",
        }
    )

    path = save_checkpoint(
        settings, packet, valid_curation, previous=None
    )

    assert path == checkpoint_path(settings, "fixture-session-001")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_checkpoint(settings, "fixture-session-001")
    assert loaded is not None
    assert loaded["curation"]["decisions"][0]["evidence_ids"] == ["a1"]
    evidence = checkpoint_evidence(loaded, maximum_chars=20_000)
    assert "Use one responsive implementation" in evidence
    assert "evidence_ids" not in evidence
    assert '"a1"' not in evidence


def test_corrupt_checkpoint_falls_back_without_raising(settings) -> None:
    path = checkpoint_path(settings, "broken-session")
    path.write_text("{not-json", encoding="utf-8")

    assert load_checkpoint(settings, "broken-session") is None
    log = settings.log_dir / "checkpoint-errors.jsonl"
    assert log.is_file()
    assert "JSONDecodeError" in log.read_text(encoding="utf-8")


def test_existing_session_note_seeds_first_checkpoint(
    settings, transcript_path: Path, valid_curation: dict, tmp_path: Path
) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    packet = build_curation_packet(event)
    write_curation(settings, valid_curation, packet, review_required=False)

    seeded = seed_checkpoint_from_vault(
        settings,
        session_id="fixture-session-001",
        transcript_path=transcript_path,
    )

    assert seeded is not None
    assert seeded["update_count"] == 0
    assert seeded["cursor"]["byte_offset"] is None
    assert seeded["cursor"]["after_timestamp"] == "2026-07-14T08:01:00Z"
    assert seeded["curation"]["current_phase"] == "verification"
    assert seeded["curation"]["unresolved"][0]["disposition"] == "scheduled"
