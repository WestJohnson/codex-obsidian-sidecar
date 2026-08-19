import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import obsidian_sidecar.alerts as alerts
from obsidian_sidecar.alerts import alert_status, run_alert_cycle
from obsidian_sidecar.config import Settings


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


def test_alert_cycle_notifies_once_then_suppresses(settings: Settings) -> None:
    configured = replace(settings, alerts_enabled=True)
    failed = configured.failed_dir / "failed.json"
    failed.write_text("{}", encoding="utf-8")
    notifications: list[tuple[str, str]] = []

    first = run_alert_cycle(
        configured,
        now=NOW,
        notifier=lambda title, message: notifications.append((title, message)),
    )
    second = run_alert_cycle(
        configured,
        now=NOW + timedelta(minutes=5),
        notifier=lambda title, message: notifications.append((title, message)),
    )

    assert first["status"] == "notified"
    assert second["status"] == "suppressed"
    assert len(notifications) == 1
    assert "failed events" in notifications[0][1]
    assert "vault sync is a separate subsystem" in notifications[0][1]


def test_alert_status_only_flags_staged_report_after_24_hours(
    settings: Settings,
) -> None:
    stage = settings.state_dir / "cloud-staged-report.json"
    stage.write_text(
        json.dumps(
            {
                "schema": 1,
                "staged_at": (NOW - timedelta(hours=25)).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    result = alert_status(settings, now=NOW)

    assert [item["code"] for item in result["alerts"]] == ["staged-report-stale"]


def test_remote_cloud_failure_marker_is_included_without_connectivity_alert(
    settings: Settings,
) -> None:
    configured = replace(
        settings,
        alerts_enabled=True,
        cloud_status_ssh_host="memory-vps",
    )
    notifications: list[str] = []

    result = run_alert_cycle(
        configured,
        now=NOW,
        notifier=lambda _title, message: notifications.append(message),
        remote_probe=lambda _host: {
            "alerts": [
                {
                    "code": "cloud-failure-marker",
                    "title": "Obsidian cloud maintenance failure persists",
                }
            ]
        },
    )

    assert result["status"] == "notified"
    assert notifications == ["Obsidian cloud maintenance failure persists"]


def test_reconnect_failure_marker_is_reported_separately(settings: Settings) -> None:
    (settings.state_dir / "reconnect.failed").touch()

    result = alert_status(settings, now=NOW)

    assert result["alerts"] == [
        {
            "code": "cloud-reconnect-failure-marker",
            "title": "Obsidian cloud reconnect failure persists",
        }
    ]


def test_remote_cloud_probe_is_cached_for_fifteen_minutes(
    settings: Settings,
) -> None:
    configured = replace(
        settings,
        alerts_enabled=True,
        cloud_status_ssh_host="memory-vps",
        cloud_status_probe_interval_seconds=900,
    )
    calls = 0

    def probe(_host: str) -> dict:
        nonlocal calls
        calls += 1
        return {"alerts": []}

    run_alert_cycle(
        configured, now=NOW, notifier=lambda *_args: None, remote_probe=probe
    )
    run_alert_cycle(
        configured,
        now=NOW + timedelta(minutes=5),
        notifier=lambda *_args: None,
        remote_probe=probe,
    )

    assert calls == 1


def test_linux_notification_uses_notify_send(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(alerts.shutil, "which", lambda _name: "/usr/bin/notify-send")

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(alerts.subprocess, "run", run)

    alerts._linux_notification("Memory alert", "Cloud maintenance needs attention")

    assert calls == [
        [
            "/usr/bin/notify-send",
            "--app-name",
            "Codex Memory",
            "Memory alert",
            "Cloud maintenance needs attention",
        ]
    ]


def test_platform_notification_selects_linux(monkeypatch) -> None:
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts.sys, "platform", "linux")
    monkeypatch.setattr(
        alerts,
        "_linux_notification",
        lambda title, message: notifications.append((title, message)),
    )

    alerts._platform_notification("Memory alert", "Linux works")

    assert notifications == [("Memory alert", "Linux works")]


def test_platform_notification_preserves_macos_path(monkeypatch) -> None:
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts.sys, "platform", "darwin")
    monkeypatch.setattr(
        alerts,
        "_macos_notification",
        lambda title, message: notifications.append((title, message)),
    )

    alerts._platform_notification("Memory alert", "macOS still works")

    assert notifications == [("Memory alert", "macOS still works")]


def test_alert_cycle_cools_down_stale_report_when_notification_is_unavailable(
    settings: Settings, monkeypatch
) -> None:
    configured = replace(settings, alerts_enabled=True)
    (configured.state_dir / "cloud-staged-report.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "staged_at": (NOW - timedelta(hours=25)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(alerts.sys, "platform", "linux")
    monkeypatch.setattr(alerts.shutil, "which", lambda _name: None)

    first = run_alert_cycle(configured, now=NOW)
    second = run_alert_cycle(
        configured,
        now=NOW + timedelta(minutes=5),
    )
    state = json.loads((configured.state_dir / "alert-state.json").read_text())

    assert first["status"] == "recorded"
    assert first["notification_error"] == "RuntimeError"
    assert second["status"] == "suppressed"
    assert state["active_codes"] == ["staged-report-stale"]
    assert state["notification_error"] == "RuntimeError"
