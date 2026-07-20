import json
from datetime import UTC, datetime
from pathlib import Path

from obsidian_sidecar.config import Settings
from obsidian_sidecar.curator import StaticCurator
from obsidian_sidecar.queueing import enqueue_event
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
