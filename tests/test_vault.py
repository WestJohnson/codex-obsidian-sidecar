from copy import deepcopy
from pathlib import Path

import pytest

from obsidian_sidecar.config import Settings
from obsidian_sidecar.vault import (
    parse_frontmatter,
    vault_permalink,
    write_curation,
    write_quarantine,
)


def packet(
    tmp_path: Path,
    *,
    captured_at: str = "2026-07-14T08:01:00Z",
    cwd: Path | None = None,
) -> dict:
    return {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "cwd": str(cwd or tmp_path / "rainbow-joes"),
        "captured_at": captured_at,
    }


def test_vault_permalink_matches_basic_memory_normalization() -> None:
    assert (
        vault_permalink(
            Path("_System/Cloud Tasks/Processed/2026-07-14--Review Links.md")
        )
        == "codex-vault/system/cloud-tasks/processed/2026-07-14-review-links"
    )


def test_atomic_write_and_readback(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    result = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=False
    )
    assert result.created
    assert result.note_path.exists()
    metadata, body = parse_frontmatter(result.note_path.read_text(encoding="utf-8"))
    assert metadata["session_id"] == "fixture-session-001"
    assert metadata["project"] == "rainbow-joes"
    assert "REDACTED_SECRET" not in body
    assert "## Artifacts\n\n- None recorded." in body
    assert "Type: `implemented-choice`" in body
    project = result.project_path.read_text(encoding="utf-8")
    assert (
        result.note_path.relative_to(settings.vault_path).with_suffix("").as_posix()
        in project
    )


