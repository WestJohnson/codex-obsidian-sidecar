from pathlib import Path
from dataclasses import replace

from obsidian_sidecar.config import Settings
from obsidian_sidecar.coordination import CloudLease, local_writer_status
from obsidian_sidecar.maintenance import (
    basic_memory_status,
    inspect_vault,
    write_health_report,
)
from obsidian_sidecar.worker import run_maintenance
from obsidian_sidecar.vault import write_curation


def packet(tmp_path: Path) -> dict:
    return {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }


def test_clean_managed_vault_has_no_content_failures(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    write_curation(settings, valid_curation, packet(tmp_path), review_required=False)
    health = inspect_vault(settings)
    assert health.managed_notes == 3
    assert health.valid_managed_notes == 3
    assert not health.malformed_frontmatter
    assert not health.missing_required_fields
    assert not health.unresolved_links
    assert not health.orphan_managed_notes
    assert not health.duplicate_session_ids


def test_doctor_detects_broken_link_and_secret(settings: Settings) -> None:
    note = settings.vault_path / "bad.md"
    fake_key = "sk-api-" + "abcdefghijklmnopqrstuvwxyz123456"
    note.write_text(
        f"---\ntitle: Bad\ntype: note\n---\n[[missing-target]]\n{fake_key}\n",
        encoding="utf-8",
    )
    health = inspect_vault(settings)
    assert "bad.md" in health.unresolved_links
    assert "bad.md" in health.possible_secret_files
    assert health.score < 80
    latest, history = write_health_report(settings, health)
    assert latest.exists() and history.exists()
    latest_text = latest.read_text(encoding="utf-8")
    assert "ATTENTION" in latest_text
    assert "permalink: codex-vault/system/health/latest" in latest_text
    assert not latest_text.endswith("\n")


def test_maintenance_serializes_derived_health_fields(settings: Settings) -> None:
    result = run_maintenance(settings, backup=False)
    assert result["critical_failures"] == 0
    assert result["warnings"] == 0
    assert isinstance(result["score"], int)


def test_cloud_health_uses_separate_report_paths(settings: Settings) -> None:
    cloud = replace(settings, runtime_role="cloud")
    health = inspect_vault(cloud)

    latest, history = write_health_report(cloud, health)

    assert latest.name == "cloud-latest.md"
    assert history.name.startswith("cloud-")
    assert not (settings.vault_path / "_System/Health/latest.md").exists()


def test_local_maintenance_holds_writer_lease_while_writing(
    settings: Settings, monkeypatch
) -> None:
    from obsidian_sidecar import worker

    original = worker.write_health_report

    def guarded_write(*args, **kwargs):
        assert local_writer_status(settings.vault_path)[0] is True
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "write_health_report", guarded_write)
    result = run_maintenance(settings, backup=False)
    assert result["critical_failures"] == 0
    assert local_writer_status(settings.vault_path)[0] is False


def test_local_maintenance_defers_while_cloud_lease_is_active(
    settings: Settings,
) -> None:
    with CloudLease(settings.vault_path, ttl_seconds=600):
        result = run_maintenance(settings, backup=False)
    assert result["backup_result"] == "deferred"
    assert result["deferred_reason"] == "cloud-maintenance-active"


def test_missing_basic_memory_is_not_reported_healthy(
    settings: Settings, monkeypatch
) -> None:
    monkeypatch.setattr(
        "obsidian_sidecar.maintenance.basic_memory_binary", lambda: None
    )
    assert basic_memory_status(settings) == "unavailable"


def test_doctor_excludes_syncthing_version_history(settings: Settings) -> None:
    archived = settings.vault_path / ".stversions/archived.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "[[missing-target]]\napi_key=abcdefghijklmnopqrstuvwx\n",
        encoding="utf-8",
    )
    health = inspect_vault(settings)
    assert health.markdown_files == 0
    assert not health.unresolved_links
    assert not health.possible_secret_files
