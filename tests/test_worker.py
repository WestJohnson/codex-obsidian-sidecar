import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from obsidian_sidecar.config import Settings
from obsidian_sidecar.checkpoints import checkpoint_path, load_checkpoint
from obsidian_sidecar.curator import StaticCurator
from obsidian_sidecar.queueing import enqueue_event, save_event
from obsidian_sidecar.worker import daemon_once, process_ready
from obsidian_sidecar.coordination import CloudLease


class SecretFailureCurator:
    def curate(self, packet: dict) -> dict:
        del packet
        raise RuntimeError("provider rejected api_key=abcdefghijklmnopqrstuvwx")


def test_worker_processes_end_to_end(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.worker.reindex_basic_memory", lambda _settings: "ok"
    )
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )
    result = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert result.failed == 0
    assert result.notes_written == 1
    assert result.processed_events == 1
    assert result.reindex_result == "ok"
    assert not list(settings.queue_dir.glob("*.json"))
    assert len(list(settings.processed_dir.glob("*.json"))) == 1
    assert len(list((settings.vault_path / "60 Sessions").rglob("*.md"))) == 1
    assert result.checkpoint_updates == 1
    assert checkpoint_path(settings, "fixture-session-001").is_file()


def test_worker_normalizes_safe_topic_metadata_before_validation(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.worker.reindex_basic_memory", lambda _settings: "ok"
    )
    curation = deepcopy(valid_curation)
    curation["topics"] = [f"topic-{index}" for index in range(14)]
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )

    result = process_ready(settings, force=True, curator=StaticCurator(curation))

    checkpoint = load_checkpoint(settings, "fixture-session-001")
    assert result.failed == 0
    assert result.notes_written == 1
    assert checkpoint is not None
    assert checkpoint["curation"]["topics"] == [
        f"topic-{index}" for index in range(12)
    ]


def test_worker_reconciles_only_failed_events_covered_by_a_committed_cursor(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.worker.reindex_basic_memory", lambda _settings: "ok"
    )
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    enqueue_event(settings, event)
    first = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert first.failed == 0

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "A later uncovered turn."}
                        ],
                    },
                }
            )
            + "\n"
        )
    covered = settings.failed_dir / "covered--max-attempts.json"
    uncovered = settings.failed_dir / "uncovered--max-attempts.json"
    save_event(
        covered,
        {**event, "turn_id": "failed-covered", "attempts": 3},
    )
    save_event(
        uncovered,
        {
            **event,
            "turn_id": "failed-uncovered",
            "captured_at": "2026-07-14T08:03:00Z",
            "attempts": 3,
        },
    )

    result = process_ready(settings, force=True, curator=StaticCurator(valid_curation))

    assert result.reconciled_failed_events == 1
    assert not covered.exists()
    assert uncovered.exists()
    processed = next(
        settings.processed_dir.glob("covered--max-attempts--superseded-by-checkpoint.json")
    )
    record = json.loads(processed.read_text(encoding="utf-8"))
    assert record["disposition"] == "superseded-by-checkpoint"
    assert record["reconciliation"]["checkpoint_byte_offset"] >= record[
        "reconciliation"
    ]["event_boundary_byte_offset"]


def test_worker_uses_saved_checkpoint_for_the_next_turn(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.worker.reindex_basic_memory", lambda _settings: "ok"
    )
    first_event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    enqueue_event(settings, first_event)
    first = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert first.failed == 0

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Approve the release."}
                        ],
                    },
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The release was approved and verified.",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    second_curation = deepcopy(valid_curation)
    second_curation["current_phase"] = "complete"
    second_curation["resume_context"] = "No follow-up is required."
    second_curation["outcome"] = "The release was approved and verified."
    second_curation["changes"].append(
        {"text": "Approved the release.", "evidence_ids": ["a1"]}
    )
    second_curation["verification"].append(
        {"text": "Verified the approved release.", "evidence_ids": ["a1"]}
    )
    second_curation["unresolved"] = []
    second_curation["next_actions"] = []
    for field in ("decisions", "changes", "verification"):
        retained = (
            second_curation[field][:-1]
            if field != "decisions"
            else second_curation[field]
        )
        for item in retained:
            item["evidence_ids"] = ["c1"]
    enqueue_event(
        settings,
        {**first_event, "turn_id": "turn-2", "captured_at": "2026-07-14T08:03:00Z"},
    )

    second = process_ready(
        settings, force=True, curator=StaticCurator(second_curation)
    )

    assert second.failed == 0
    checkpoint = load_checkpoint(settings, "fixture-session-001")
    assert checkpoint is not None and checkpoint["update_count"] == 2
    note = next((settings.vault_path / "60 Sessions").rglob("*.md")).read_text(
        encoding="utf-8"
    )
    assert "capture_mode: incremental" in note
    assert "Use one responsive implementation" in note
    assert "## Resume Context\n\nNo follow-up is required." in note


