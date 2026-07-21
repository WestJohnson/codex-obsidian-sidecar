from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .artifacts import render_artifact_bullets
from .config import Settings
from .security import contains_secret, redact_text


MANAGED_BY = "codex-obsidian-sidecar"
IGNORED_VAULT_PARTS = frozenset({".git", ".obsidian", ".stversions", ".trash"})
SESSION_BLOCK_START = "<!-- SIDECAR:SESSIONS:START -->"
SESSION_BLOCK_END = "<!-- SIDECAR:SESSIONS:END -->"
CANONICAL_REFERENCE_TYPES = frozenset(
    {"project", "decision", "runbook", "operational-instruction"}
)


@dataclass(frozen=True)
class WriteResult:
    note_path: Path
    project_path: Path
    review_required: bool
    created: bool
    decision_paths: tuple[Path, ...] = ()


def ensure_vault_layout(vault: Path) -> None:
    folders = (
        "00 Inbox/Needs Review",
        "10 Projects",
        "20 Areas",
        "30 Knowledge",
        "40 Decisions",
        "50 Runbooks",
        "60 Sessions",
        "90 Archive",
        "_System/Health/history",
        "_System/Knowledge",
        "_System/Cloud Reports",
        "_System/Cloud Tasks/Pending",
        "_System/Cloud Tasks/Processed",
        "_System/Coordination",
        "_System/Logs",
        "_System/Schemas",
        "_System/Search Tests",
        "_System/Quarantine",
    )
    for folder in folders:
        (vault / folder).mkdir(parents=True, exist_ok=True)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("unterminated YAML frontmatter")
    value = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be a mapping")
    return value, text[marker + 5 :]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise ValueError("project slug is empty after normalization")
    return slug[:64]


def vault_permalink(relative_path: Path, project: str = "codex-vault") -> str:
    segments: list[str] = []
    for raw in relative_path.with_suffix("").parts:
        cleaned = re.sub(r"[^a-z0-9]+", "-", raw.lstrip("_").casefold()).strip("-")
        if cleaned:
            segments.append(cleaned)
    return "/".join([project.strip("/"), *segments])


def _session_token(session_id: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9]", "", session_id)
    return token[-12:] or "unknown"


def _evidenced_bullets(
    items: list[dict[str, Any]], *, include_rationale: bool = False
) -> str:
    if not items:
        return "- None recorded."
    lines: list[str] = []
    for item in items:
        evidence = ", ".join(f"`{value}`" for value in item.get("evidence_ids", []))
        line = f"- {str(item.get('text', '')).strip()} _(evidence: {evidence})_"
        if include_rationale and str(item.get("rationale", "")).strip():
            line += f"\n  - Rationale: {str(item['rationale']).strip()}"
        lines.append(line)
    return "\n".join(lines)


def _render_note(
    curation: dict[str, Any], packet: dict[str, Any], review_required: bool
) -> str:
    from .knowledge import _git_revision

    session_id = str(packet.get("session_id") or "unknown")
    captured_at = str(packet.get("captured_at") or datetime.now(UTC).isoformat())
    date = captured_at[:10]
    project_slug = _safe_slug(str(curation["project_slug"]))
    metadata = {
        "title": str(curation["title"]).strip(),
        "type": "work-session",
        "project": project_slug,
        "status": "needs-review" if review_required else "current",
        "date": date,
        "updated": captured_at,
        "source_cwd": str(packet.get("cwd") or ""),
        "session_id": session_id,
        "turn_id": str(packet.get("turn_id") or ""),
        "confidence": round(float(curation.get("confidence", 0)), 3),
        "tags": ["work-session", *curation.get("topics", [])],
        "managed_by": MANAGED_BY,
    }
    source_revision = _git_revision(packet)
    if source_revision:
        metadata["source_revision"] = source_revision
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False).strip()
    project_link = f"[[10 Projects/{project_slug}/Project|{curation['project_name']}]]"
    body = f"""---
{frontmatter}
---

# {str(curation["title"]).strip()}

**Project:** {project_link}

## Summary

{str(curation.get("summary", "")).strip() or "No summary recorded."}

## Objective

{str(curation.get("objective", "")).strip() or "No objective recorded."}

## Outcome

{str(curation.get("outcome", "")).strip() or "No outcome recorded."}

## Decisions

{_evidenced_bullets(curation.get("decisions", []), include_rationale=True)}

## Changes

{_evidenced_bullets(curation.get("changes", []))}

## Verification

{_evidenced_bullets(curation.get("verification", []))}

## Unresolved

{_evidenced_bullets(curation.get("unresolved", []))}

## Next Actions

{_evidenced_bullets(curation.get("next_actions", []))}

## Artifacts

{render_artifact_bullets(packet)}

## Provenance

- Codex session: `{session_id}`
- Turn: `{packet.get("turn_id") or "unknown"}`
- Working directory: `{packet.get("cwd") or "unknown"}`
- Curated from user requests, final answers, and bounded Git metadata only.
"""
    if contains_secret(body):
        raise ValueError("rendered note contains an apparent secret")
    return body


