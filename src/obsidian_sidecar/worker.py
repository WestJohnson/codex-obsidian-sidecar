from __future__ import annotations

import fcntl
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .checkpoints import (
    load_checkpoint,
    save_checkpoint,
    seed_checkpoint_from_vault,
)
from .config import Settings
from .coordination import LeaseBusy, LocalWriterLease, cloud_lease_status
from .curator import CodexLunaCurator, Curator
from .maintenance import (
    commit_git_backup,
    indexing_problem,
    inspect_vault,
    mark_index_pending,
    reindex_basic_memory,
    write_health_report,
)
from .queueing import (
    capture_cutoff,
    has_usable_transcript_path,
    load_event,
    move_event,
    ready_groups,
    recover_captures,
    save_event,
    utc_now,
)
from .security import redact_text
from .transcript import _cursor_at_cutoff, build_curation_packet, resolve_session_event
from .validation import normalize_curation_metadata, validate_curation
from .vault import write_curation, write_quarantine
from .vault import _atomic_write


@dataclass
class ProcessSummary:
    groups_seen: int = 0
    notes_written: int = 0
    skipped: int = 0
    failed: int = 0
    processed_events: int = 0
    reconciled_failed_events: int = 0
    checkpoint_items_compacted: int = 0
    checkpoint_updates: int = 0
    checkpoint_chunks_pending: int = 0
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
        self.handle = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Respect a recent legacy directory lock during a rolling upgrade.
        if self.path.is_dir():
            age = time.time() - self.path.stat().st_mtime
            if age <= self.stale_seconds:
                return self
        self.handle = self.path.with_suffix(".flock").open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except BlockingIOError:
            self.handle.close()
            self.handle = None
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        self.acquired = False


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
        save_event(path, event)
        if event["attempts"] >= 3:
            move_event(path, settings.failed_dir, "max-attempts")


def _checkpoint_coverage(
    settings: Settings, event: dict[str, Any]
) -> dict[str, int] | None:
    try:
        event = resolve_session_event(event)
    except (OSError, ValueError, TypeError):
        return None
    session_id = event.get("session_id")
    transcript_value = event.get("transcript_path")
    captured_at = event.get("captured_at")
    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (session_id, transcript_value, captured_at)
    ):
        return None
    try:
        capture_cutoff(event)
    except ValueError:
        return None

    checkpoint = load_checkpoint(settings, session_id)
    if checkpoint is None:
        return None
    return _cursor_coverage(event, checkpoint)


def _cursor_coverage(
    event: dict[str, Any], checkpoint: dict[str, Any]
) -> dict[str, int] | None:
    try:
        if capture_cutoff(event) > capture_cutoff(checkpoint):
            return None
    except ValueError:
        return None
    transcript_value = event.get("transcript_path")
    if not has_usable_transcript_path(event):
        return None
    cursor = checkpoint.get("cursor")
    if not isinstance(cursor, dict):
        return None

    transcript_path = Path(transcript_value).expanduser()
    checkpoint_path_value = str(cursor.get("transcript_path") or "")
    checkpoint_offset = cursor.get("byte_offset")
    checkpoint_update_count = checkpoint.get("update_count")
    if (
        not transcript_path.is_file()
        or not checkpoint_path_value
        or Path(checkpoint_path_value).expanduser().resolve()
        != transcript_path.resolve()
        or isinstance(checkpoint_offset, bool)
        or not isinstance(checkpoint_offset, int)
        or checkpoint_offset < 0
        or checkpoint_offset > transcript_path.stat().st_size
        or isinstance(checkpoint_update_count, bool)
        or not isinstance(checkpoint_update_count, int)
        or checkpoint_update_count < 1
    ):
        return None

    try:
        event_boundary = _cursor_at_cutoff(
            transcript_path, event["captured_at"], require_complete=True
        )
    except ValueError:
        return None
    if checkpoint_offset < event_boundary:
        return None
    return {
        "checkpoint_update_count": checkpoint_update_count,
        "checkpoint_byte_offset": checkpoint_offset,
        "event_boundary_byte_offset": event_boundary,
    }


def _retire_covered_events(
    paths: list[Path], settings: Settings, packet: dict[str, Any]
) -> int:
    covered = []
    for path in paths:
        event = load_event(path)
        if settings.checkpoint_enabled:
            coverage = _checkpoint_coverage(settings, event)
        else:
            coverage = _cursor_coverage(
                event,
                {
                    "cursor": packet["checkpoint"]["cursor"],
                    "captured_at": packet["captured_at"],
                    "update_count": 1,
                },
            )
        if coverage is not None:
            covered.append(path)
    _mark_group(covered, settings, "processed")
    return len(covered)


def reconcile_superseded_failures(settings: Settings) -> int:
    reconciled = 0
    for path in sorted(settings.failed_dir.glob("*.json")):
        try:
            event = load_event(path)
            coverage = _checkpoint_coverage(settings, event)
            if coverage is None:
                continue
            event["disposition"] = "superseded-by-checkpoint"
            event["reconciled_at"] = utc_now()
            event["reconciliation"] = {
                "reason": "superseded-by-checkpoint",
                **coverage,
            }
            save_event(path, event)
            move_event(path, settings.processed_dir, "superseded-by-checkpoint")
            reconciled += 1
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return reconciled


