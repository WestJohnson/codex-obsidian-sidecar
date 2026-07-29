from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import yaml

from obsidian_sidecar.knowledge import (
    _duplicate_candidates,
    assess_freshness,
    decision_id_for,
    migrate_knowledge,
    preview_decision_impact,
)
from obsidian_sidecar.maintenance import inspect_vault
from obsidian_sidecar.vault import (
    ensure_vault_layout,
    parse_frontmatter,
    write_curation,
)


def _packet(repo: Path, *, session_id: str = "session-1") -> dict:
    return {
        "session_id": session_id,
        "turn_id": "turn-1",
        "cwd": str(repo),
        "captured_at": "2026-07-20T05:00:00+00:00",
        "evidence": [
            {
                "id": "u1",
                "kind": "conversation",
                "role": "user",
                "text": "Use the reviewed update path.",
            },
            {
                "id": "g1",
                "kind": "git",
                "label": "Repository head",
                "text": "abcdef1 implement update path",
            },
        ],
        "artifacts": [],
    }


def _decision_curation(valid_curation: dict) -> dict:
    curation = deepcopy(valid_curation)
    curation["decisions"] = [
        {
            "text": "Use checksummed offline wheels for Sidecar updates.",
            "rationale": "This keeps exact-version installation reproducible.",
            "decision_type": "operator-decision",
            "evidence_ids": ["u1"],
        }
    ]
    curation["verification"] = [
        {
            "text": "Verified the update path with a clean installation.",
            "evidence_ids": ["u1"],
        }
    ]
    return curation


def test_session_write_adds_freshness_and_canonical_decision(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    artifact = repo / "docs" / "updates.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# Updates\n", encoding="utf-8")
    packet = _packet(repo)
    packet["artifacts"] = [
        {"label": "Update runbook", "path": str(artifact), "evidence_id": "u1"}
    ]

    result = write_curation(
        settings,
        _decision_curation(valid_curation),
        packet,
        review_required=False,
    )

    project_metadata, _ = parse_frontmatter(
        result.project_path.read_text(encoding="utf-8")
    )
    assert project_metadata["canonical_id"] == "project:rainbow-joes"
    assert project_metadata["freshness"]["verified_at"].startswith("2026-07-20")
    assert project_metadata["freshness"]["source_revision"] == "abcdef1"
    assert len(result.decision_paths) == 1
    decision_metadata, decision_body = parse_frontmatter(
        result.decision_paths[0].read_text(encoding="utf-8")
    )
    assert decision_metadata["type"] == "decision"
    assert decision_metadata["decision_id"].startswith("rainbow-joes/")
    assert decision_metadata["decision_type"] == "operator-decision"
    assert decision_metadata["authority"] == "operator"
    assert decision_metadata["status"] == "active"
    assert "repo:docs/updates.md" in decision_metadata["affects"]
    assert decision_metadata["freshness"]["verified_at"].startswith("2026-07-20")
    assert "operator approval" in decision_body
    assert str(
        result.decision_paths[0].relative_to(settings.vault_path).with_suffix("")
    ) in result.project_path.read_text(encoding="utf-8")