def _project_page(project_name: str, project_slug: str, source_cwd: str) -> str:
    metadata = yaml.safe_dump(
        {
            "title": project_name,
            "type": "project",
            "project": project_slug,
            "canonical_id": f"project:{project_slug}",
            "status": "active",
            "source_cwd": source_cwd,
            "managed_by": MANAGED_BY,
        },
        sort_keys=False,
    ).strip()
    return f"""---
{metadata}
---

# {project_name}

## Current Context

Durable work history and decisions for this project.

## Sessions

{SESSION_BLOCK_START}
- No sessions recorded.
{SESSION_BLOCK_END}
"""


def _update_project_sessions(
    project_path: Path, vault: Path, project_slug: str
) -> None:
    session_links: list[tuple[str, str]] = []
    for path in vault.rglob("*.md"):
        if IGNORED_VAULT_PARTS.intersection(path.parts) or path == project_path:
            continue
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if (
            metadata.get("type") != "work-session"
            or metadata.get("project") != project_slug
        ):
            continue
        relative = path.relative_to(vault).with_suffix("")
        title = str(metadata.get("title") or path.stem)
        date = str(metadata.get("date") or "")
        session_links.append((date, f"- [[{relative.as_posix()}|{date} - {title}]]"))
    session_links.sort(reverse=True)
    replacement = (
        "\n".join(item[1] for item in session_links) or "- No sessions recorded."
    )
    text = project_path.read_text(encoding="utf-8")
    pattern = re.compile(
        re.escape(SESSION_BLOCK_START) + r".*?" + re.escape(SESSION_BLOCK_END),
        re.DOTALL,
    )
    block = f"{SESSION_BLOCK_START}\n{replacement}\n{SESSION_BLOCK_END}"
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        text = text.rstrip() + f"\n\n## Sessions\n\n{block}\n"
    _atomic_write(project_path, text)


def _duplicate_session_notes(
    vault: Path, target: Path, session_id: str
) -> list[tuple[Path, str]]:
    duplicates: list[tuple[Path, str]] = []
    for path in vault.rglob("*.md"):
        if path == target or IGNORED_VAULT_PARTS.intersection(path.parts):
            continue
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        if metadata.get("type") != "work-session":
            continue
        if str(metadata.get("session_id") or "") != session_id:
            continue
        project = str(metadata.get("project") or "").strip()
        duplicates.append((path, project))
    return sorted(duplicates, key=lambda item: str(item[0]))