def process_ready(
    settings: Settings,
    *,
    force: bool = False,
    curator: Curator | None = None,
) -> ProcessSummary:
    try:
        return _process_ready(settings, force=force, curator=curator)
    except LeaseBusy as error:
        return ProcessSummary(deferred_reason=error.reason)


def _process_ready(
    settings: Settings, *, force: bool = False, curator: Curator | None = None
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
        recover_captures(settings)
        summary.reconciled_failed_events = reconcile_superseded_failures(settings)
        groups = ready_groups(settings, force=force)
        summary.groups_seen = len(groups)
        retry_index = settings.runtime_role == "local" and indexing_problem(settings)
        if not groups and not retry_index:
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
                    try:
                        capture_cutoff(candidate_event)
                    except ValueError:
                        continue
                    if has_usable_transcript_path(candidate_event):
                        event = candidate_event
                        break
                if event is None:
                    # Old malformed entries are not proof that a session was captured.
                    summary.failed += 1
                    _mark_group(paths, settings, "failed")
                    continue
                try:
                    event = resolve_session_event(event)
                    session_id = str(event.get("session_id") or "unknown")
                    checkpoint = load_checkpoint(settings, session_id)
                    if checkpoint is None and settings.checkpoint_enabled:
                        transcript_value = str(event.get("transcript_path") or "")
                        checkpoint = seed_checkpoint_from_vault(
                            settings,
                            session_id=session_id,
                            transcript_path=Path(transcript_value).expanduser(),
                        )
                    packet = build_curation_packet(
                        event,
                        checkpoint=checkpoint,
                        checkpoint_max_evidence_chars=(
                            settings.checkpoint_max_evidence_chars
                        ),
                    )
                    raw_curation = active_curator.curate(packet)
                    curation = normalize_curation_metadata(raw_curation)
                    summary.checkpoint_items_compacted += sum(
                        max(
                            0,
                            len(raw_curation.get(field, []))
                            - len(curation.get(field, [])),
                        )
                        for field in (
                            "decisions",
                            "changes",
                            "verification",
                            "unresolved",
                            "next_actions",
                        )
                        if isinstance(raw_curation.get(field), list)
                        and isinstance(curation.get(field), list)
                    )
                    validation = validate_curation(
                        curation,
                        packet,
                        minimum_confidence=settings.minimum_confidence,
                    )
                    if not validation.valid:
                        mark_index_pending(settings)
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
                        checkpoint_curation = (
                            checkpoint.get("curation")
                            if checkpoint
                            and isinstance(checkpoint.get("curation"), dict)
                            else curation
                        )
                        if save_checkpoint(
                            settings,
                            packet,
                            checkpoint_curation,
                            previous=checkpoint,
                        ):
                            summary.checkpoint_updates += 1
                        summary.skipped += 1
                        if bool((packet.get("checkpoint") or {}).get("has_more")):
                            summary.checkpoint_chunks_pending += 1
                        summary.processed_events += _retire_covered_events(
                            paths, settings, packet
                        )
                        continue
                    mark_index_pending(settings)
                    result = write_curation(
                        settings,
                        curation,
                        packet,
                        review_required=validation.review_required,
                    )
                    if save_checkpoint(
                        settings,
                        packet,
                        curation,
                        previous=checkpoint,
                    ):
                        summary.checkpoint_updates += 1
                    summary.notes_written += 1
                    summary.note_paths.append(str(result.note_path))
                    if bool((packet.get("checkpoint") or {}).get("has_more")):
                        summary.checkpoint_chunks_pending += 1
                    summary.processed_events += _retire_covered_events(
                        paths, settings, packet
                    )
                except Exception as exc:
                    summary.failed += 1
                    _record_failure(paths, settings, exc)
            if summary.notes_written or retry_index:
                summary.reindex_result = reindex_basic_memory(settings)
    return summary


def _deferred_maintenance(settings: Settings, reason: str) -> dict[str, Any]:
    health = inspect_vault(settings, create_layout=False)
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
        health.indexing_problem = indexing_problem(settings)
        if reindex_result == "ok":
            health.basic_memory = "ok"
        else:
            health.basic_memory = reindex_result
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
        "reindex_result": reindex_result,
    }


def run_maintenance(settings: Settings, *, backup: bool = True) -> dict[str, Any]:
    try:
        result = _run_maintenance(settings, backup=backup)
    except LeaseBusy as error:
        return _deferred_maintenance(settings, error.reason)
    if (
        not result.get("deferred_reason")
        and result.get("critical_failures") == 0
        and result.get("reindex_result") in {"ok", "not-required"}
        and result.get("backup_result") in {"ok", "clean", "disabled"}
    ):
        save_event(
            settings.state_dir / "maintenance-success.json",
            {"schema": 1, "completed_at": utc_now()},
        )
    return result


def _run_maintenance(settings: Settings, *, backup: bool = True) -> dict[str, Any]:
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
    try:
        return _run_git_checkpoint_uncontended(settings)
    except LeaseBusy as error:
        return {"status": "deferred", "reason": error.reason}


