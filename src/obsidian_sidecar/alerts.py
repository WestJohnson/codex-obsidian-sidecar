from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .vault import _atomic_write


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sync_conflicts(vault: Path) -> list[str]:
    return sorted(
        path.relative_to(vault).as_posix()
        for path in vault.rglob("*")
        if path.is_file()
        and ".sync-conflict-" in path.name
        and not {".git", ".stversions", ".trash"}.intersection(path.parts)
    )


def alert_status(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    checked_at = now or datetime.now(UTC)
    alerts: list[dict[str, Any]] = []
    failed = sorted(path.name for path in settings.failed_dir.glob("*.json"))
    if failed:
        alerts.append(
            {
                "code": "queue-failed",
                "title": "Obsidian memory queue has failed events",
                "count": len(failed),
            }
        )
    conflicts = _sync_conflicts(settings.vault_path)
    if conflicts:
        alerts.append(
            {
                "code": "sync-conflict",
                "title": "Obsidian vault has a sync conflict",
                "count": len(conflicts),
            }
        )
    failure_marker = settings.state_dir / "maintenance.failed"
    if failure_marker.exists():
        alerts.append(
            {
                "code": "cloud-failure-marker",
                "title": "Obsidian cloud maintenance failure persists",
            }
        )
    reconnect_failure_marker = settings.state_dir / "reconnect.failed"
    if reconnect_failure_marker.exists():
        alerts.append(
            {
                "code": "cloud-reconnect-failure-marker",
                "title": "Obsidian cloud reconnect failure persists",
            }
        )
    staged = settings.state_dir / "cloud-staged-report.json"
    if staged.exists():
        staged_at = datetime.fromtimestamp(staged.stat().st_mtime, tz=UTC)
        try:
            value = json.loads(staged.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("staged_at"), str):
                staged_at = _parse_time(value["staged_at"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        age = checked_at - staged_at
        if age >= timedelta(hours=settings.staged_report_alert_hours):
            alerts.append(
                {
                    "code": "staged-report-stale",
                    "title": "Obsidian cloud report is still unpublished",
                    "age_hours": round(age.total_seconds() / 3600, 1),
                }
            )
    return {
        "checked_at": checked_at.isoformat(),
        "healthy": not alerts,
        "alerts": alerts,
    }


def _remote_cloud_status(host: str) -> dict[str, Any]:
    command = [
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        host,
        "runuser",
        "-u",
        "obsidian-sync",
        "--",
        "/opt/obsidian-cloud/venv/bin/obsidian-sidecar",
        "--config",
        "/etc/obsidian-cloud/config.json",
        "alert-status",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError("cloud alert probe failed")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("cloud alert probe returned invalid JSON")
    return value


def _macos_notification(title: str, message: str) -> None:
    def escaped(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{escaped(message)}" with title "{escaped(title)}"'
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("macOS notification delivery failed")


def run_alert_cycle(
    settings: Settings,
    *,
    now: datetime | None = None,
    notifier: Callable[[str, str], None] | None = None,
    remote_probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checked_at = now or datetime.now(UTC)
    if not settings.alerts_enabled:
        return {"status": "disabled", "checked_at": checked_at.isoformat()}
    status = alert_status(settings, now=checked_at)
    remote_error: str | None = None
    if settings.runtime_role == "local" and settings.cloud_status_ssh_host:
        cache_path = settings.state_dir / "cloud-alert-cache.json"
        try:
            remote: dict[str, Any]
            if (
                cache_path.exists()
                and checked_at.timestamp() - cache_path.stat().st_mtime
                < settings.cloud_status_probe_interval_seconds
            ):
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("cloud alert cache is invalid")
                remote = loaded
            else:
                remote = (remote_probe or _remote_cloud_status)(
                    settings.cloud_status_ssh_host
                )
                _atomic_write(cache_path, json.dumps(remote, indent=2) + "\n")
            status["alerts"].extend(remote.get("alerts", []))
            status["healthy"] = not status["alerts"]
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            remote_error = type(error).__name__

    state_path = settings.state_dir / "alert-state.json"
    if not status["alerts"]:
        _atomic_write(
            state_path,
            json.dumps(
                {
                    "schema": 1,
                    "checked_at": checked_at.isoformat(),
                    "active_fingerprint": None,
                    "remote_probe_error": remote_error,
                },
                indent=2,
            )
            + "\n",
        )
        return {
            "status": "clear",
            "checked_at": checked_at.isoformat(),
            "remote_probe_error": remote_error,
        }

    fingerprint = hashlib.sha256(
        json.dumps(status["alerts"], sort_keys=True).encode()
    ).hexdigest()
    prior: dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prior = loaded
        except (OSError, json.JSONDecodeError):
            pass
    last_notified_at = prior.get("last_notified_at")
    cooldown_active = False
    if prior.get("active_fingerprint") == fingerprint and isinstance(
        last_notified_at, str
    ):
        try:
            cooldown_active = (
                checked_at - _parse_time(last_notified_at)
            ).total_seconds() < settings.alert_cooldown_seconds
        except ValueError:
            pass
    if cooldown_active:
        return {
            "status": "suppressed",
            "checked_at": checked_at.isoformat(),
            "alerts": status["alerts"],
            "remote_probe_error": remote_error,
        }

    titles = [str(item.get("title") or item.get("code")) for item in status["alerts"]]
    (notifier or _macos_notification)("Codex memory needs attention", "; ".join(titles))
    _atomic_write(
        state_path,
        json.dumps(
            {
                "schema": 1,
                "checked_at": checked_at.isoformat(),
                "last_notified_at": checked_at.isoformat(),
                "active_fingerprint": fingerprint,
                "active_codes": [item.get("code") for item in status["alerts"]],
                "remote_probe_error": remote_error,
            },
            indent=2,
        )
        + "\n",
    )
    return {
        "status": "notified",
        "checked_at": checked_at.isoformat(),
        "alerts": status["alerts"],
        "remote_probe_error": remote_error,
    }
