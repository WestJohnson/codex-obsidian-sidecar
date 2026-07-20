from __future__ import annotations

import time
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .coordination import LocalWriterLease, cloud_lease_status
from .curator import CodexLunaCurator, Curator
from .maintenance import (
    commit_git_backup,
    inspect_vault,
    reindex_basic_memory,
    write_health_report,
)
from .queueing import (
    has_usable_transcript_path,
    load_event,
    move_event,
    ready_groups,
    save_event,
    utc_now,
)
from .security import redact_text
from .transcript import build_curation_packet
from .validation import validate_curation
from .vault import write_curation, write_quarantine
from .vault import _atomic_write


@dataclass
class ProcessSummary:
    groups_seen: int = 0
    notes_written: int = 0
    skipped: int = 0
    failed: int = 0
    processed_events: int = 0
    note_paths: list[str] | None = None
    deferred_reason: str | None = None
    reindex_result: str | None = None

    def __post_init__(self) -> None:
        if self.note_paths is None:
            self.note_paths = []


class ProcessLock:
    def __init__(self, path: Path, stale_seconds: int = 900) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.acquired = False

    def __enter__(self) -> "ProcessLock":
        try:
            self.path.mkdir(parents=True)
            self.acquired = True
        except FileExistsError:
            age = time.time() - self.path.stat().st_mtime
            if age > self.stale_seconds:
                self.path.rmdir()
                self.path.mkdir(parents=True)
                self.acquired = True
        return self

    def __exit__(self, *_: object) -> None:
        if self.acquired:
            self.path.rmdir()


def _mark_group(paths: list[Path], settings: Settings, destination: str) -> None:
    target = (
        settings.processed_dir if destination == "processed" else settings.failed_dir
    )
    for path in paths:
        if path.exists():
            move_event(path, target)


def _record_failure(paths: list[Path], settings: Settings, error: Exception) -> None:
    clean_error, _ = redact_text(str(error))
    detail = f"{utc_now()} {type(error).__name__}: {clean_error[:1_500]}\n"
    with (settings.log_dir / "worker-errors.log").open("a", encoding="utf-8") as handle:
        handle.write(detail)
    for path in paths:
        if not path.exists():
            continue
        event = load_event(path)
        event["attempts"] = int(event.get("attempts", 0)) + 1
        event["last_error"] = f"{type(error).__name__}: {clean_error[:500]}"
        event["last_attempt_at"] = utc_now()
        if event["attempts"] >= 3:
            move_event(path, settings.failed_dir, "max-attempts")
        else:
            save_event(path, event)


def process_ready(
    settings: Settings,
    *,
    force: bool = False,
    curator: Curator | None = None,
) -> ProcessSummary:
    summary = ProcessSummary()
    lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
    if settings.runtime_role == "local" and lease_active:
        summary.deferred_reason = f"cloud-maintenance-{lease_reason}"
        return summary
    active_curator = curator or CodexLunaCurator(settings)
    with ProcessLock(settings.lock_dir / "worker.lock") as lock:
        if not lock.acquired:
            summary.deferred_reason = "local-worker-lock"
            return summary
        groups = ready_groups(settings, force=force)
        summary.groups_seen = len(groups)
        if not groups:
            return summary
        with LocalWriterLease(
            settings.vault_path,
            ttl_seconds=max(900, settings.curator_timeout_seconds * len(groups) + 300),
        ):
            for paths in groups:
                lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
                if settings.runtime_role == "local" and lease_active:
                    summary.deferred_reason = f"cloud-maintenance-{lease_reason}"
                    break
                event = None
                for candidate in reversed(paths):
                    candidate_event = load_event(candidate)
                    if has_usable_transcript_path(candidate_event):
                        event = candidate_event
                        break
                if event is None:
                    summary.skipped += 1
                    _mark_group(paths, settings, "processed")
                    summary.processed_events += len(paths)
                    continue
                try:
                    packet = build_curation_packet(event)
                    curation = active_curator.curate(packet)
                    validation = validate_curation(
                        curation,
                        packet,
                        minimum_confidence=settings.minimum_confidence,
                    )
                    if not validation.valid:
                        write_quarantine(
                            settings,
                            session_id=str(packet.get("session_id") or "unknown"),
                            reason="; ".join(validation.errors),
                            curation=curation,
                        )
                        raise ValueError(
                            "curation validation failed: "
                            + "; ".join(validation.errors)
                        )
                    if curation.get("skip"):
                        summary.skipped += 1
                        _mark_group(paths, settings, "processed")
                        summary.processed_events += len(paths)
                        continue
                    result = write_curation(
                        settings,
                        curation,
                        packet,
                        review_required=validation.review_required,
                    )
                    summary.notes_written += 1
                    summary.note_paths.append(str(result.note_path))
                    _mark_group(paths, settings, "processed")
                    summary.processed_events += len(paths)
                except Exception as exc:
                    summary.failed += 1
                    _record_failure(paths, settings, exc)
    if summary.notes_written:
        summary.reindex_result = reindex_basic_memory(settings)
    return summary


