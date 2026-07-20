from copy import deepcopy
from pathlib import Path

from obsidian_sidecar.config import Settings
from obsidian_sidecar.vault import (
    parse_frontmatter,
    vault_permalink,
    write_curation,
    write_quarantine,
)


def packet(tmp_path: Path) -> dict:
    return {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "cwd": str(tmp_path / "rainbow-joes"),
        "captured_at": "2026-07-14T08:01:00Z",
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
    project = result.project_path.read_text(encoding="utf-8")
    assert (
        result.note_path.relative_to(settings.vault_path).with_suffix("").as_posix()
        in project
    )


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


def test_low_confidence_routes_to_review(
    settings: Settings, valid_curation: dict, tmp_path: Path
) -> None:
    result = write_curation(
        settings, valid_curation, packet(tmp_path), review_required=True
    )
    assert "Needs Review" in result.note_path.parts
    metadata, _ = parse_frontmatter(result.note_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "needs-review"


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
