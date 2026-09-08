from __future__ import annotations

import json
import shlex
import subprocess
import tarfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from obsidian_sidecar.cloud import (
    AGENT_SCHEMA,
    AGENT_SYSTEM_PROMPT,
    OpenRouterCloudAgent,
    SyncSnapshot,
    cloud_doctor,
    collect_evidence,
    create_cloud_backup,
    load_cloud_tasks,
    run_cloud_benchmark,
    run_cloud_reconcile,
    run_cloud_maintenance,
    source_snapshot,
    validate_cloud_backup,
)
from obsidian_sidecar.config import Settings
from obsidian_sidecar.coordination import (
    CloudLease,
    LocalWriterLease,
    MachineProcessLock,
)
from obsidian_sidecar.vault import parse_frontmatter


NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


class FakeSync:
    def __init__(self, snapshot: SyncSnapshot | None = None) -> None:
        self.value = snapshot or SyncSnapshot("idle", 0, 0, 0, 100, "valid", True)
        self.scans = 0
        self.waits = 0

    def snapshot(self) -> SyncSnapshot:
        return self.value

    def scan(self) -> None:
        self.scans += 1

    def wait_healthy(self, timeout_seconds: int) -> SyncSnapshot:
        assert timeout_seconds > 0
        self.waits += 1
        return self.value


class FakeAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.tasks: list[dict[str, str]] = []

    def analyze(
        self,
        evidence: list[dict[str, str]],
        tasks: list[dict[str, str]] | None = None,
    ) -> dict:
        self.calls += 1
        self.tasks = tasks or []
        first = evidence[0]["path"]
        return {
            "summary": "The changed note is organized and searchable.",
            "organization_actions": [
                {
                    "title": "Keep the project note current",
                    "rationale": "It is the primary durable context.",
                    "priority": "medium",
                    "evidence_paths": [first],
                }
            ],
            "suggested_links": [],
            "quality_issues": [],
            "topics": [{"name": "memory", "evidence_paths": [first]}],
            "next_actions": ["Review the derived report."],
            "_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
            "_model": "test-model",
        }


def cloud_settings(settings: Settings, tmp_path: Path) -> Settings:
    return replace(
        settings,
        runtime_role="cloud",
        cloud_agent_enabled=True,
        cloud_backup_dir=tmp_path / "backups",
        cloud_backup_retention=2,
        cloud_settle_timeout_seconds=5,
        auto_git_backup=True,
    )


@pytest.mark.parametrize("contention", ["writer", "cloud", "host"])
@pytest.mark.parametrize("connected", [True, False])
def test_cloud_contention_defers_without_work_and_recovers(
    settings, tmp_path, contention, connected
):
    from obsidian_sidecar.alerts import alert_status
    from obsidian_sidecar.queueing import load_event

    configured = cloud_settings(settings, tmp_path)
    locks = {
        "writer": LocalWriterLease(configured.vault_path, ttl_seconds=600),
        "cloud": CloudLease(configured.vault_path, ttl_seconds=600),
        "host": MachineProcessLock(configured.lock_dir / "cloud-maintenance.lock"),
    }
    client = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", connected))
    agent = FakeAgent()
    (configured.vault_path / "project.md").write_text("# Project\n")
    with locks[contention]:
        result = run_cloud_maintenance(configured, client=client, agent=agent)
        assert result["status"] == "deferred"
        assert agent.calls == 0
        assert not list(configured.cloud_backup_dir.glob("*.tar.gz"))
        assert alert_status(configured)["healthy"]
    result = run_cloud_maintenance(configured, client=client, agent=agent)
    assert result["status"] == ("ok" if connected else "offline-staged")
    assert agent.calls == 1
    state = load_event(configured.state_dir / "cloud-maintenance-status.json")
    assert state["status"] == "ok"
    assert "deferred_since" not in state


def test_reconcile_contention_preserves_retry_eligibility(settings, tmp_path):
    from obsidian_sidecar.queueing import load_event

    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n")
    agent = FakeAgent()
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    run_cloud_maintenance(configured, client=offline, agent=agent)
    staged_path = configured.state_dir / "cloud-staged-report.json"
    staged = staged_path.read_bytes()
    with LocalWriterLease(configured.vault_path, ttl_seconds=600):
        deferred = run_cloud_reconcile(configured, client=FakeSync())
    assert deferred["status"] == "deferred"
    state = load_event(configured.state_dir / "cloud-reconnect-state.json")
    assert "last_attempt_at" not in state
    assert "last_success_at" not in state
    assert staged_path.read_bytes() == staged
    recovered = run_cloud_reconcile(configured, client=FakeSync())
    assert recovered["status"] == "published"
    assert recovered["result"]["agent"]["reason"].startswith(
        "published validated offline"
    )
    assert agent.calls == 1
    assert not staged_path.exists()


def test_cloud_contention_does_not_hide_sync_failure(settings, tmp_path):
    from obsidian_sidecar.alerts import alert_status

    configured = cloud_settings(settings, tmp_path)
    broken = FakeSync(SyncSnapshot("idle", 1, 0, 0, 100, "valid", True))
    with LocalWriterLease(configured.vault_path, ttl_seconds=600):
        with pytest.raises(RuntimeError, match="preflight failed"):
            run_cloud_maintenance(configured, client=broken)
        assert (
            run_cloud_maintenance(configured, client=FakeSync())["status"] == "deferred"
        )
        assert "cloud-maintenance-error" in {
            item["code"] for item in alert_status(configured)["alerts"]
        }