def test_session_note_records_model_provenance(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    value = packet(tmp_path)
    value["model_provenance"] = {
        "model": "gpt-test-model",
        "provider": "openai",
        "effort": "high",
        "harness": "codex-tui",
    }

    result = write_curation(settings, valid_curation, value, review_required=False)

    metadata, body = parse_frontmatter(
        result.note_path.read_text(encoding="utf-8")
    )
    assert metadata["model_provenance"]["model"] == "gpt-test-model"
    assert metadata["model_provenance"]["effort"] == "high"
    assert "Model: `gpt-test-model` via `codex-tui` at effort `high`" in body


def test_session_note_preserves_canonical_artifact_link(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    artifact = tmp_path / "release report.md"
    artifact.write_text("# Release report\n", encoding="utf-8")
    value = packet(tmp_path)
    value["artifacts"] = [
        {"label": "Release report", "path": str(artifact), "evidence_id": "a1"}
    ]

    result = write_curation(settings, valid_curation, value, review_required=False)

    text = result.note_path.read_text(encoding="utf-8")
    assert f"[Release report](<{artifact}>)" in text


def test_same_session_updates_without_duplicate(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    first = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=False
    )
    changed = deepcopy(valid_curation)
    changed["summary"] = "Updated summary after additional work."
    second = write_curation(settings, changed, packet(tmp_path), review_required=False)
    assert first.note_path == second.note_path
    assert not second.created
    sessions = list((settings.vault_path / "60 Sessions").rglob("*.md"))
    assert sessions == [first.note_path]
    assert "Updated summary" in first.note_path.read_text(encoding="utf-8")


def test_same_session_move_retargets_canonical_references(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    workspace = tmp_path / "Documents"
    workspace.mkdir()
    first = write_curation(
        settings,
        valid_curation,
        packet(
            tmp_path,
            captured_at="2026-07-20T23:55:00+00:00",
            cwd=workspace,
        ),
        review_required=False,
    )
    old_path = first.note_path
    old_relative = old_path.relative_to(settings.vault_path)
    old_source = f"vault:{old_relative.as_posix()}"
    old_decision = first.decision_paths[0]
    old_project = first.project_path

    changed = deepcopy(valid_curation)
    changed["project_name"] = "Rainbow Site"
    changed["project_slug"] = "rainbow-site"
    changed["title"] = "Rainbow Site follow-up"
    changed["decisions"] = [
        {
            "text": "Keep the follow-up under the new project identity.",
            "rationale": "The later capture uses the reviewed project name.",
            "decision_type": "implemented-choice",
            "evidence_ids": ["a1"],
        }
    ]
    second = write_curation(
        settings,
        changed,
        packet(
            tmp_path,
            captured_at="2026-07-21T00:05:00+00:00",
            cwd=workspace,
        ),
        review_required=False,
    )
    new_relative = second.note_path.relative_to(settings.vault_path)
    new_source = f"vault:{new_relative.as_posix()}"

    assert second.note_path != old_path
    assert not old_path.exists()
    sessions = []
    for path in settings.vault_path.rglob("*.md"):
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if metadata.get("type") == "work-session" and (
            metadata.get("session_id") == "fixture-session-001"
        ):
            sessions.append(path)
    assert sessions == [second.note_path]

    decision_metadata, decision_body = parse_frontmatter(
        old_decision.read_text(encoding="utf-8")
    )
    assert decision_metadata["sources"] == [new_source]
    assert decision_metadata["freshness"]["source"] == new_source
    assert decision_metadata["freshness"]["verified_source"] == new_source
    assert old_source not in old_decision.read_text(encoding="utf-8")
    assert new_relative.with_suffix("").as_posix() in decision_body

    project_metadata, project_body = parse_frontmatter(
        old_project.read_text(encoding="utf-8")
    )
    assert project_metadata["freshness"]["source"] == new_source
    assert project_metadata["freshness"]["verified_source"] == new_source
    assert old_relative.with_suffix("").as_posix() not in project_body

    from obsidian_sidecar.maintenance import inspect_vault

    health = inspect_vault(settings)
    assert not health.unresolved_links
    assert not health.duplicate_session_ids
    assert not health.stale_project_indexes
    assert not health.freshness_invalid


def test_session_move_keeps_old_note_when_unmanaged_reference_blocks_deletion(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    workspace = tmp_path / "Documents"
    workspace.mkdir()
    first = write_curation(
        settings,
        valid_curation,
        packet(
            tmp_path,
            captured_at="2026-07-20T23:55:00+00:00",
            cwd=workspace,
        ),
        review_required=False,
    )
    old_relative = first.note_path.relative_to(settings.vault_path).with_suffix("")
    manual = settings.vault_path / "30 Knowledge/manual.md"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual_text = f"# Manual\n\n- [[{old_relative.as_posix()}|Original session]]\n"
    manual.write_text(manual_text, encoding="utf-8")

    changed = deepcopy(valid_curation)
    changed["project_name"] = "Rainbow Site"
    changed["project_slug"] = "rainbow-site"
    with pytest.raises(ValueError, match="remaining references"):
        write_curation(
            settings,
            changed,
            packet(
                tmp_path,
                captured_at="2026-07-21T00:05:00+00:00",
                cwd=workspace,
            ),
            review_required=False,
        )

    expected_new = list(
        (settings.vault_path / "60 Sessions/2026/2026-07").glob(
            "2026-07-21--rainbow-site--*.md"
        )
    )
    assert first.note_path.exists()
    assert len(expected_new) == 1
    assert manual.read_text(encoding="utf-8") == manual_text


def test_session_write_does_not_delete_syncthing_versions(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    archived = settings.vault_path / ".stversions/archived-session.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "---\nmanaged_by: codex-obsidian-sidecar\n"
        "session_id: fixture-session-001\n---\n# Archived\n",
        encoding="utf-8",
    )
    write_curation(settings, valid_curation, packet(tmp_path), review_required=False)
    assert archived.exists()


def test_session_cleanup_ignores_managed_non_session_notes(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    sentinel = settings.vault_path / "30 Knowledge/sentinel.md"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        """---
title: Sentinel
type: knowledge
managed_by: codex-obsidian-sidecar
session_id: fixture-session-001
---

# Sentinel
""",
        encoding="utf-8",
    )

    write_curation(settings, valid_curation, packet(tmp_path), review_required=False)

    assert sentinel.exists()


def test_low_confidence_routes_to_review(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    result = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=True
    )
    assert "Needs Review" in result.note_path.parts
    metadata, _ = parse_frontmatter(result.note_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "needs-review"
    project_metadata, _ = parse_frontmatter(
        result.project_path.read_text(encoding="utf-8")
    )
    assert project_metadata["freshness"]["observed_at"].startswith("2026-07-14")
    assert "verified_at" not in project_metadata["freshness"]


def test_confidence_route_change_keeps_one_session_note(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    review = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=True
    )
    current = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=False
    )

    assert not review.note_path.exists()
    assert current.note_path.exists()
    matching = []
    for path in settings.vault_path.rglob("*.md"):
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        if metadata.get("session_id") == "fixture-session-001":
            matching.append(path)
    assert matching == [current.note_path]
    project = current.project_path.read_text(encoding="utf-8")
    assert (
        review.note_path.relative_to(settings.vault_path).with_suffix("").as_posix()
        not in project
    )
    assert (
        current.note_path.relative_to(settings.vault_path).with_suffix("").as_posix()
        in project
    )


def test_quarantine_redacts_secrets_from_reason_and_curation(
    settings: Settings,
) -> None:
    secret = "api_key=abcdefghijklmnopqrstuvwx"
    target = write_quarantine(
        settings,
        session_id="session-secret",
        reason=f"invalid output included {secret}",
        curation={"unsafe": secret},
    )

    text = target.read_text(encoding="utf-8")
    assert "abcdefghijklmnopqrstuvwx" not in text
    assert "[REDACTED_SECRET]" in text
    assert '"curation": null' in text