def test_project_identity_reuses_exact_source_cwd(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    repo.mkdir()
    first = write_curation(
        settings,
        valid_curation,
        _packet(repo, session_id="first"),
        review_required=False,
    )
    drifted = deepcopy(valid_curation)
    drifted["project_name"] = "Rainbow Site"
    drifted["project_slug"] = "rainbow-site"
    second = write_curation(
        settings,
        drifted,
        _packet(repo, session_id="second"),
        review_required=False,
    )

    assert first.project_path == second.project_path
    assert not (settings.vault_path / "10 Projects/rainbow-site").exists()
    second_metadata, _ = parse_frontmatter(second.note_path.read_text(encoding="utf-8"))
    assert second_metadata["project"] == "rainbow-joes"


def test_project_identity_does_not_collapse_generic_workspace(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    workspace = tmp_path / "Documents"
    workspace.mkdir()
    first = write_curation(
        settings,
        valid_curation,
        _packet(workspace, session_id="generic-first"),
        review_required=False,
    )
    second_curation = deepcopy(valid_curation)
    second_curation["project_name"] = "Different Project"
    second_curation["project_slug"] = "different-project"
    second = write_curation(
        settings,
        second_curation,
        _packet(workspace, session_id="generic-second"),
        review_required=False,
    )

    assert first.project_path != second.project_path
    assert second.project_path.parent.name == "different-project"


def test_freshness_states_are_computed_not_persisted(settings, tmp_path: Path) -> None:
    source = settings.vault_path / "session.md"
    source.write_text("# Source\n", encoding="utf-8")
    path = settings.vault_path / "10 Projects/example/Project.md"
    path.parent.mkdir(parents=True)
    metadata = {
        "title": "Example",
        "type": "project",
        "managed_by": "codex-obsidian-sidecar",
        "freshness": {
            "class": "project",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "verified_at": "2026-01-01T00:00:00+00:00",
            "review_after": "2026-01-31T00:00:00+00:00",
            "source": "vault:session.md",
            "verified_source": "vault:session.md",
        },
    }
    path.write_text(
        f"---\n{yaml.safe_dump(metadata, sort_keys=False).strip()}\n---\n# Example\n",
        encoding="utf-8",
    )

    finding = assess_freshness(
        path,
        settings.vault_path,
        metadata,
        now=datetime(2026, 2, 1, tzinfo=UTC),
    )

    assert finding is not None
    assert finding.state == "review-due"
    assert "status:" not in path.read_text(encoding="utf-8")


def test_decision_impact_is_read_only(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    repo.mkdir()
    result = write_curation(
        settings,
        _decision_curation(valid_curation),
        _packet(repo),
        review_required=False,
    )
    before = {path: path.read_bytes() for path in settings.vault_path.rglob("*.md")}
    metadata, _ = parse_frontmatter(
        result.decision_paths[0].read_text(encoding="utf-8")
    )

    preview = preview_decision_impact(settings, metadata["decision_id"])

    after = {path: path.read_bytes() for path in settings.vault_path.rglob("*.md")}
    assert preview["status"] == "ok"
    assert preview["read_only"] is True
    assert preview["blast_radius"]["affected_count"] == 0
    assert preview["blast_radius"]["direct"] == []
    assert any(
        item["relationship"] == "related-context"
        for item in preview["blast_radius"]["related"]
    )
    assert before == after


def test_legacy_migration_is_planned_applied_and_idempotent(settings) -> None:
    ensure_vault_layout(settings.vault_path)
    project = settings.vault_path / "10 Projects/legacy/Project.md"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        """---
title: Legacy
type: project
project: legacy
status: active
source_cwd: /tmp/legacy
managed_by: codex-obsidian-sidecar
---

# Legacy
""",
        encoding="utf-8",
    )
    session = settings.vault_path / "60 Sessions/2026/2026-07/legacy.md"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        """---
title: Legacy work
type: work-session
project: legacy
status: current
date: '2026-07-19'
updated: '2026-07-19T12:00:00+00:00'
source_cwd: /tmp/legacy
session_id: legacy-1
confidence: 0.9
managed_by: codex-obsidian-sidecar
---

# Legacy work

## Decisions

- Keep the canonical legacy workflow. _(evidence: `u1`)_
  - Rationale: The operator approved it.

## Verification

- Verified the workflow. _(evidence: `u1`)_

## Artifacts

- None recorded.
""",
        encoding="utf-8",
    )

    plan = migrate_knowledge(settings, apply=False)
    applied = migrate_knowledge(settings, apply=True)
    repeated = migrate_knowledge(settings, apply=True)

    assert plan["mutates"] is False
    assert plan["schema"] == 2
    assert plan["project_hubs_to_update"] == 1
    assert plan["decision_records_new"] == 1
    assert applied["decision_records_created"] == 1
    project_metadata, _ = parse_frontmatter(project.read_text(encoding="utf-8"))
    assert project_metadata["canonical_id"] == "project:legacy"
    assert project_metadata["freshness"]["verified_at"].startswith("2026-07-19")
    decision_id = decision_id_for("legacy", "Keep the canonical legacy workflow.")
    preview = preview_decision_impact(settings, decision_id)
    assert preview["status"] == "ok"
    assert preview["decision"]["decision_type"] == "operator-decision"
    assert preview["decision"]["authority"] == "operator"
    assert "SIDECAR:CURRENT-STATE:START" in project.read_text(encoding="utf-8")
    assert "SIDECAR:OPEN-WORK:START" in project.read_text(encoding="utf-8")
    assert repeated["decision_records_created"] == 0
    assert repeated["decision_records_updated"] == 0


def test_health_flags_review_due_freshness(settings) -> None:
    ensure_vault_layout(settings.vault_path)
    (settings.vault_path / "old.md").write_text("# Old source\n", encoding="utf-8")
    project = settings.vault_path / "10 Projects/stale/Project.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        """---
title: Stale
type: project
project: stale
canonical_id: project:stale
status: active
managed_by: codex-obsidian-sidecar
freshness:
  class: project
  observed_at: '2020-01-01T00:00:00+00:00'
  verified_at: '2020-01-01T00:00:00+00:00'
  review_after: '2020-02-01T00:00:00+00:00'
  source: vault:old.md
  verified_source: vault:old.md
---

# Stale
""",
        encoding="utf-8",
    )

    health = inspect_vault(settings)

    assert "10 Projects/stale/Project.md" in health.freshness_review_due
    assert health.warnings >= 1


def test_missing_vault_freshness_source_is_invalid(settings) -> None:
    ensure_vault_layout(settings.vault_path)
    project = settings.vault_path / "10 Projects/missing-source/Project.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        """---
title: Missing Source
type: project
project: missing-source
canonical_id: project:missing-source
status: active
managed_by: codex-obsidian-sidecar
freshness:
  class: project
  observed_at: '2026-07-20T00:00:00+00:00'
  verified_at: '2026-07-20T00:00:00+00:00'
  review_after: '2026-08-19T00:00:00+00:00'
  source: vault:60 Sessions/missing.md
  verified_source: vault:60 Sessions/missing.md
---

# Missing Source
""",
        encoding="utf-8",
    )

    health = inspect_vault(settings)

    assert "10 Projects/missing-source/Project.md" in health.freshness_invalid
    assert health.critical_failures >= 1


def test_migration_does_not_refresh_empty_project_from_its_own_write(settings) -> None:
    ensure_vault_layout(settings.vault_path)
    project = settings.vault_path / "10 Projects/empty/Project.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        """---
title: Empty
type: project
project: empty
status: active
source_cwd: /tmp/empty
managed_by: codex-obsidian-sidecar
---

# Empty
""",
        encoding="utf-8",
    )

    first = migrate_knowledge(settings, apply=True)
    first_payload = project.read_bytes()
    second = migrate_knowledge(settings, apply=True)

    assert first["projects_updated"] == 1
    assert second["projects_updated"] == 0
    assert project.read_bytes() == first_payload
    metadata, body = parse_frontmatter(project.read_text(encoding="utf-8"))
    assert metadata["current_state"]["phase"] == "no-session-history"
    assert metadata["current_state"]["open_items"] == 0
    assert "No managed session has been captured." in body


def test_high_confidence_duplicate_reuses_record_and_probable_duplicate_waits_for_review(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    repo.mkdir()
    first = write_curation(
        settings,
        _decision_curation(valid_curation),
        _packet(repo, session_id="decision-first"),
        review_required=False,
    )
    paraphrase = _decision_curation(valid_curation)
    paraphrase["decisions"][0][
        "text"
    ] = "Use checksummed offline wheels for reproducible Sidecar updates."
    second = write_curation(
        settings,
        paraphrase,
        _packet(repo, session_id="decision-paraphrase"),
        review_required=False,
    )
    probable = _decision_curation(valid_curation)
    probable["decisions"][0][
        "text"
    ] = "Use checksummed wheel files when installing Sidecar updates offline."
    third = write_curation(
        settings,
        probable,
        _packet(repo, session_id="decision-probable"),
        review_required=False,
    )

    assert second.decision_paths == first.decision_paths
    assert len(third.decision_paths) == 1
    assert third.decision_paths[0] != first.decision_paths[0]
    metadata, _ = parse_frontmatter(
        third.decision_paths[0].read_text(encoding="utf-8")
    )
    assert metadata["status"] == "needs-review"
    assert metadata["possible_duplicates"][0]["decision_id"].startswith(
        "rainbow-joes/"
    )
    project_text = third.project_path.read_text(encoding="utf-8")
    assert "## Decision Proposals and Reviews" in project_text
    assert third.decision_paths[0].stem in project_text


def test_terminal_decisions_do_not_trigger_duplicate_review(tmp_path: Path) -> None:
    statement = "Use checksummed wheel files for offline Sidecar updates."
    terminal = (
        tmp_path / "retired.md",
        {"decision_id": "example/retired", "status": "superseded"},
        "Use checksummed offline wheels for reproducible Sidecar updates.",
        "Retired in favor of a canonical record.",
    )
    active = (
        tmp_path / "active.md",
        {"decision_id": "example/active", "status": "active"},
        "Use signed archives for production Sidecar releases.",
        "This is a distinct release requirement.",
    )

    assert _duplicate_candidates([terminal], statement) == []
    assert _duplicate_candidates([terminal, active], statement) == []


def test_checkpoint_artifact_fingerprints_keep_direct_blast_radius_narrow(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    repo.mkdir()
    first_artifact = repo / "first.md"
    second_artifact = repo / "second.md"
    first_artifact.write_text("# First\n", encoding="utf-8")
    second_artifact.write_text("# Second\n", encoding="utf-8")
    first_text = "Use the first artifact for deployment checks."
    second_text = "Use the second artifact for rollback checks."

    def fingerprint(value: str) -> str:
        normalized = " ".join(value.strip().casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    curation = deepcopy(valid_curation)
    curation["decisions"] = [
        {
            "text": first_text,
            "rationale": "It records deployment evidence.",
            "decision_type": "implemented-choice",
            "evidence_ids": ["c1"],
        },
        {
            "text": second_text,
            "rationale": "It records rollback evidence.",
            "decision_type": "implemented-choice",
            "evidence_ids": ["c1"],
        },
    ]
    packet = _packet(repo, session_id="narrow-impact")
    packet["evidence"].append(
        {
            "id": "c1",
            "kind": "checkpoint",
            "text": "Previously validated state.",
        }
    )
    packet["artifacts"] = [
        {
            "label": "First",
            "path": str(first_artifact),
            "evidence_id": "c1",
            "decision_fingerprints": [fingerprint(first_text)],
        },
        {
            "label": "Second",
            "path": str(second_artifact),
            "evidence_id": "c1",
            "decision_fingerprints": [fingerprint(second_text)],
        },
    ]

    result = write_curation(settings, curation, packet, review_required=False)

    impacts = []
    for path in result.decision_paths:
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        impacts.append(metadata["impact"])
    assert sorted(len(item["direct"]) for item in impacts) == [1, 1]
    assert all(
        "10 Projects/rainbow-joes/Project.md" not in target
        for item in impacts
        for target in item["direct"]
    )
    assert all(len(item["related"]) == 1 for item in impacts)


def test_project_hub_surfaces_current_state_and_ranked_open_work(
    settings, valid_curation: dict, tmp_path: Path
) -> None:
    repo = tmp_path / "rainbow-joes"
    repo.mkdir()

    result = write_curation(
        settings,
        valid_curation,
        _packet(repo, session_id="project-state"),
        review_required=False,
    )

    metadata, body = parse_frontmatter(
        result.project_path.read_text(encoding="utf-8")
    )
    assert metadata["current_state"]["phase"] == "verification"
    assert metadata["current_state"]["open_items"] == 2
    assert metadata["current_state"]["blockers"] == 0
    assert "**Latest outcome:** The landing page" in body
    assert "## Ranked Open Work" in body
    assert "Production deployment remains pending." in body
    assert "Review and approve the production deployment." in body


def test_migration_conservatively_backfills_orphan_legacy_decision(
    settings,
) -> None:
    ensure_vault_layout(settings.vault_path)
    project = settings.vault_path / "10 Projects/orphan/Project.md"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        """---
title: Orphan
type: project
project: orphan
status: active
source_cwd: /tmp/orphan
managed_by: codex-obsidian-sidecar
---

# Orphan
""",
        encoding="utf-8",
    )
    decision = settings.vault_path / "40 Decisions/orphan/orphan-choice.md"
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text(
        """---
title: Retain the orphan choice
type: decision
decision_id: orphan/orphan-choice
project: orphan
status: active
created: '2026-07-01T00:00:00+00:00'
updated: '2026-07-01T00:00:00+00:00'
managed_by: codex-obsidian-sidecar
affects:
  - vault:10 Projects/orphan/Project.md
sources: []
supersedes: []
---

# Retain the orphan choice

## Decision

Retain the orphan choice.

## Rationale

No evidence survives to establish who authorized it.
""",
        encoding="utf-8",
    )

    first = migrate_knowledge(settings, apply=True)
    second = migrate_knowledge(settings, apply=True)

    metadata, body = parse_frontmatter(decision.read_text(encoding="utf-8"))
    assert first["decision_records_updated"] == 1
    assert second["decision_records_updated"] == 0
    assert metadata["decision_type"] == "legacy-unclassified"
    assert metadata["authority"] == "legacy"
    assert metadata["status"] == "needs-review"
    assert metadata["impact"]["direct"] == []
    assert metadata["impact"]["inferred"] == []
    assert metadata["impact"]["related"] == [
        "vault:10 Projects/orphan/Project.md"
    ]
    assert "## Duplicate Review" in body
    assert not inspect_vault(settings).missing_required_fields