def test_cloud_long_deferral_is_visible_and_clears_on_recovery(settings, tmp_path):
    from datetime import timedelta
    from obsidian_sidecar.alerts import alert_status
    from obsidian_sidecar.maintenance import inspect_vault

    configured = cloud_settings(settings, tmp_path)
    now = datetime.now(UTC)
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    with LocalWriterLease(configured.vault_path, ttl_seconds=3600):
        assert (
            run_cloud_maintenance(configured, client=offline, now=now)["status"]
            == "deferred"
        )
        assert (
            run_cloud_maintenance(
                configured, client=offline, now=now + timedelta(minutes=31)
            )["status"]
            == "deferred"
        )
        assert "cloud-maintenance-stalled" in {
            item["code"]
            for item in alert_status(configured, now=now + timedelta(minutes=31))[
                "alerts"
            ]
        }
    run_cloud_maintenance(configured, client=offline)
    assert alert_status(configured)["healthy"]
    assert inspect_vault(configured).runtime_problem is None


def test_reconcile_sync_failure_still_consumes_cooldown(settings, tmp_path):
    from obsidian_sidecar.queueing import load_event

    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n")
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    run_cloud_maintenance(configured, client=offline, agent=FakeAgent())

    class FailingSync(FakeSync):
        def wait_healthy(self, timeout_seconds):
            return SyncSnapshot("syncing", 1, 1, 1, 90, "valid", True)

    with pytest.raises(RuntimeError, match="did not converge"):
        run_cloud_reconcile(configured, client=FailingSync())
    state = load_event(configured.state_dir / "cloud-reconnect-state.json")
    assert state["status"] == "error"
    assert state["last_attempt_at"]
    assert (
        run_cloud_reconcile(configured, client=FakeSync())["status"] == "rate-limited"
    )


def test_cloud_cli_returns_success_for_expected_contention(
    settings, tmp_path, monkeypatch, capsys
):
    from obsidian_sidecar import cli
    from obsidian_sidecar.cloud import SyncthingClient

    configured = cloud_settings(settings, tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda _: configured)
    monkeypatch.setattr(SyncthingClient, "from_settings", lambda _: FakeSync())
    with LocalWriterLease(configured.vault_path, ttl_seconds=600):
        assert cli.main(["cloud-maintenance"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "deferred"


@pytest.mark.parametrize("state", ["syncing", "scanning", "idle"])
def test_progressing_replication_with_writer_is_deferred(settings, tmp_path, state):
    configured = cloud_settings(settings, tmp_path)
    agent = FakeAgent()
    client = FakeSync(SyncSnapshot(state, 0, 2, 100, 90, "valid", True))
    with LocalWriterLease(configured.vault_path, ttl_seconds=600):
        result = run_cloud_maintenance(configured, client=client, agent=agent)
    assert result["status"] == "deferred"
    assert agent.calls == 0
    assert not list(configured.cloud_backup_dir.glob("*.tar.gz"))


@pytest.mark.parametrize("connected", [True, False])
def test_deferred_nightly_without_stage_retries_from_reconciler(
    settings, tmp_path, monkeypatch, connected
):
    from obsidian_sidecar import cloud
    from obsidian_sidecar.queueing import load_event

    configured = cloud_settings(settings, tmp_path)
    agent = FakeAgent()
    monkeypatch.setattr(cloud, "OpenRouterCloudAgent", lambda _: agent)
    (configured.vault_path / "project.md").write_text(
        "# Retry the deferred nightly job\n"
    )
    sync = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", connected))
    with LocalWriterLease(configured.vault_path, ttl_seconds=600):
        assert run_cloud_maintenance(configured, client=sync)["status"] == "deferred"
        assert run_cloud_reconcile(configured, client=sync)["status"] == "deferred"
        assert not (configured.state_dir / "cloud-staged-report.json").exists()
        state = load_event(configured.state_dir / "cloud-reconnect-state.json")
        assert "last_attempt_at" not in state
    assert agent.calls == 0
    recovered = run_cloud_reconcile(configured, client=sync)
    assert recovered["status"] == ("published" if connected else "offline-staged")
    assert agent.calls == 1
    assert list(configured.cloud_backup_dir.glob("*.tar.gz"))
    state = load_event(configured.state_dir / "cloud-maintenance-status.json")
    assert not state.get("maintenance_due")
    run_cloud_reconcile(configured, client=sync)
    assert agent.calls == 1


def test_sync_snapshot_distinguishes_complete_offline_replica_from_healthy_sync() -> (
    None
):
    snapshot = SyncSnapshot("idle", 0, 0, 0, 100, "valid", False)
    assert snapshot.complete is True
    assert snapshot.healthy is False


def test_cloud_doctor_marks_complete_offline_replica_read_only_safe(
    settings: Settings,
) -> None:
    result = cloud_doctor(
        settings,
        client=FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False)),
    )
    assert result["healthy"] is False
    assert result["offline_read_safe"] is True
    assert result["replica_complete"] is True


@pytest.mark.parametrize(
    "snapshot",
    [
        SyncSnapshot("syncing", 0, 0, 0, 100, "valid", True),
        SyncSnapshot("idle", 1, 0, 0, 100, "valid", True),
        SyncSnapshot("idle", 0, 1, 10, 99, "valid", True),
        SyncSnapshot("idle", 0, 0, 0, 100, "unknown-error", True),
    ],
)
def test_unsettled_sync_is_not_healthy(snapshot: SyncSnapshot) -> None:
    assert snapshot.healthy is False


def test_cloud_doctor_blocks_conflict_file(settings: Settings) -> None:
    conflict = settings.vault_path / "note.sync-conflict-20260714.md"
    conflict.write_text("conflict", encoding="utf-8")
    result = cloud_doctor(settings, client=FakeSync())
    assert result["healthy"] is False
    assert result["conflicts"] == [conflict.name]