def _run_git_checkpoint_uncontended(settings: Settings) -> dict[str, Any]:
    lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
    if lease_active:
        return {"status": "deferred", "reason": f"cloud-maintenance-{lease_reason}"}
    with LocalWriterLease(settings.vault_path, ttl_seconds=900):
        lease_active, lease_reason, _ = cloud_lease_status(settings.vault_path)
        if lease_active:
            return {"status": "deferred", "reason": f"cloud-maintenance-{lease_reason}"}
        result = commit_git_backup(settings, "chore(memory): hourly local checkpoint")
    checked_at = utc_now()
    if result in {"ok", "clean"}:
        _atomic_write(
            settings.state_dir / "git-checkpoint.json",
            json.dumps(
                {"schema": 1, "checked_at": checked_at, "result": result}, indent=2
            )
            + "\n",
        )
    return {"status": result, "checked_at": checked_at}


def daemon_once(settings: Settings) -> dict[str, Any]:
    # Serialize the whole timer tick, including maintenance and backup scheduling.
    with ProcessLock(settings.lock_dir / "daemon.lock") as lock:
        if not lock.acquired:
            return {"status": "deferred", "reason": "local-daemon-lock"}
        status_path = settings.state_dir / "worker-status.json"
        previous: dict[str, Any] = {}
        try:
            if status_path.exists():
                previous = load_event(status_path)
        except (OSError, ValueError):
            pass
        save_event(status_path, {"checked_at": utc_now(), "status": "running"})
        try:
            result = _daemon_once(settings)
        except Exception as error:
            save_event(
                status_path,
                {
                    "checked_at": utc_now(),
                    "status": "error",
                    "error": type(error).__name__,
                },
            )
            _run_alerts(settings)
            raise
        processing = result["processing"]
        maintenance = result.get("maintenance") or {}
        checkpoint = result.get("checkpoint") or {}
        deferred = processing.get("deferred_reason") or maintenance.get(
            "deferred_reason"
        )
        if checkpoint.get("status") == "deferred":
            deferred = deferred or checkpoint.get("reason")
        failed = bool(processing.get("failed")) or checkpoint.get("status") not in {
            None,
            "ok",
            "clean",
            "deferred",
        }
        failed = failed or maintenance.get("backup_result") not in {
            None,
            "ok",
            "clean",
            "disabled",
            "deferred",
        }
        failed = failed or bool(indexing_problem(settings))
        failed = failed or processing.get("reindex_result") not in {None, "ok"}
        failed = failed or maintenance.get("reindex_result") not in {
            None,
            "ok",
            "not-required",
        }
        status = {
            "checked_at": utc_now(),
            "status": "error" if failed else ("deferred" if deferred else "ok"),
        }
        if deferred:
            status["deferred_since"] = (
                previous.get("deferred_since") or status["checked_at"]
            )
            status["reason"] = deferred
        save_event(status_path, status)
        if processing.get("reindex_result") or maintenance.get("reindex_result"):
            health = inspect_vault(settings, create_layout=False)
            if not health.indexing_problem and (
                processing.get("reindex_result") == "ok"
                or maintenance.get("reindex_result") == "ok"
            ):
                health.basic_memory = "ok"
            save_event(
                settings.state_dir / "health.json",
                {
                    **asdict(health),
                    "critical_failures": health.critical_failures,
                    "warnings": health.warnings,
                    "score": health.score,
                },
            )
        result["alerts"] = _run_alerts(settings)
        return result


def _daemon_once(settings: Settings) -> dict[str, Any]:
    from .updates import maybe_check_for_update

    processed = process_ready(settings)
    maintenance: dict[str, Any] | None = None
    if _maintenance_due(settings):
        maintenance = run_maintenance(settings)
    checkpoint: dict[str, Any] | None = None
    if maintenance is None and _checkpoint_due(settings, time.time()):
        checkpoint = _run_git_checkpoint(settings)
    elif maintenance is not None and maintenance.get("backup_result") in {
        "ok",
        "clean",
    }:
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
    updates = maybe_check_for_update(settings)
    return {
        "processing": asdict(processed),
        "maintenance": maintenance,
        "checkpoint": checkpoint,
        "updates": updates,
    }


def _maintenance_due(settings: Settings) -> bool:
    try:
        state = load_event(settings.state_dir / "maintenance-success.json")
        completed = datetime.fromisoformat(state["completed_at"])
        if completed.tzinfo is None:
            return True
        age = time.time() - completed.timestamp()
        return age < 0 or age >= 86_400
    except (OSError, ValueError, TypeError, KeyError):
        return True


def _run_alerts(settings: Settings) -> dict[str, Any]:
    from .alerts import run_alert_cycle

    try:
        alerts = run_alert_cycle(settings)
    except Exception as error:
        clean_error, _ = redact_text(str(error))
        with (settings.log_dir / "alert-errors.log").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(f"{utc_now()} {type(error).__name__}: {clean_error[:500]}\n")
        alerts = {"status": "error", "error": type(error).__name__}
    return alerts