def _replace_exact_reference(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_exact_reference(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_exact_reference(item, replacements)
            for key, item in value.items()
        }
    return value


def _retarget_session_wikilinks(
    body: str, old_relative: Path, new_relative: Path
) -> str:
    updated = body
    targets = (
        (
            old_relative.with_suffix("").as_posix(),
            new_relative.with_suffix("").as_posix(),
        ),
        (old_relative.as_posix(), new_relative.as_posix()),
    )
    for old_target, new_target in targets:
        pattern = re.compile(
            r"\[\["
            + re.escape(old_target)
            + r"(?P<heading>#[^|\]]+)?(?P<alias>\|[^\]]+)?\]\]"
        )

        def replacement(match: re.Match[str]) -> str:
            heading = match.group("heading") or ""
            alias = match.group("alias") or ""
            if alias == f"|{old_relative.stem}":
                alias = f"|{new_relative.stem}"
            return f"[[{new_target}{heading}{alias}]]"

        updated = pattern.sub(replacement, updated)
    return updated


def _contains_exact_reference(value: Any, references: set[str]) -> bool:
    if isinstance(value, str):
        return value in references
    if isinstance(value, list):
        return any(_contains_exact_reference(item, references) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_exact_reference(item, references) for item in value.values()
        )
    return False


def retarget_managed_session_references(
    vault: Path, old_path: Path, new_path: Path
) -> list[Path]:
    """Retarget exact canonical references for a controlled session-note move."""
    if not new_path.exists():
        raise ValueError(f"session move target does not exist: {new_path.name}")
    old_relative = old_path.relative_to(vault)
    new_relative = new_path.relative_to(vault)
    replacements = {
        f"vault:{old_relative.as_posix()}": f"vault:{new_relative.as_posix()}",
        f"vault:{old_relative.with_suffix('').as_posix()}": (
            f"vault:{new_relative.with_suffix('').as_posix()}"
        ),
    }
    changed: list[Path] = []
    for path in sorted(vault.rglob("*.md")):
        if IGNORED_VAULT_PARTS.intersection(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            metadata, body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            continue
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        if metadata.get("type") not in CANONICAL_REFERENCE_TYPES:
            continue
        updated_metadata = _replace_exact_reference(metadata, replacements)
        updated_body = _retarget_session_wikilinks(body, old_relative, new_relative)
        if updated_metadata == metadata and updated_body == body:
            continue
        frontmatter = yaml.safe_dump(
            updated_metadata, sort_keys=False, allow_unicode=False
        ).strip()
        content = f"---\n{frontmatter}\n---\n{updated_body}"
        if contains_secret(content):
            raise ValueError(
                f"retargeted session reference contains an apparent secret: {path.name}"
            )
        _atomic_write(path, content)
        verified_metadata, verified_body = parse_frontmatter(
            path.read_text(encoding="utf-8")
        )
        if _contains_exact_reference(verified_metadata, set(replacements)):
            raise ValueError(f"session reference read-back failed: {path.name}")
        if verified_body != _retarget_session_wikilinks(
            verified_body, old_relative, new_relative
        ):
            raise ValueError(f"session wikilink read-back failed: {path.name}")
        changed.append(path)
    return changed


def session_reference_paths(
    vault: Path, session_path: Path, *, exclude: set[Path] | None = None
) -> list[Path]:
    """Return notes that still contain a structured or wiki session reference."""
    ignored = exclude or set()
    relative = session_path.relative_to(vault)
    structured = {
        f"vault:{relative.as_posix()}",
        f"vault:{relative.with_suffix('').as_posix()}",
    }
    remaining: list[Path] = []
    for path in sorted(vault.rglob("*.md")):
        if path in ignored or IGNORED_VAULT_PARTS.intersection(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            metadata, body = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            metadata, body = {}, text
        has_structured = (
            metadata.get("managed_by") == MANAGED_BY
            and metadata.get("type") in CANONICAL_REFERENCE_TYPES
            and _contains_exact_reference(metadata, structured)
        )
        has_wikilink = body != _retarget_session_wikilinks(
            body, relative, Path("__retarget_probe__.md")
        )
        if has_structured or has_wikilink:
            remaining.append(path)
    return remaining


def write_curation(
    settings: Settings,
    curation: dict[str, Any],
    packet: dict[str, Any],
    *,
    review_required: bool,
) -> WriteResult:
    ensure_vault_layout(settings.vault_path)
    from .knowledge import (
        resolve_project_identity,
        update_project_metadata,
        upsert_decision_records,
    )

    requested_slug = _safe_slug(str(curation["project_slug"]))
    project_slug, resolved_name = resolve_project_identity(
        settings.vault_path,
        requested_slug,
        str(packet.get("cwd") or ""),
    )
    selected_curation = dict(curation)
    selected_curation["project_slug"] = project_slug
    if resolved_name:
        selected_curation["project_name"] = resolved_name
    session_id = str(packet.get("session_id") or "unknown")
    captured_at = str(packet.get("captured_at") or datetime.now(UTC).isoformat())
    date = captured_at[:10]
    year_month = date[:7]
    filename = f"{date}--{project_slug}--{_session_token(session_id)}.md"
    if review_required:
        note_path = settings.vault_path / "00 Inbox" / "Needs Review" / filename
    else:
        note_path = (
            settings.vault_path / "60 Sessions" / date[:4] / year_month / filename
        )
    created = not note_path.exists()
    duplicate_sessions = _duplicate_session_notes(
        settings.vault_path, note_path, session_id
    )
    affected_projects = {project for _, project in duplicate_sessions if project}
    _atomic_write(note_path, _render_note(selected_curation, packet, review_required))

    metadata, _ = parse_frontmatter(note_path.read_text(encoding="utf-8"))
    if (
        metadata.get("session_id") != session_id
        or metadata.get("managed_by") != MANAGED_BY
    ):
        raise ValueError("note read-back verification failed")
    project_path = settings.vault_path / "10 Projects" / project_slug / "Project.md"
    if not project_path.exists():
        _atomic_write(
            project_path,
            _project_page(
                str(curation["project_name"]),
                project_slug,
                str(packet.get("cwd") or ""),
            ),
        )
    update_project_metadata(
        settings,
        selected_curation,
        packet,
        session_path=note_path,
        project_path=project_path,
        trusted=not review_required,
    )
    decision_results: list[dict[str, Any]] = []
    if not review_required:
        decision_results = upsert_decision_records(
            settings,
            selected_curation,
            packet,
            session_path=note_path,
            project_path=project_path,
        )
    duplicate_paths = {path for path, _ in duplicate_sessions}
    for old_path, _ in duplicate_sessions:
        retarget_managed_session_references(settings.vault_path, old_path, note_path)
    blocking_references: dict[str, list[str]] = {}
    for old_path, _ in duplicate_sessions:
        remaining = session_reference_paths(
            settings.vault_path, old_path, exclude=duplicate_paths
        )
        if remaining:
            blocking_references[
                old_path.relative_to(settings.vault_path).as_posix()
            ] = [path.relative_to(settings.vault_path).as_posix() for path in remaining]
    if blocking_references:
        detail = "; ".join(
            f"{source}: {', '.join(paths)}"
            for source, paths in blocking_references.items()
        )
        raise ValueError(
            "refusing to remove a moved session with remaining references: " + detail
        )
    for old_path, _ in duplicate_sessions:
        old_path.unlink()
    _update_project_sessions(project_path, settings.vault_path, project_slug)
    for affected_project in affected_projects - {project_slug}:
        affected_path = (
            settings.vault_path / "10 Projects" / affected_project / "Project.md"
        )
        if affected_path.exists():
            _update_project_sessions(
                affected_path, settings.vault_path, affected_project
            )
    return WriteResult(
        note_path,
        project_path,
        review_required,
        created,
        tuple(Path(item["path"]) for item in decision_results),
    )


def write_quarantine(
    settings: Settings,
    *,
    session_id: str,
    reason: str,
    curation: dict[str, Any] | None = None,
) -> Path:
    ensure_vault_layout(settings.vault_path)
    safe_session = _session_token(session_id)
    target = settings.vault_path / "_System" / "Quarantine" / f"{safe_session}.json"
    safe_reason, _ = redact_text(reason)
    payload = {"session_id": session_id, "reason": safe_reason, "curation": curation}
    serialized = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    if contains_secret(serialized):
        payload["curation"] = None
        payload["reason"] = f"{safe_reason}; curation omitted after secret detection"
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write(target, serialized)
    return target
