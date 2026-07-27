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
    assert (
        seeded["curation"]["decisions"][0]["decision_type"]
        == "implemented-choice"
    )
    assert seeded["curation"]["unresolved"][0]["disposition"] == "scheduled"


def test_checkpoint_persists_artifact_to_decision_associations(
    settings, tmp_path: Path
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    packet = {
        "session_id": "artifact-associations",
        "captured_at": "2026-07-25T20:00:00Z",
        "checkpoint": {
            "cursor": {
                "transcript_path": str(tmp_path / "transcript.jsonl"),
                "byte_offset": 10,
                "after_timestamp": None,
            }
        },
        "artifacts": [
            {"label": "First", "path": str(first), "evidence_id": "a1"},
            {"label": "Second", "path": str(second), "evidence_id": "a2"},
        ],
        "model_provenance": {
            "model": "gpt-test-model",
            "provider": "openai",
        },
    }
    curation = {
        "decisions": [
            {
                "text": "Associate only the first artifact.",
                "decision_type": "implemented-choice",
                "evidence_ids": ["a1"],
            },
            {
                "text": "Associate only the second artifact.",
                "decision_type": "implemented-choice",
                "evidence_ids": ["a2"],
            },
        ]
    }

    path = save_checkpoint(settings, packet, curation, previous=None)
    assert path is not None
    loaded = load_checkpoint(settings, "artifact-associations")
    assert loaded is not None
    assert loaded["model_provenance"]["model"] == "gpt-test-model"
    fingerprints = [
        artifact["decision_fingerprints"] for artifact in loaded["artifacts"]
    ]
    assert len(fingerprints[0]) == 1
    assert len(fingerprints[1]) == 1
    assert fingerprints[0] != fingerprints[1]