def _deferred_maintenance(settings: Settings, reason: str) -> dict[str, Any]:
    health = inspect_vault(settings)
    return {
        **asdict(health),
        "critical_failures": health.critical_failures,
        "warnings": health.warnings,
        "score": health.score,
        "backup_result": "deferred",
        "deferred_reason": reason,
    }


def _run_maintenance_unfenced(
    settings: Settings, *, backup: bool = True
) -> dict[str, Any]:
    from .knowledge import write_knowledge_report

    health = inspect_vault(settings)
    write_knowledge_report(settings)
    if settings.runtime_role == "cloud":
        reindex_result = "not-required"
        health.basic_memory = "not-required"
    else:
        reindex_result = reindex_basic_memory(settings)
        if reindex_result == "ok":
            health.basic_memory = "ok"
    backup_result = "disabled"
    if backup and settings.auto_git_backup and health.critical_failures == 0:
        health.git_backup = "ok"
        write_health_report(settings, health)
        backup_result = commit_git_backup(settings)
        if backup_result not in {"ok", "clean"}:
            health.git_backup = backup_result
            write_health_report(settings, health)
    else:
        write_health_report(settings, health)
    return {
        **asdict(health),
        "critical_failures": health.critical_failures,
        "warnings": health.warnings,
        "score": health.score,
        "backup_result": backup_result,
    }


def run_maintenance(settings: Settings, *, backup: bool = True) -> dict[str, Any]:
    if settings.runtime_role != "local":
        return _run_maintenance_unfenced(settings, backup=backup)

    lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
    if lease_active:
        return _deferred_maintenance(settings, f"cloud-maintenance-{lease_reason}")
    with LocalWriterLease(
        settings.vault_path,
        ttl_seconds=max(900, settings.curator_timeout_seconds + 300),
    ):
        lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
        if lease_active:
            return _deferred_maintenance(settings, f"cloud-maintenance-{lease_reason}")
        return _run_maintenance_unfenced(settings, backup=backup)


def _checkpoint_due(settings: Settings, now: float) -> bool:
    if not settings.auto_git_backup or settings.git_checkpoint_interval_seconds <= 0:
        return False
    state = settings.state_dir / "git-checkpoint.json"
    return (
        not state.exists()
        or now - state.stat().st_mtime >= settings.git_checkpoint_interval_seconds
    )


def _run_git_checkpoint(settings: Settings) -> dict[str, Any]:
    lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
    if lease_active:
        return {"status": "deferred", "reason": f"cloud-maintenance-{lease_reason}"}
    with LocalWriterLease(settings.vault_path, ttl_seconds=900):
        lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
        if lease_active:
            return {"status": "deferred", "reason": f"cloud-maintenance-{lease_reason}"}
        result = commit_git_backup(settings, "chore(memory): hourly local checkpoint")
    checked_at = utc_now()
    _atomic_write(
        settings.state_dir / "git-checkpoint.json",
        json.dumps({"schema": 1, "checked_at": checked_at, "result": result}, indent=2)
        + "\n",
    )
    return {"status": result, "checked_at": checked_at}


def daemon_once(settings: Settings) -> dict[str, Any]:
    from .alerts import run_alert_cycle
    from .updates import maybe_check_for_update

    processed = process_ready(settings)
    health_path = settings.state_dir / "health.json"
    maintenance: dict[str, Any] | None = None
    if not health_path.exists() or time.time() - health_path.stat().st_mtime >= 86_400:
        maintenance = run_maintenance(settings)
    checkpoint: dict[str, Any] | None = None
    if maintenance is None and _checkpoint_due(settings, time.time()):
        checkpoint = _run_git_checkpoint(settings)
    elif maintenance is not None:
        _atomic_write(
            settings.state_dir / "git-checkpoint.json",
            json.dumps(
                {
                    "schema": 1,
                    "checked_at": utc_now(),
                    "result": maintenance.get("backup_result", "unknown"),
                },
                indent=2,
            )
            + "\n",
        )
    try:
        alerts = run_alert_cycle(settings)
    except Exception as error:
        clean_error, _ = redact_text(str(error))
        with (settings.log_dir / "alert-errors.log").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(f"{utc_now()} {type(error).__name__}: {clean_error[:500]}\n")
        alerts = {"status": "error", "error": type(error).__name__}
    updates = maybe_check_for_update(settings)
    return {
        "processing": asdict(processed),
        "maintenance": maintenance,
        "checkpoint": checkpoint,
        "alerts": alerts,
        "updates": updates,
    }