def test_failed_vault_write_does_not_advance_checkpoint(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.worker.reindex_basic_memory", lambda _settings: "ok"
    )
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    enqueue_event(settings, event)
    process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    path = checkpoint_path(settings, "fixture-session-001")
    before = path.read_bytes()
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": "Verified a new change."}
                        ],
                    },
                }
            )
            + "\n"
        )
    enqueue_event(
        settings,
        {**event, "turn_id": "turn-2", "captured_at": "2026-07-14T08:03:00Z"},
    )
    retry_curation = deepcopy(valid_curation)
    for field in ("decisions", "changes", "verification", "unresolved", "next_actions"):
        for item in retry_curation[field]:
            item["evidence_ids"] = ["c1"]
    def fail_write(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("obsidian_sidecar.worker.write_curation", fail_write)

    result = process_ready(
        settings, force=True, curator=StaticCurator(retry_curation)
    )

    assert result.failed == 1
    assert path.read_bytes() == before


def test_invalid_output_is_quarantined_and_retried(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
) -> None:
    invalid = dict(valid_curation)
    invalid["changes"] = [{"text": "Ungrounded", "evidence_ids": ["a999"]}]
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
        },
    )
    result = process_ready(settings, force=True, curator=StaticCurator(invalid))
    assert result.failed == 1
    queued = list(settings.queue_dir.glob("*.json"))
    assert len(queued) == 1
    assert len(list((settings.vault_path / "_System/Quarantine").glob("*.json"))) == 1


def test_final_failed_attempt_is_persisted_before_quarantine(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
) -> None:
    invalid = deepcopy(valid_curation)
    invalid["changes"] = [{"text": "Ungrounded", "evidence_ids": ["a999"]}]
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )

    for _ in range(3):
        process_ready(settings, force=True, curator=StaticCurator(invalid))

    failed = json.loads(
        next(settings.failed_dir.glob("*--max-attempts.json")).read_text(
            encoding="utf-8"
        )
    )
    assert failed["attempts"] == 3
    assert "unknown evidence id" in failed["last_error"]


def test_worker_redacts_secrets_from_failure_state(
    settings: Settings,
    transcript_path: Path,
    tmp_path: Path,
) -> None:
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-secret",
            "turn_id": "turn-secret",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
        },
    )

    result = process_ready(settings, force=True, curator=SecretFailureCurator())

    assert result.failed == 1
    event = json.loads(
        next(settings.queue_dir.glob("*.json")).read_text(encoding="utf-8")
    )
    log = (settings.log_dir / "worker-errors.log").read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwx" not in json.dumps(event)
    assert "abcdefghijklmnopqrstuvwx" not in log
    assert "[REDACTED_SECRET]" in event["last_error"]
    assert "[REDACTED_SECRET]" in log


def test_worker_consumes_legacy_event_without_transcript_path(
    settings: Settings, valid_curation: dict
) -> None:
    legacy = settings.queue_dir / "legacy-missing-transcript.json"
    legacy.write_text(
        json.dumps(
            {
                "session_id": "legacy-internal-session",
                "turn_id": "legacy-turn",
                "attempts": 3,
            }
        ),
        encoding="utf-8",
    )

    result = process_ready(
        settings, force=True, curator=StaticCurator(valid_curation)
    )

    assert result.skipped == 1
    assert result.failed == 0
    assert result.processed_events == 1
    assert not list(settings.queue_dir.glob("*.json"))
    assert (settings.processed_dir / legacy.name).exists()


def test_worker_defers_while_cloud_maintenance_lease_is_active(
    settings: Settings,
    transcript_path: Path,
    valid_curation: dict,
    tmp_path: Path,
) -> None:
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-deferred",
            "turn_id": "turn-deferred",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
        },
    )
    now = datetime.now(UTC)
    with CloudLease(settings.vault_path, ttl_seconds=600, now=now):
        result = process_ready(
            settings, force=True, curator=StaticCurator(valid_curation)
        )

    assert result.notes_written == 0
    assert result.failed == 0
    assert result.deferred_reason == "cloud-maintenance-active"
    assert len(list(settings.queue_dir.glob("*.json"))) == 1


def test_daemon_creates_hourly_dirty_git_checkpoint(settings: Settings) -> None:
    from dataclasses import replace

    configured = replace(
        settings,
        auto_git_backup=True,
        git_checkpoint_interval_seconds=3600,
        alerts_enabled=False,
    )
    (configured.state_dir / "health.json").write_text("{}", encoding="utf-8")
    note = configured.vault_path / "manual-note.md"
    note.write_text("# Manual note\n", encoding="utf-8")
    result = daemon_once(configured)

    assert result["checkpoint"]["status"] == "ok"
    assert (configured.state_dir / "git-checkpoint.json").exists()
    assert (configured.vault_path / ".git").exists()