def test_cloud_doctor_detects_synced_obsidian_config_conflict(
    settings: Settings,
) -> None:
    conflict = settings.vault_path / ".obsidian/app.sync-conflict-20260714.json"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("{}", encoding="utf-8")
    result = cloud_doctor(settings, client=FakeSync())
    assert result["healthy"] is False
    assert result["conflicts"] == [".obsidian/app.sync-conflict-20260714.json"]


def test_cloud_doctor_blocks_local_writer(settings: Settings) -> None:
    with LocalWriterLease(settings.vault_path, ttl_seconds=600, now=datetime.now(UTC)):
        result = cloud_doctor(settings, client=FakeSync())
    assert result["healthy"] is False
    assert result["local_writer"]["active"] is True


def test_cloud_doctor_blocks_cloud_lease(settings: Settings) -> None:
    with CloudLease(settings.vault_path, ttl_seconds=600, now=datetime.now(UTC)):
        result = cloud_doctor(settings, client=FakeSync())
    assert result["healthy"] is False
    assert result["lease"]["active"] is True


def test_source_snapshot_excludes_generated_and_secret_notes(
    settings: Settings,
) -> None:
    source = settings.vault_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    generated = settings.vault_path / "_System/Health/latest.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("# Generated\n", encoding="utf-8")
    task = settings.vault_path / "_System/Cloud Tasks/Pending/task.md"
    task.parent.mkdir(parents=True)
    task.write_text("---\ntype: cloud-task\n---\nReview links.\n", encoding="utf-8")
    secret = settings.vault_path / "secret.md"
    secret.write_text("api_key=abcdefghijklmnopqrstuvwx", encoding="utf-8")

    snapshot, excluded = source_snapshot(settings.vault_path)

    assert list(snapshot) == ["source.md"]
    assert excluded == ["secret.md"]


def test_collect_evidence_respects_character_cap(settings: Settings) -> None:
    for index in range(2):
        (settings.vault_path / f"{index}.md").write_text("x" * 5000, encoding="utf-8")
    evidence = collect_evidence(settings.vault_path, ["0.md", "1.md"], max_chars=6000)
    assert sum(len(item["content"]) for item in evidence) == 6000


def test_backup_excludes_git_and_rotates(settings: Settings, tmp_path: Path) -> None:
    (settings.vault_path / "note.md").write_text("hello", encoding="utf-8")
    git_file = settings.vault_path / ".git/config"
    git_file.parent.mkdir(parents=True)
    git_file.write_text("secret-ish", encoding="utf-8")
    obsidian_config = settings.vault_path / ".obsidian/app.json"
    obsidian_config.parent.mkdir(parents=True)
    obsidian_config.write_text("{}", encoding="utf-8")
    workspace = settings.vault_path / ".obsidian/workspace.json"
    workspace.write_text("{}", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    for second in range(3):
        create_cloud_backup(
            settings.vault_path,
            backup_dir,
            retention=2,
            now=NOW.replace(second=second),
        )
    backups = sorted(backup_dir.glob("*.tar.gz"))
    assert len(backups) == 2
    with tarfile.open(backups[-1]) as archive:
        names = archive.getnames()
    assert "note.md" in names
    assert ".obsidian/app.json" in names
    assert ".obsidian/workspace.json" not in names
    assert ".git/config" not in names


def test_backup_blocks_secret_before_archive_creation_or_rotation(
    settings: Settings, tmp_path: Path
) -> None:
    note = settings.vault_path / "note.md"
    note.write_text("clean", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    clean = create_cloud_backup(settings.vault_path, backup_dir, retention=1, now=NOW)
    secret = settings.vault_path / "secret.md"
    secret.write_text("api_key=abcdefghijklmnopqrstuvwx", encoding="utf-8")

    with pytest.raises(ValueError, match="backup blocked by apparent secret"):
        create_cloud_backup(
            settings.vault_path,
            backup_dir,
            retention=1,
            now=NOW.replace(minute=5),
        )

    assert list(backup_dir.glob("*.tar.gz")) == [clean]
    with tarfile.open(clean) as archive:
        assert "secret.md" not in archive.getnames()


def test_backup_rejects_file_changed_after_scan_before_publish_or_rotation(
    settings: Settings, tmp_path: Path, monkeypatch
) -> None:
    note = settings.vault_path / "note.md"
    note.write_text("clean", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    clean = create_cloud_backup(settings.vault_path, backup_dir, retention=1, now=NOW)
    original_addfile = tarfile.TarFile.addfile
    mutated = False

    def mutate_during_archive(self, tarinfo, fileobj=None):
        nonlocal mutated
        if tarinfo.name == "note.md" and not mutated:
            mutated = True
            note.write_text("api_key=abcdefghijklmnopqrstuvwx", encoding="utf-8")
        return original_addfile(self, tarinfo, fileobj)

    monkeypatch.setattr(tarfile.TarFile, "addfile", mutate_during_archive)
    with pytest.raises(RuntimeError, match="backup candidate failed validation"):
        create_cloud_backup(
            settings.vault_path,
            backup_dir,
            retention=1,
            now=NOW.replace(minute=5),
        )

    assert list(backup_dir.glob("*.tar.gz")) == [clean]
    with tarfile.open(clean) as archive:
        assert b"abcdefghijklmnopqrstuvwx" not in archive.extractfile("note.md").read()


def test_backup_validation_restores_and_matches_current_durable_files(
    settings: Settings, tmp_path: Path
) -> None:
    note = settings.vault_path / "note.md"
    note.write_text("current", encoding="utf-8")
    backup = create_cloud_backup(
        settings.vault_path, tmp_path / "backups", retention=2, now=NOW
    )

    valid, detail = validate_cloud_backup(backup, settings.vault_path, now=NOW)
    assert valid is True
    assert "matched 1 durable files" in detail

    note.write_text("changed after backup", encoding="utf-8")
    valid, detail = validate_cloud_backup(backup, settings.vault_path, now=NOW)
    assert valid is False
    assert "does not match current durable files" in detail


def test_backup_validation_accepts_intact_recovery_point_after_newer_changes(
    settings: Settings, tmp_path: Path
) -> None:
    note = settings.vault_path / "note.md"
    note.write_text("backed up", encoding="utf-8")
    backup = create_cloud_backup(
        settings.vault_path, tmp_path / "backups", retention=2, now=NOW
    )
    note.write_text("newer working copy", encoding="utf-8")

    valid, detail = validate_cloud_backup(
        backup,
        settings.vault_path,
        now=NOW.replace(hour=21),
        require_current_match=False,
    )

    assert valid is True
    assert "recovery-point age" in detail


def test_backup_validation_rejects_unmanifested_archive_member(
    settings: Settings, tmp_path: Path
) -> None:
    (settings.vault_path / "note.md").write_text("current", encoding="utf-8")
    backup = create_cloud_backup(
        settings.vault_path, tmp_path / "backups", retention=2, now=NOW
    )
    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(backup) as archive:
        archive.extractall(restored, filter="data")
    injected = restored / "_System/Cloud Tasks/Pending/injected.md"
    injected.parent.mkdir(parents=True)
    injected.write_text("unmanifested", encoding="utf-8")
    tampered = tmp_path / "obsidian-vault-20260714T200500Z.tar.gz"
    with tarfile.open(tampered, "w:gz") as archive:
        for path in sorted(restored.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(restored), recursive=False)

    valid, detail = validate_cloud_backup(tampered, settings.vault_path, now=NOW)
    assert valid is False
    assert "do not exactly match manifest" in detail


def test_cloud_maintenance_runs_end_to_end_and_skips_unchanged_second_run(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    source = configured.vault_path / "project.md"
    source.write_text("# Project\n\nDurable status.\n", encoding="utf-8")
    sync = FakeSync()
    agent = FakeAgent()

    first = run_cloud_maintenance(configured, client=sync, agent=agent, now=NOW)
    second = run_cloud_maintenance(
        configured, client=sync, agent=agent, now=NOW.replace(minute=5)
    )

    assert first["status"] == "ok"
    assert first["agent"]["status"] == "ok"
    assert second["agent"]["status"] == "skipped"
    assert second["agent"]["reason"] == "no source changes"
    assert agent.calls == 1
    assert sync.scans == 4
    assert sync.waits == 4
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()
    report = configured.vault_path / "_System/Cloud Reports/latest.md"
    assert report.exists()
    report_text = report.read_text(encoding="utf-8")
    report_metadata, _ = parse_frontmatter(report_text)
    assert "Source notes modified by model: 0" in report_text
    assert report_metadata["permalink"] == "codex-vault/system/cloud-reports/latest"
    assert not report_text.endswith("\n")
    assert len(list(configured.cloud_backup_dir.glob("*.tar.gz"))) == 2
    assert (configured.vault_path / ".git").exists()
    assert (
        json.loads((configured.state_dir / "cloud-state.json").read_text())["schema"]
        == 1
    )


def test_offline_analysis_stages_without_vault_writes_then_publishes_on_reconnect(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    source = configured.vault_path / "project.md"
    source.write_text("# Project\n\nDurable status.\n", encoding="utf-8")
    task = configured.vault_path / "_System/Cloud Tasks/Pending/review.md"
    task.parent.mkdir(parents=True)
    task.write_text(
        "---\ntitle: Review links\ntype: cloud-task\nstatus: pending\n---\n"
        "Review project links.\n",
        encoding="utf-8",
    )
    source_hash = source.read_bytes()

    def vault_tree() -> dict[str, bytes | None]:
        return {
            path.relative_to(configured.vault_path).as_posix(): (
                None if path.is_dir() else path.read_bytes()
            )
            for path in sorted(configured.vault_path.rglob("*"))
        }

    tree_before = vault_tree()
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    agent = FakeAgent()

    first = run_cloud_maintenance(configured, client=offline, agent=agent, now=NOW)
    second = run_cloud_maintenance(
        configured, client=offline, agent=agent, now=NOW.replace(minute=5)
    )

    assert first["status"] == "offline-staged"
    assert first["synced_vault_writes"] == 0
    assert first["agent"]["status"] == "staged"
    assert second["agent"]["status"] == "already-staged"
    assert agent.calls == 1
    assert vault_tree() == tree_before
    assert source.read_bytes() == source_hash
    assert task.exists()
    assert not (configured.vault_path / "_System/Cloud Reports/latest.md").exists()
    assert not (configured.vault_path / ".git").exists()
    assert (configured.state_dir / "cloud-staged-report.json").exists()

    connected = run_cloud_maintenance(
        configured, client=FakeSync(), agent=agent, now=NOW.replace(minute=10)
    )

    assert connected["status"] == "ok"
    assert connected["agent"]["reason"].startswith("published validated offline")
    assert agent.calls == 1
    assert not task.exists()
    assert (configured.vault_path / "_System/Cloud Reports/latest.md").exists()
    assert not (configured.state_dir / "cloud-staged-report.json").exists()


def test_reconnect_trigger_publishes_matching_stage_without_second_agent_call(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n", encoding="utf-8")
    agent = FakeAgent()
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    run_cloud_maintenance(configured, client=offline, agent=agent, now=NOW)

    result = run_cloud_reconcile(
        configured, client=FakeSync(), now=NOW.replace(minute=10)
    )

    assert result["status"] == "published"
    assert result["result"]["agent"]["reason"].startswith("published validated offline")
    assert agent.calls == 1
    assert not (configured.state_dir / "cloud-staged-report.json").exists()


def test_reconnect_trigger_is_rate_limited_before_sync_work(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.state_dir / "cloud-staged-report.json").write_text(
        json.dumps({"schema": 1, "staged_at": NOW.isoformat()}), encoding="utf-8"
    )
    (configured.state_dir / "cloud-reconnect-state.json").write_text(
        json.dumps({"last_attempt_at": NOW.isoformat()}), encoding="utf-8"
    )

    result = run_cloud_reconcile(
        configured, client=FakeSync(), now=NOW.replace(minute=5)
    )

    assert result["status"] == "rate-limited"


def test_reconnect_discards_stale_stage_and_reanalyzes_current_source(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    source = configured.vault_path / "project.md"
    source.write_text("# Project\n\nVersion one.\n", encoding="utf-8")
    agent = FakeAgent()
    offline = FakeSync(SyncSnapshot("idle", 0, 0, 0, 100, "valid", False))
    run_cloud_maintenance(configured, client=offline, agent=agent, now=NOW)
    source.write_text("# Project\n\nVersion two.\n", encoding="utf-8")

    result = run_cloud_maintenance(
        configured, client=FakeSync(), agent=agent, now=NOW.replace(minute=5)
    )

    assert result["agent"]["status"] == "ok"
    assert "reason" not in result["agent"]
    assert agent.calls == 2
    assert not (configured.state_dir / "cloud-staged-report.json").exists()


def test_pending_cloud_task_triggers_agent_and_moves_to_processed(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text(
        "# Project\n\nDurable status.\n", encoding="utf-8"
    )
    sync = FakeSync()
    agent = FakeAgent()
    run_cloud_maintenance(configured, client=sync, agent=agent, now=NOW)
    task = configured.vault_path / "_System/Cloud Tasks/Pending/review-links.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text(
        "---\ntitle: Review project links\ntype: cloud-task\nstatus: pending\n---\n"
        "Find missing links in the project notes.\n",
        encoding="utf-8",
    )

    result = run_cloud_maintenance(
        configured, client=sync, agent=agent, now=NOW.replace(minute=5)
    )

    assert result["agent"]["status"] == "ok"
    assert result["agent"]["tasks"] == ["review-links.md"]
    assert agent.tasks[0]["instruction"] == "Find missing links in the project notes."
    assert not task.exists()
    completed = configured.vault_path / (
        "_System/Cloud Tasks/Processed/2026-07-14--review-links.md"
    )
    assert completed.exists()
    completed_text = completed.read_text(encoding="utf-8")
    completed_metadata, _ = parse_frontmatter(completed_text)
    assert "status: completed" in completed_text
    assert completed_metadata["permalink"].endswith("/2026-07-14-review-links")
    assert not completed_text.endswith("\n")


def test_cloud_task_with_secret_is_rejected(settings: Settings) -> None:
    task = settings.vault_path / "_System/Cloud Tasks/Pending/unsafe.md"
    task.parent.mkdir(parents=True)
    task.write_text(
        "---\ntype: cloud-task\n---\nUse api_key=abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="apparent secret"):
        load_cloud_tasks(settings)


def benchmark_settings(settings: Settings, tmp_path: Path) -> Settings:
    configured = cloud_settings(settings, tmp_path)
    syncthing = tmp_path / "config.xml"
    syncthing.write_text(
        """<configuration>
<folder id="codex-obsidian-vault" path="vault" type="sendreceive">
  <versioning type="staggered"><param key="maxAge" val="31536000" /></versioning>
</folder>
<gui><address>127.0.0.1:8384</address><apikey>test-only</apikey></gui>
<options>
  <listenAddress>tcp://0.0.0.0:22000</listenAddress>
  <globalAnnounceEnabled>false</globalAnnounceEnabled>
  <localAnnounceEnabled>false</localAnnounceEnabled>
  <relaysEnabled>false</relaysEnabled>
  <natEnabled>false</natEnabled>
</options>
</configuration>
""",
        encoding="utf-8",
    )
    configured = replace(configured, syncthing_config_path=syncthing)
    (configured.vault_path / "note.md").write_text("# Note\n", encoding="utf-8")
    report = configured.vault_path / "_System/Cloud Reports/latest.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Report\n", encoding="utf-8")
    create_cloud_backup(
        configured.vault_path,
        configured.cloud_backup_dir,
        retention=2,
        now=NOW,
    )
    subprocess.run(["git", "-C", str(configured.vault_path), "init"], check=True)
    subprocess.run(
        ["git", "-C", str(configured.vault_path), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(configured.vault_path),
            "config",
            "user.email",
            "test@example.invalid",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(configured.vault_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(configured.vault_path), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
    )
    (configured.state_dir / "cloud-state.json").write_text(
        json.dumps(
            {
                "last_success_at": NOW.isoformat(),
                "agent": {"status": "ok"},
            }
        ),
        encoding="utf-8",
    )
    return configured


def test_cloud_benchmark_requires_80_and_all_critical_gates(
    settings: Settings, tmp_path: Path
) -> None:
    configured = benchmark_settings(settings, tmp_path)
    result = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW,
    )
    assert result["score"] == 100
    assert result["threshold"] == 80
    assert result["passed"] is True

    conflict = configured.vault_path / "note.sync-conflict-20260714.md"
    conflict.write_text("conflict", encoding="utf-8")
    subprocess.run(["git", "-C", str(configured.vault_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(configured.vault_path), "commit", "-m", "conflict"],
        check=True,
        capture_output=True,
    )
    failed = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW,
    )
    assert failed["score"] == 85
    assert failed["passed"] is False
    assert failed["failed_critical"] == ["no-sync-conflicts"]


def test_cloud_benchmark_accepts_recent_backup_when_vault_has_newer_work(
    settings: Settings, tmp_path: Path
) -> None:
    configured = benchmark_settings(settings, tmp_path)
    (configured.vault_path / "note.md").write_text(
        "changed after nightly backup", encoding="utf-8"
    )

    result = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW.replace(hour=21),
    )

    backup_case = next(
        case for case in result["cases"] if case["name"] == "restorable-backup"
    )
    assert backup_case["passed"] is True
    assert result["score"] == 90
    assert result["passed"] is True


def test_cloud_benchmark_rejects_partial_future_backup(
    settings: Settings, tmp_path: Path
) -> None:
    configured = benchmark_settings(settings, tmp_path)
    partial = configured.cloud_backup_dir / "obsidian-vault-20990101T000000Z.tar.gz"
    with tarfile.open(partial, "w:gz") as archive:
        obsolete = tmp_path / "obsolete.md"
        obsolete.write_text("obsolete", encoding="utf-8")
        archive.add(obsolete, arcname="obsolete.md")

    result = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW,
    )

    backup_case = next(
        case for case in result["cases"] if case["name"] == "restorable-backup"
    )
    assert backup_case["passed"] is False
    assert result["passed"] is False
    assert "restorable-backup" in result["failed_critical"]


def _systemd_unit(text: str) -> dict[tuple[str, str], list[str]]:
    directives: dict[tuple[str, str], list[str]] = {}
    section = ""
    continued = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        line = continued + line
        if line.endswith("\\"):
            continued = line[:-1] + " "
            continue
        continued = ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        key, separator, value = line.partition("=")
        if not section or not separator:
            raise ValueError("Invalid systemd directive")
        values = directives.setdefault((section, key.strip()), [])
        if value.strip():
            values.append(value.strip())
        else:
            values.clear()
    if continued:
        raise ValueError("Incomplete systemd continuation")
    return directives


def _cloud_retry_units() -> dict[str, str]:
    deploy = Path(__file__).parents[1] / "deploy/systemd-cloud"
    names = [
        f"obsidian-cloud-{job}{suffix}.service"
        for job in ("maintenance", "reconnect")
        for suffix in ("", "-failure", "-success")
    ] + ["obsidian-cloud-reconnect.timer"]
    return {name: (deploy / name).read_text(encoding="utf-8") for name in names}


def _assert_cloud_retry_contract(sources: dict[str, str]) -> None:
    units = {name: _systemd_unit(text) for name, text in sources.items()}
    maintenance = units["obsidian-cloud-maintenance.service"]
    assert int(maintenance.get(("Unit", "StartLimitBurst"), ["0"])[-1]) == 3
    assert maintenance.get(("Unit", "StartLimitIntervalSec")) == ["1h"]
    assert maintenance.get(("Service", "Restart")) == ["on-failure"]
    assert maintenance.get(("Service", "RestartSec")) == ["15min"]
    for job in ("maintenance", "reconnect"):
        service = f"obsidian-cloud-{job}"
        unit = units[f"{service}.service"]
        marker = f"/var/lib/obsidian-cloud/{job}.failed"
        assert unit.get(("Service", "Type")) == ["oneshot"]
        assert unit.get(("Unit", "OnFailure")) == [f"{service}-failure.service"]
        assert unit.get(("Unit", "OnSuccess")) == [f"{service}-success.service"]
        assert all(
            marker not in shlex.split(command)
            for command in unit.get(("Service", "ExecStartPre"), [])
        )
        failure = units[f"{service}-failure.service"]
        success = units[f"{service}-success.service"]
        assert failure.get(("Service", "Type")) == ["oneshot"]
        assert success.get(("Service", "Type")) == ["oneshot"]
        assert [
            shlex.split(command)
            for command in failure.get(("Service", "ExecStart"), [])
        ] == [
            ["/usr/bin/touch", marker],
        ]
        assert [
            shlex.split(command)
            for command in success.get(("Service", "ExecStart"), [])
        ] == [
            ["/usr/bin/rm", "-f", marker],
            ["/usr/bin/systemctl", "reset-failed", f"{service}.service"],
        ]
    timer = units["obsidian-cloud-reconnect.timer"]
    assert timer.get(("Timer", "OnUnitActiveSec")) == ["5min"]
    assert timer.get(("Timer", "Unit")) == ["obsidian-cloud-reconnect.service"]


def test_cloud_service_has_bounded_failure_retries() -> None:
    _assert_cloud_retry_contract(_cloud_retry_units())


@pytest.mark.parametrize("mutation", ["commented", "misplaced"])
@pytest.mark.parametrize(
    ("filename", "directive"),
    [
        ("obsidian-cloud-maintenance.service", "StartLimitBurst"),
        ("obsidian-cloud-maintenance.service", "Restart"),
        ("obsidian-cloud-maintenance.service", "RestartSec"),
        ("obsidian-cloud-maintenance.service", "OnFailure"),
        ("obsidian-cloud-maintenance.service", "OnSuccess"),
        ("obsidian-cloud-maintenance-failure.service", "ExecStart"),
        ("obsidian-cloud-maintenance-success.service", "ExecStart"),
        ("obsidian-cloud-reconnect.service", "OnFailure"),
        ("obsidian-cloud-reconnect.service", "OnSuccess"),
        ("obsidian-cloud-reconnect-failure.service", "ExecStart"),
        ("obsidian-cloud-reconnect-success.service", "ExecStart"),
        ("obsidian-cloud-reconnect.timer", "OnUnitActiveSec"),
        ("obsidian-cloud-reconnect.timer", "Unit"),
    ],
)
def test_cloud_retry_contract_rejects_inactive_directives(
    filename: str, directive: str, mutation: str
) -> None:
    sources = _cloud_retry_units()
    retained = []
    removed = []
    for line in sources[filename].splitlines():
        if line.partition("=")[0].strip() == directive:
            removed.append(line)
        else:
            retained.append(line)
    assert removed
    if mutation == "commented":
        retained.extend("# " + line for line in removed)
    else:
        retained.extend(["[WrongSection]", *removed])
    sources[filename] = "\n".join(retained)
    with pytest.raises(AssertionError):
        _assert_cloud_retry_contract(sources)


def test_systemd_unit_normalizes_comments_continuations_and_command_resets() -> None:
    unit = _systemd_unit(
        "[Service]\n"
        "# ExecStart=/usr/bin/false\n"
        "; ExecStart=/bin/false\n"
        "ExecStart=/usr/bin/false\n"
        "ExecStart=\n"
        "ExecStart=/usr/bin/echo \\\n"
        "  ready\n"
        "ExecStart=/usr/bin/true\n"
        "[Unit]\nRestart=always\n"
    )
    assert [shlex.split(command) for command in unit[("Service", "ExecStart")]] == [
        ["/usr/bin/echo", "ready"],
        ["/usr/bin/true"],
    ]
    assert ("Service", "Restart") not in unit


def test_cloud_benchmark_surfaces_previous_service_failure(
    settings: Settings, tmp_path: Path
) -> None:
    configured = benchmark_settings(settings, tmp_path)
    (configured.state_dir / "maintenance.failed").touch()
    result = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW,
    )
    scheduler = next(
        case for case in result["cases"] if case["name"] == "nightly-scheduler"
    )
    assert scheduler["passed"] is False
    assert scheduler["detail"]["failure_marker"] is True
    assert result["passed"] is False
    assert "nightly-scheduler" in result["failed_critical"]


def test_cloud_benchmark_surfaces_previous_reconnect_failure(
    settings: Settings, tmp_path: Path
) -> None:
    configured = benchmark_settings(settings, tmp_path)
    (configured.state_dir / "reconnect.failed").touch()
    result = run_cloud_benchmark(
        configured,
        client=FakeSync(),
        service_checker=lambda _property: True,
        now=NOW,
    )
    scheduler = next(
        case for case in result["cases"] if case["name"] == "nightly-scheduler"
    )
    assert scheduler["passed"] is False
    assert scheduler["detail"]["reconnect_failure_marker"] is True
    assert result["passed"] is False
    assert "nightly-scheduler" in result["failed_critical"]


def test_cloud_maintenance_refuses_pending_sync(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    sync = FakeSync(SyncSnapshot("syncing", 0, 1, 100, 50, "valid", True))
    with pytest.raises(RuntimeError, match="cloud preflight failed"):
        run_cloud_maintenance(configured, client=sync, agent=FakeAgent(), now=NOW)
    assert not list(configured.cloud_backup_dir.glob("*.tar.gz"))


def test_cloud_maintenance_does_not_archive_or_rotate_secret_vault(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    note = configured.vault_path / "note.md"
    note.write_text("clean", encoding="utf-8")
    clean = create_cloud_backup(
        configured.vault_path,
        configured.cloud_backup_dir,
        retention=1,
        now=NOW,
    )
    note.write_text("api_key=abcdefghijklmnopqrstuvwx", encoding="utf-8")

    with pytest.raises(ValueError, match="backup blocked by apparent secret"):
        run_cloud_maintenance(
            configured,
            client=FakeSync(),
            agent=FakeAgent(),
            now=NOW.replace(minute=5),
        )

    assert list(configured.cloud_backup_dir.glob("*.tar.gz")) == [clean]


def test_cloud_maintenance_yields_to_writer_that_appears_during_lease_sync(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    writer_path = configured.vault_path / "_System/Coordination/local-writer.json"

    class RacingSync(FakeSync):
        def wait_healthy(self, timeout_seconds: int) -> SyncSnapshot:
            writer_path.parent.mkdir(parents=True, exist_ok=True)
            writer_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "owner": "test-local-writer",
                        "expires_at": "2099-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            return super().wait_healthy(timeout_seconds)

    result = run_cloud_maintenance(
        configured, client=RacingSync(), agent=FakeAgent(), now=NOW
    )
    assert result["status"] == "deferred"
    assert result["reason"] == "local-writer-active"

    assert not list(configured.cloud_backup_dir.glob("*.tar.gz"))
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()


def test_cloud_maintenance_stops_for_conflict_created_during_lease_sync(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    conflict = configured.vault_path / "note.sync-conflict-20260714.md"

    class RacingSync(FakeSync):
        def wait_healthy(self, timeout_seconds: int) -> SyncSnapshot:
            conflict.write_text("competing replica", encoding="utf-8")
            return super().wait_healthy(timeout_seconds)

    with pytest.raises(RuntimeError, match="sync conflict appeared during"):
        run_cloud_maintenance(
            configured, client=RacingSync(), agent=FakeAgent(), now=NOW
        )

    assert not list(configured.cloud_backup_dir.glob("*.tar.gz"))
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()


def test_cloud_maintenance_fails_when_postflight_does_not_converge(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n", encoding="utf-8")

    class PostflightFailureSync(FakeSync):
        def wait_healthy(self, timeout_seconds: int) -> SyncSnapshot:
            result = super().wait_healthy(timeout_seconds)
            if self.waits == 2:
                return SyncSnapshot("syncing", 0, 1, 10, 99, "valid", True)
            return result

    with pytest.raises(RuntimeError, match="cloud postflight did not converge"):
        run_cloud_maintenance(
            configured, client=PostflightFailureSync(), agent=FakeAgent(), now=NOW
        )

    assert list(configured.cloud_backup_dir.glob("*.tar.gz"))
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()


def test_cloud_maintenance_fails_for_conflict_created_during_postflight(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n", encoding="utf-8")
    conflict = configured.vault_path / "note.sync-conflict-20260714.md"

    class PostflightConflictSync(FakeSync):
        def wait_healthy(self, timeout_seconds: int) -> SyncSnapshot:
            result = super().wait_healthy(timeout_seconds)
            if self.waits == 2:
                conflict.write_text("competing replica", encoding="utf-8")
            return result

    with pytest.raises(RuntimeError, match="conflict appeared during cloud postflight"):
        run_cloud_maintenance(
            configured, client=PostflightConflictSync(), agent=FakeAgent(), now=NOW
        )

    assert list(configured.cloud_backup_dir.glob("*.tar.gz"))
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()


def test_cloud_maintenance_fails_if_final_snapshot_degrades_after_wait(
    settings: Settings, tmp_path: Path
) -> None:
    configured = cloud_settings(settings, tmp_path)
    (configured.vault_path / "project.md").write_text("# Project\n", encoding="utf-8")

    class FinalSnapshotFailureSync(FakeSync):
        def __init__(self) -> None:
            super().__init__()
            self.snapshots = 0

        def snapshot(self) -> SyncSnapshot:
            self.snapshots += 1
            if self.snapshots == 3:
                return SyncSnapshot("syncing", 0, 2, 20, 98, "valid", True)
            return super().snapshot()

    with pytest.raises(RuntimeError, match="final snapshot is not complete"):
        run_cloud_maintenance(
            configured,
            client=FinalSnapshotFailureSync(),
            agent=FakeAgent(),
            now=NOW,
        )

    assert list(configured.cloud_backup_dir.glob("*.tar.gz"))
    assert not (
        configured.vault_path / "_System/Coordination/cloud-maintenance.json"
    ).exists()


def test_openrouter_agent_rejects_paths_outside_evidence(
    settings: Settings, monkeypatch
) -> None:
    configured = replace(settings, cloud_agent_enabled=True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-test-key")
    response = {
        "model": "test-model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Summary",
                            "organization_actions": [],
                            "suggested_links": [],
                            "quality_issues": [
                                {
                                    "path": "not-supplied.md",
                                    "issue": "Unsupported",
                                    "severity": "low",
                                }
                            ],
                            "topics": [],
                            "next_actions": [],
                        }
                    )
                }
            }
        ],
        "usage": {},
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(response).encode()

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/key"):
            return type(
                "KeyResponse",
                (),
                {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *_args: None,
                    "read": lambda self: json.dumps(
                        {
                            "data": {
                                "usage_daily": 0,
                                "usage_monthly": 0,
                                "limit_remaining": 10,
                            }
                        }
                    ).encode(),
                },
            )()
        return FakeResponse()

    monkeypatch.setattr("obsidian_sidecar.cloud.urllib.request.urlopen", urlopen)
    agent = OpenRouterCloudAgent(configured)
    with pytest.raises(ValueError, match="outside evidence"):
        agent.analyze([{"path": "supplied.md", "content": "# Supplied"}])


def test_cloud_agent_contract_requires_exact_evidence_paths() -> None:
    assert "must be copied exactly" in AGENT_SYSTEM_PROMPT
    assert "from an evidence[].path" in AGENT_SYSTEM_PROMPT
    actions = AGENT_SCHEMA["properties"]["organization_actions"]["items"]
    action_path = actions["properties"]["evidence_paths"]["items"]
    links = AGENT_SCHEMA["properties"]["suggested_links"]["items"]["properties"]
    issues = AGENT_SCHEMA["properties"]["quality_issues"]["items"]["properties"]

    for value in (action_path, links["source"], links["target"], issues["path"]):
        assert "Exact value copied from evidence[].path" == value["description"]


def test_openrouter_agent_canonicalizes_unique_evidence_basename(
    settings: Settings, monkeypatch
) -> None:
    configured = replace(settings, cloud_agent_enabled=True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-test-key")
    shortened = "60 Sessions/2026/session.md"
    canonical = "60 Sessions/2026/2026-07/session.md"
    response = {
        "model": "test-model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Summary",
                            "organization_actions": [
                                {
                                    "title": "Review",
                                    "rationale": "Current evidence.",
                                    "priority": "low",
                                    "evidence_paths": [shortened],
                                }
                            ],
                            "suggested_links": [],
                            "quality_issues": [],
                            "topics": [],
                            "next_actions": [],
                        }
                    )
                }
            }
        ],
        "usage": {},
    }

    class FakeResponse:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(self.value).encode()

    def urlopen(request, **_kwargs):
        if request.full_url.endswith("/key"):
            return FakeResponse(
                {
                    "data": {
                        "usage_daily": 0,
                        "usage_monthly": 0,
                        "limit_remaining": 10,
                    }
                }
            )
        return FakeResponse(response)

    monkeypatch.setattr("obsidian_sidecar.cloud.urllib.request.urlopen", urlopen)

    result = OpenRouterCloudAgent(configured).analyze(
        [{"path": canonical, "content": "# Session"}]
    )

    assert result["organization_actions"][0]["evidence_paths"] == [canonical]


def test_openrouter_agent_blocks_before_daily_spend_ceiling(
    settings: Settings, monkeypatch
) -> None:
    configured = replace(
        settings,
        cloud_agent_enabled=True,
        cloud_agent_daily_cost_limit_usd=0.25,
        cloud_agent_cost_reserve_usd=0.05,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "not-a-real-test-key")
    calls: list[str] = []

    class KeyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "data": {
                        "usage_daily": 0.21,
                        "usage_monthly": 0.21,
                        "limit_remaining": 10,
                    }
                }
            ).encode()

    def urlopen(request, **_kwargs):
        calls.append(request.full_url)
        return KeyResponse()

    monkeypatch.setattr("obsidian_sidecar.cloud.urllib.request.urlopen", urlopen)

    with pytest.raises(RuntimeError, match="daily spend ceiling"):
        OpenRouterCloudAgent(configured).analyze(
            [{"path": "supplied.md", "content": "# Supplied"}]
        )

    assert calls == ["https://openrouter.ai/api/v1/key"]
