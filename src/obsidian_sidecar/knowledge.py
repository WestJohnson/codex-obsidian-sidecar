from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .artifacts import local_markdown_links
from .security import contains_secret

if TYPE_CHECKING:
    from .config import Settings


FRESHNESS_CLASSES = frozenset({"durable", "project", "runtime"})
FRESHNESS_NOTE_TYPES = frozenset(
    {"project", "decision", "runbook", "operational-instruction"}
)
DECISION_BLOCK_START = "<!-- SIDECAR:DECISIONS:START -->"
DECISION_BLOCK_END = "<!-- SIDECAR:DECISIONS:END -->"
DECISION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,79}$")
DERIVED_REFERENCE_PREFIXES = (
    "_System/Cloud Reports/",
    "_System/Coordination/",
    "_System/Health/",
    "_System/Knowledge/",
    "_System/Logs/",
)
GENERIC_WORKSPACE_NAMES = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "projects",
        "repos",
        "workspace",
        "workspaces",
    }
)


@dataclass(frozen=True)
class FreshnessFinding:
    path: str
    note_type: str
    title: str
    state: str
    freshness_class: str | None
    observed_at: str | None
    verified_at: str | None
    review_after: str | None
    source: str | None
    source_revision: str | None
    managed: bool
    detail: str


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(parsed)


def _latest_timestamp(*values: Any) -> str | None:
    parsed = [item for item in (_parse_datetime(value) for value in values) if item]
    return max(parsed).isoformat() if parsed else None


def _slug(value: str, *, maximum: int = 64) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return cleaned[:maximum].rstrip("-") or "decision"


def _decision_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip()).casefold()
    digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
    stem = _slug(normalized, maximum=62)
    return f"{stem}-{digest}"[:80].rstrip("-")


def decision_id_for(project: str, text: str) -> str:
    return f"{_slug(project)}/{_decision_key(text)}"


def _frontmatter_text(metadata: dict[str, Any], body: str) -> str:
    serialized = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{serialized}\n---\n{body}"


def _write_frontmatter(path: Path, metadata: dict[str, Any], body: str) -> bool:
    from .vault import _atomic_write

    content = _frontmatter_text(metadata, body)
    if contains_secret(content):
        raise ValueError(f"knowledge write contains an apparent secret: {path.name}")
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    _atomic_write(path, content)
    return True


def _default_policy(settings: Settings, note_type: str) -> tuple[str, int | None]:
    if note_type == "project":
        return "project", settings.freshness_project_days
    if note_type == "decision":
        return "project", settings.freshness_decision_days
    return "runtime", settings.freshness_runbook_days


def _merge_freshness(
    existing: Any,
    *,
    settings: Settings,
    note_type: str,
    observed_at: str,
    verified_at: str | None,
    source: str,
    verified_source: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    current = dict(existing) if isinstance(existing, dict) else {}
    default_class, default_days = _default_policy(settings, note_type)
    freshness_class = str(current.get("class") or default_class)
    if freshness_class not in FRESHNESS_CLASSES:
        freshness_class = default_class

    merged_observed = _latest_timestamp(current.get("observed_at"), observed_at)
    merged_verified = _latest_timestamp(current.get("verified_at"), verified_at)
    envelope: dict[str, Any] = {
        "class": freshness_class,
        "observed_at": merged_observed or observed_at,
    }
    if merged_verified:
        envelope["verified_at"] = merged_verified

    existing_observed = _parse_datetime(current.get("observed_at"))
    incoming_observed = _parse_datetime(observed_at)
    if incoming_observed and (
        existing_observed is None or incoming_observed >= existing_observed
    ):
        envelope["source"] = source
        if source_revision:
            envelope["source_revision"] = source_revision
    else:
        envelope["source"] = str(current.get("source") or source)
        if current.get("source_revision"):
            envelope["source_revision"] = str(current["source_revision"])

    if merged_verified:
        existing_verified = _parse_datetime(current.get("verified_at"))
        incoming_verified = _parse_datetime(verified_at)
        if incoming_verified and (
            existing_verified is None or incoming_verified >= existing_verified
        ):
            envelope["verified_source"] = verified_source or source
        elif current.get("verified_source"):
            envelope["verified_source"] = str(current["verified_source"])

    if freshness_class != "durable":
        try:
            ttl_days = max(1, int(current.get("ttl_days", default_days or 1)))
        except (TypeError, ValueError):
            ttl_days = default_days or 1
        envelope["ttl_days"] = ttl_days
        base = _parse_datetime(merged_verified or merged_observed)
        if base:
            envelope["review_after"] = (base + timedelta(days=ttl_days)).isoformat()
    return envelope


def assess_freshness(
    path: Path,
    vault: Path,
    metadata: dict[str, Any],
    *,
    now: datetime | None = None,
) -> FreshnessFinding | None:
    from .vault import MANAGED_BY

    note_type = str(metadata.get("type") or "")
    if note_type not in FRESHNESS_NOTE_TYPES:
        return None
    relative = path.relative_to(vault).as_posix()
    title = str(metadata.get("title") or path.stem)
    managed = metadata.get("managed_by") == MANAGED_BY
    envelope = metadata.get("freshness")
    if envelope is None:
        return FreshnessFinding(
            relative,
            note_type,
            title,
            "unknown",
            None,
            None,
            None,
            None,
            None,
            None,
            managed,
            "freshness envelope is missing",
        )
    if not isinstance(envelope, dict):
        return FreshnessFinding(
            relative,
            note_type,
            title,
            "invalid",
            None,
            None,
            None,
            None,
            None,
            None,
            managed,
            "freshness envelope must be a mapping",
        )

    freshness_class = str(envelope.get("class") or "")
    observed = _parse_datetime(envelope.get("observed_at"))
    verified = _parse_datetime(envelope.get("verified_at"))
    review_after = _parse_datetime(envelope.get("review_after"))
    source = str(envelope.get("source") or "") or None
    verified_source = str(envelope.get("verified_source") or "") or None
    revision = str(envelope.get("source_revision") or "") or None
    invalid: list[str] = []
    if freshness_class not in FRESHNESS_CLASSES:
        invalid.append("unsupported freshness class")
    if observed is None:
        invalid.append("observed_at is missing or invalid")
    if freshness_class != "durable" and review_after is None:
        invalid.append("review_after is missing or invalid")
    if source is None:
        invalid.append("source is missing")
    elif source.startswith("vault:") and not _vault_reference_exists(vault, source):
        invalid.append("source does not resolve")
    if verified is not None and verified_source is None:
        invalid.append("verified_source is missing")
    elif (
        verified_source
        and verified_source.startswith("vault:")
        and not (_vault_reference_exists(vault, verified_source))
    ):
        invalid.append("verified_source does not resolve")
    if invalid:
        state = "invalid"
        detail = "; ".join(invalid)
    elif metadata.get("status") == "superseded":
        state = "superseded"
        detail = "decision is superseded"
    elif review_after and _utc(now or datetime.now(UTC)) > review_after:
        state = "review-due"
        detail = f"review was due {review_after.isoformat()}"
    elif verified is None:
        state = "unverified"
        detail = "observed but not explicitly verified"
    else:
        state = "current"
        detail = f"verified {verified.isoformat()}"
    return FreshnessFinding(
        relative,
        note_type,
        title,
        state,
        freshness_class or None,
        observed.isoformat() if observed else None,
        verified.isoformat() if verified else None,
        review_after.isoformat() if review_after else None,
        source,
        revision,
        managed,
        detail,
    )


def _vault_reference_exists(vault: Path, reference: str) -> bool:
    relative = reference.removeprefix("vault:").strip()
    if not relative:
        return False
    candidate = vault / relative
    if not candidate.suffix:
        candidate = candidate.with_suffix(".md")
    try:
        candidate.resolve(strict=False).relative_to(vault.resolve(strict=False))
    except ValueError:
        return False
    return candidate.exists()


def scan_freshness(
    vault: Path, *, now: datetime | None = None
) -> list[FreshnessFinding]:
    from .vault import IGNORED_VAULT_PARTS, parse_frontmatter

    findings: list[FreshnessFinding] = []
    for path in sorted(vault.rglob("*.md")):
        if IGNORED_VAULT_PARTS.intersection(path.parts):
            continue
        try:
            metadata, _ = parse_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ValueError, yaml.YAMLError):
            continue
        finding = assess_freshness(path, vault, metadata, now=now)
        if finding:
            findings.append(finding)
    return findings


def _git_revision(packet: dict[str, Any]) -> str | None:
    for item in packet.get("evidence", []):
        if not isinstance(item, dict) or item.get("kind") != "git":
            continue
        if item.get("label") != "Repository head":
            continue
        match = re.match(r"([0-9a-f]{7,40})(?:\s|$)", str(item.get("text") or ""))
        if match:
            return match.group(1)
    return None


def resolve_project_identity(
    vault: Path, requested_slug: str, source_cwd: str
) -> tuple[str, str | None]:
    """Resolve only exact, deterministic identities; never merge by fuzzy naming."""
    from .vault import MANAGED_BY, parse_frontmatter

    requested = _slug(requested_slug)
    direct = vault / "10 Projects" / requested / "Project.md"
    if direct.exists():
        return requested, None
    normalized_cwd = str(Path(source_cwd).expanduser().resolve(strict=False))
    if Path(normalized_cwd).name.casefold() in GENERIC_WORKSPACE_NAMES:
        return requested, None
    matches: list[tuple[str, str]] = []
    for path in sorted((vault / "10 Projects").glob("*/Project.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        existing_cwd = str(metadata.get("source_cwd") or "").strip()
        if not existing_cwd:
            continue
        normalized_existing = str(Path(existing_cwd).expanduser().resolve(strict=False))
        if normalized_existing == normalized_cwd:
            matches.append(
                (
                    _slug(str(metadata.get("project") or path.parent.name)),
                    str(metadata.get("title") or path.parent.name),
                )
            )
    if len(matches) == 1:
        return matches[0]
    return requested, None


def _verification_evidence(curation: dict[str, Any]) -> set[str]:
    return {
        str(evidence_id)
        for item in curation.get("verification", [])
        if isinstance(item, dict)
        for evidence_id in item.get("evidence_ids", [])
    }


def _decision_verified(decision: dict[str, Any], curation: dict[str, Any]) -> bool:
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    return bool(evidence & _verification_evidence(curation)) or any(
        value.startswith("u") for value in evidence
    )


def _relationship_target(path_value: str, cwd: Path, vault: Path) -> str:
    path = Path(path_value).expanduser().resolve(strict=False)
    try:
        return "vault:" + path.relative_to(vault.resolve()).as_posix()
    except ValueError:
        pass
    try:
        return "repo:" + path.relative_to(cwd.resolve()).as_posix()
    except ValueError:
        return "file:" + str(path)


def _decision_affects(
    decision: dict[str, Any],
    packet: dict[str, Any],
    vault: Path,
    project_path: Path,
) -> list[str]:
    affects = ["vault:" + project_path.relative_to(vault).as_posix()]
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    cwd = Path(str(packet.get("cwd") or ".")).expanduser().resolve(strict=False)
    for artifact in packet.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("evidence_id") or "") not in evidence:
            continue
        path_value = str(artifact.get("path") or "").strip()
        if path_value:
            affects.append(_relationship_target(path_value, cwd, vault))
    return sorted(set(affects))


def _decision_status(text: str, rationale: str) -> str:
    combined = f"{text} {rationale}".casefold()
    provisional = (
        "provisional",
        "temporary",
        "for now",
        "pending review",
        "open to future",
        "not fully reviewed",
    )
    return (
        "provisional" if any(value in combined for value in provisional) else "active"
    )


def _target_line(target: str) -> str:
    if target.startswith("vault:"):
        relative = target.removeprefix("vault:")
        link = str(Path(relative).with_suffix(""))
        target_path = Path(relative)
        label = (
            target_path.parent.name
            if target_path.stem.casefold() == "project"
            else target_path.stem
        )
        return f"- [[{link}|{label}]]"
    return f"- `{target}`"


def _render_decision(metadata: dict[str, Any], statement: str, rationale: str) -> str:
    affects = "\n".join(_target_line(value) for value in metadata.get("affects", []))
    sources = "\n".join(_target_line(value) for value in metadata.get("sources", []))
    supersedes = "\n".join(f"- `{value}`" for value in metadata.get("supersedes", []))
    body = f"""
# {metadata["title"]}

## Decision

{statement}

## Rationale

{rationale or "No rationale recorded."}

## Blast Radius

{affects or "- None recorded."}

## Supersedes

{supersedes or "- None recorded."}

## Sources

{sources or "- None recorded."}

This is a read-only impact record. Downstream notes require operator approval before modification.
"""
    return _frontmatter_text(metadata, body)


def upsert_decision_records(
    settings: Settings,
    curation: dict[str, Any],
    packet: dict[str, Any],
    *,
    session_path: Path,
    project_path: Path,
) -> list[dict[str, Any]]:
    from .vault import MANAGED_BY, _atomic_write, parse_frontmatter, vault_permalink

    decisions = [
        item for item in curation.get("decisions", []) if isinstance(item, dict)
    ]
    if not decisions:
        update_project_decision_index(settings.vault_path, project_path)
        return []
    project = _slug(str(curation["project_slug"]))
    captured_at = str(packet.get("captured_at") or datetime.now(UTC).isoformat())
    source = "vault:" + session_path.relative_to(settings.vault_path).as_posix()
    revision = _git_revision(packet)
    directory = settings.vault_path / "40 Decisions" / project
    results: list[dict[str, Any]] = []
    for decision in decisions:
        statement = str(decision.get("text") or "").strip()
        if not statement:
            continue
        rationale = str(decision.get("rationale") or "").strip()
        decision_id = decision_id_for(project, statement)
        if not DECISION_ID.fullmatch(decision_id):
            raise ValueError(f"invalid derived decision id: {decision_id}")
        key = decision_id.split("/", 1)[1]
        path = directory / f"{key}.md"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (ValueError, yaml.YAMLError):
                existing = {}
        created_at = str(existing.get("created") or captured_at)
        existing_status = str(existing.get("status") or "")
        status = (
            existing_status
            if existing_status in {"superseded", "rejected"}
            else _decision_status(statement, rationale)
        )
        sources = sorted(
            set(str(value) for value in existing.get("sources", []) if value) | {source}
        )
        affects = sorted(
            set(str(value) for value in existing.get("affects", []) if value)
            | set(
                _decision_affects(decision, packet, settings.vault_path, project_path)
            )
        )
        verified_at = captured_at if _decision_verified(decision, curation) else None
        metadata: dict[str, Any] = {
            **existing,
            "title": statement.rstrip(".")[:120],
            "type": "decision",
            "decision_id": decision_id,
            "project": project,
            "status": status,
            "created": created_at,
            "updated": captured_at,
            "statement_hash": hashlib.sha256(
                re.sub(r"\s+", " ", statement).casefold().encode()
            ).hexdigest(),
            "managed_by": MANAGED_BY,
            "freshness": _merge_freshness(
                existing.get("freshness"),
                settings=settings,
                note_type="decision",
                observed_at=captured_at,
                verified_at=verified_at,
                source=source,
                verified_source=source if verified_at else None,
                source_revision=revision,
            ),
            "sources": sources,
            "affects": affects,
            "supersedes": sorted(
                set(str(value) for value in existing.get("supersedes", []) if value)
            ),
        }
        metadata["permalink"] = vault_permalink(
            path.relative_to(settings.vault_path), settings.basic_memory_project
        )
        content = _render_decision(metadata, statement, rationale)
        if contains_secret(content):
            raise ValueError("rendered decision contains an apparent secret")
        created = not path.exists()
        changed = created or path.read_text(encoding="utf-8") != content
        if changed:
            _atomic_write(path, content)
        results.append(
            {
                "decision_id": decision_id,
                "path": str(path),
                "created": created,
                "changed": changed,
            }
        )
    update_project_decision_index(settings.vault_path, project_path)
    return results


def update_project_metadata(
    settings: Settings,
    curation: dict[str, Any],
    packet: dict[str, Any],
    *,
    session_path: Path,
    project_path: Path,
    trusted: bool,
) -> bool:
    from .vault import MANAGED_BY, parse_frontmatter, vault_permalink

    metadata, body = parse_frontmatter(project_path.read_text(encoding="utf-8"))
    project = _slug(str(curation["project_slug"]))
    metadata["canonical_id"] = f"project:{project}"
    metadata["project"] = project
    metadata["managed_by"] = MANAGED_BY
    metadata.setdefault(
        "permalink",
        vault_permalink(
            project_path.relative_to(settings.vault_path),
            settings.basic_memory_project,
        ),
    )
    captured_at = str(packet.get("captured_at") or datetime.now(UTC).isoformat())
    source = "vault:" + session_path.relative_to(settings.vault_path).as_posix()
    verified_at = captured_at if trusted and curation.get("verification") else None
    metadata["freshness"] = _merge_freshness(
        metadata.get("freshness"),
        settings=settings,
        note_type="project",
        observed_at=captured_at,
        verified_at=verified_at,
        source=source,
        verified_source=source if verified_at else None,
        source_revision=_git_revision(packet),
    )
    return _write_frontmatter(project_path, metadata, body)


def update_project_decision_index(vault: Path, project_path: Path) -> bool:
    from .vault import _atomic_write, parse_frontmatter

    try:
        project_metadata, _ = parse_frontmatter(
            project_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError):
        return False
    project = _slug(str(project_metadata.get("project") or project_path.parent.name))
    links: list[tuple[str, str]] = []
    for path in sorted((vault / "40 Decisions" / project).glob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        relative = path.relative_to(vault).with_suffix("").as_posix()
        title = str(metadata.get("title") or path.stem)
        status = str(metadata.get("status") or "unknown")
        links.append(
            (
                str(metadata.get("updated") or ""),
                f"- [[{relative}|{title}]] — `{status}`",
            )
        )
    links.sort(reverse=True)
    listing = "\n".join(value for _, value in links) or "- None recorded."
    text = project_path.read_text(encoding="utf-8")
    block = f"{DECISION_BLOCK_START}\n{listing}\n{DECISION_BLOCK_END}"
    pattern = re.compile(
        re.escape(DECISION_BLOCK_START) + r".*?" + re.escape(DECISION_BLOCK_END),
        re.DOTALL,
    )
    if pattern.search(text):
        updated = pattern.sub(block, text)
    else:
        updated = text.rstrip() + f"\n\n## Decision Index\n\n{block}\n"
    if updated == text:
        return False
    _atomic_write(project_path, updated)
    return True


def _resolve_target(
    target: str, vault: Path, project_cwd: Path | None
) -> dict[str, Any]:
    kind, separator, value = target.partition(":")
    if not separator:
        return {"target": target, "kind": "unknown", "resolved": False}
    path: Path | None = None
    if kind == "vault":
        path = vault / value
    elif kind == "repo" and project_cwd is not None:
        path = project_cwd / value
    elif kind == "file":
        path = Path(value)
    return {
        "target": target,
        "kind": kind,
        "path": str(path) if path else None,
        "resolved": bool(path and path.exists()),
    }


def _project_cwd(vault: Path, project: str) -> Path | None:
    from .vault import parse_frontmatter

    path = vault / "10 Projects" / project / "Project.md"
    if not path.exists():
        return None
    try:
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    value = str(metadata.get("source_cwd") or "").strip()
    return Path(value).expanduser() if value else None


def preview_decision_impact(
    settings: Settings, decision_id: str, *, depth: int = 1
) -> dict[str, Any]:
    from .vault import IGNORED_VAULT_PARTS, parse_frontmatter

    depth = max(1, min(3, depth))
    decisions: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted((settings.vault_path / "40 Decisions").rglob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        value = str(metadata.get("decision_id") or "")
        if value:
            decisions[value] = (path, metadata)
    if decision_id not in decisions:
        suffix_matches = [
            value for value in decisions if value.endswith("/" + decision_id)
        ]
        if len(suffix_matches) == 1:
            decision_id = suffix_matches[0]
        else:
            return {
                "schema": 1,
                "status": "not-found",
                "decision_id": decision_id,
                "available": sorted(decisions),
                "read_only": True,
            }

    root_path, root = decisions[decision_id]
    project = str(root.get("project") or decision_id.split("/", 1)[0])
    cwd = _project_cwd(settings.vault_path, project)
    direct: list[dict[str, Any]] = []
    for relationship in ("affects", "sources"):
        for target in root.get(relationship, []):
            if isinstance(target, str):
                direct.append(
                    {
                        "relationship": relationship,
                        **_resolve_target(target, settings.vault_path, cwd),
                    }
                )
    for target in root.get("supersedes", []):
        if isinstance(target, str):
            direct.append(
                {
                    "relationship": "supersedes",
                    "target": "decision:" + target,
                    "kind": "decision",
                    "path": str(decisions[target][0]) if target in decisions else None,
                    "resolved": target in decisions,
                }
            )

    incoming: list[dict[str, Any]] = []
    relative_stem = (
        root_path.relative_to(settings.vault_path).with_suffix("").as_posix()
    )
    for path in sorted(settings.vault_path.rglob("*.md")):
        if path == root_path or IGNORED_VAULT_PARTS.intersection(path.parts):
            continue
        relative_path = path.relative_to(settings.vault_path).as_posix()
        if relative_path.startswith(DERIVED_REFERENCE_PREFIXES):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            metadata, _ = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            metadata = {}
        relationships: list[str] = []
        if relative_stem in text or decision_id in text:
            relationships.append("references")
        if decision_id in [str(value) for value in metadata.get("supersedes", [])]:
            relationships.append("superseded-by")
        if relationships:
            incoming.append(
                {
                    "relationships": sorted(set(relationships)),
                    "path": relative_path,
                    "title": str(metadata.get("title") or path.stem),
                }
            )

    # Depth greater than one follows only explicit decision-to-decision edges.
    expanded: list[dict[str, Any]] = []
    frontier = {decision_id}
    visited = {decision_id}
    for distance in range(1, depth + 1):
        next_frontier: set[str] = set()
        for current_id in sorted(frontier):
            _, metadata = decisions[current_id]
            neighbors = {
                str(value) for value in metadata.get("supersedes", []) if value
            }
            neighbors |= {
                candidate
                for candidate, (_, candidate_metadata) in decisions.items()
                if current_id
                in [str(value) for value in candidate_metadata.get("supersedes", [])]
            }
            for neighbor in sorted(neighbors - visited):
                expanded.append(
                    {
                        "decision_id": neighbor,
                        "distance": distance,
                        "path": str(decisions[neighbor][0]),
                        "title": str(decisions[neighbor][1].get("title") or neighbor),
                    }
                )
                next_frontier.add(neighbor)
        visited |= next_frontier
        frontier = next_frontier

    finding = assess_freshness(
        root_path, settings.vault_path, root, now=datetime.now(UTC)
    )
    affected = [item for item in direct if item["relationship"] == "affects"]
    affected_keys = {str(item["target"]) for item in affected}
    affected_keys |= {"vault:" + str(item["path"]) for item in incoming}
    affected_keys |= {"decision:" + str(item["decision_id"]) for item in expanded}
    return {
        "schema": 1,
        "status": "ok",
        "read_only": True,
        "decision": {
            "decision_id": decision_id,
            "title": str(root.get("title") or root_path.stem),
            "status": str(root.get("status") or "unknown"),
            "project": project,
            "path": str(root_path),
            "freshness": asdict(finding) if finding else None,
        },
        "blast_radius": {
            "affected_count": len(affected_keys),
            "direct": direct,
            "incoming": incoming,
            "related_decisions": expanded,
            "depth": depth,
        },
        "safety": "preview only; no downstream note or artifact was modified",
    }


def _section(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body)
    return match.group(1).strip() if match else ""


def _parse_evidenced_items(section: str, *, rationale: bool) -> list[dict[str, Any]]:
    lines = section.splitlines()
    items: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.startswith("- ") or line.strip() == "- None recorded.":
            continue
        if " _(evidence:" not in line:
            continue
        text, evidence_text = line[2:].rsplit(" _(evidence:", 1)
        evidence_ids = re.findall(r"`([^`]+)`", evidence_text)
        item: dict[str, Any] = {"text": text.strip(), "evidence_ids": evidence_ids}
        if rationale:
            value = ""
            if index + 1 < len(lines) and lines[index + 1].startswith("  - Rationale:"):
                value = lines[index + 1].split(":", 1)[1].strip()
            item["rationale"] = value
        items.append(item)
    return items


def _session_artifacts(body: str, cwd: Path) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for line in _section(body, "Artifacts").splitlines():
        evidence = re.findall(r"`([^`]+)`", line.rsplit("_(evidence:", 1)[-1])
        for label, path in local_markdown_links(line, cwd):
            artifacts.append(
                {
                    "label": label,
                    "path": str(path),
                    "evidence_id": evidence[0] if evidence else "",
                }
            )
    return artifacts


def _project_sessions(vault: Path, project: str) -> list[dict[str, Any]]:
    from .vault import parse_frontmatter

    records: list[dict[str, Any]] = []
    for path in sorted((vault / "60 Sessions").rglob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if metadata.get("type") != "work-session" or metadata.get("project") != project:
            continue
        decisions = _parse_evidenced_items(_section(body, "Decisions"), rationale=True)
        verification = _parse_evidenced_items(
            _section(body, "Verification"), rationale=False
        )
        records.append(
            {
                "path": path,
                "metadata": metadata,
                "body": body,
                "decisions": decisions,
                "verification": verification,
            }
        )
    records.sort(
        key=lambda item: str(
            item["metadata"].get("updated") or item["metadata"].get("date") or ""
        )
    )
    return records


def migration_plan(settings: Settings) -> dict[str, Any]:
    from .vault import MANAGED_BY, parse_frontmatter

    projects = 0
    projects_to_update = 0
    sessions_scanned = 0
    decisions_found = 0
    new_decision_ids: set[str] = set()
    existing_ids: set[str] = set()
    runbooks_to_update = 0
    errors: list[str] = []
    for path in sorted((settings.vault_path / "40 Decisions").rglob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if metadata.get("decision_id"):
            existing_ids.add(str(metadata["decision_id"]))
    for project_path in sorted(
        (settings.vault_path / "10 Projects").glob("*/Project.md")
    ):
        try:
            metadata, _ = parse_frontmatter(project_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{project_path}: {error}")
            continue
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        projects += 1
        project = _slug(str(metadata.get("project") or project_path.parent.name))
        if not metadata.get("canonical_id") or not isinstance(
            metadata.get("freshness"), dict
        ):
            projects_to_update += 1
        sessions = _project_sessions(settings.vault_path, project)
        sessions_scanned += len(sessions)
        for record in sessions:
            for decision in record["decisions"]:
                decisions_found += 1
                new_decision_ids.add(decision_id_for(project, decision["text"]))
    for path in sorted(settings.vault_path.rglob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if (
            metadata.get("managed_by") == MANAGED_BY
            and metadata.get("type") in {"runbook", "operational-instruction"}
            and not isinstance(metadata.get("freshness"), dict)
        ):
            runbooks_to_update += 1
    return {
        "schema": 1,
        "status": "planned",
        "mutates": False,
        "projects": projects,
        "projects_to_update": projects_to_update,
        "sessions_scanned": sessions_scanned,
        "decisions_found": decisions_found,
        "decision_records_new": len(new_decision_ids - existing_ids),
        "decision_records_existing": len(new_decision_ids & existing_ids),
        "runbooks_to_update": runbooks_to_update,
        "errors": errors,
    }


def migrate_knowledge(settings: Settings, *, apply: bool) -> dict[str, Any]:
    from .vault import (
        MANAGED_BY,
        ensure_vault_layout,
        parse_frontmatter,
        vault_permalink,
    )

    plan = migration_plan(settings)
    if not apply:
        return plan
    ensure_vault_layout(settings.vault_path)
    updated_projects = 0
    created_decisions = 0
    updated_decisions = 0
    updated_runbooks = 0
    errors = list(plan["errors"])
    for project_path in sorted(
        (settings.vault_path / "10 Projects").glob("*/Project.md")
    ):
        try:
            metadata, body = parse_frontmatter(project_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{project_path}: {error}")
            continue
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        project = _slug(str(metadata.get("project") or project_path.parent.name))
        sessions = _project_sessions(settings.vault_path, project)
        observed_record = sessions[-1] if sessions else None
        verified_records = [item for item in sessions if item["verification"]]
        verified_record = verified_records[-1] if verified_records else None
        metadata["canonical_id"] = f"project:{project}"
        metadata.setdefault(
            "permalink",
            vault_permalink(
                project_path.relative_to(settings.vault_path),
                settings.basic_memory_project,
            ),
        )
        if observed_record:
            observed_at = str(
                observed_record["metadata"].get("updated")
                or observed_record["metadata"].get("date")
            )
            verified_at = (
                str(
                    verified_record["metadata"].get("updated")
                    or verified_record["metadata"].get("date")
                )
                if verified_record
                else None
            )
            observed_source = (
                "vault:"
                + observed_record["path"].relative_to(settings.vault_path).as_posix()
            )
            verified_source = (
                "vault:"
                + verified_record["path"].relative_to(settings.vault_path).as_posix()
                if verified_record
                else None
            )
            metadata["freshness"] = _merge_freshness(
                metadata.get("freshness"),
                settings=settings,
                note_type="project",
                observed_at=observed_at,
                verified_at=verified_at,
                source=observed_source,
                verified_source=verified_source,
                source_revision=str(
                    observed_record["metadata"].get("source_revision") or ""
                )
                or None,
            )
        else:
            existing_freshness = metadata.get("freshness")
            existing_observed = (
                str(existing_freshness.get("observed_at") or "")
                if isinstance(existing_freshness, dict)
                else ""
            )
            observed_at = (
                existing_observed
                or datetime.fromtimestamp(project_path.stat().st_mtime, UTC).isoformat()
            )
            metadata["freshness"] = _merge_freshness(
                metadata.get("freshness"),
                settings=settings,
                note_type="project",
                observed_at=observed_at,
                verified_at=None,
                source="vault:"
                + project_path.relative_to(settings.vault_path).as_posix(),
            )
        if _write_frontmatter(project_path, metadata, body):
            updated_projects += 1

        for record in sessions:
            if not record["decisions"]:
                continue
            session_metadata = record["metadata"]
            captured_at = str(
                session_metadata.get("updated") or session_metadata.get("date")
            )
            cwd = Path(str(session_metadata.get("source_cwd") or ".")).expanduser()
            curation = {
                "project_slug": project,
                "project_name": str(metadata.get("title") or project),
                "decisions": record["decisions"],
                "verification": record["verification"],
            }
            packet = {
                "captured_at": captured_at,
                "cwd": str(cwd),
                "artifacts": _session_artifacts(record["body"], cwd),
                "evidence": [],
            }
            try:
                results = upsert_decision_records(
                    settings,
                    curation,
                    packet,
                    session_path=record["path"],
                    project_path=project_path,
                )
            except (OSError, ValueError, yaml.YAMLError) as error:
                errors.append(f"{record['path']}: {error}")
                continue
            created_decisions += sum(1 for item in results if item["created"])
            updated_decisions += sum(
                1 for item in results if item["changed"] and not item["created"]
            )
        update_project_decision_index(settings.vault_path, project_path)

    for path in sorted(settings.vault_path.rglob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if metadata.get("managed_by") != MANAGED_BY or metadata.get("type") not in {
            "runbook",
            "operational-instruction",
        }:
            continue
        observed_at = (
            str(metadata.get("updated") or "")
            or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        )
        metadata["freshness"] = _merge_freshness(
            metadata.get("freshness"),
            settings=settings,
            note_type=str(metadata.get("type") or "runbook"),
            observed_at=observed_at,
            verified_at=str(metadata.get("verified_at") or "") or None,
            source="vault:" + path.relative_to(settings.vault_path).as_posix(),
        )
        if _write_frontmatter(path, metadata, body):
            updated_runbooks += 1

    return {
        **plan,
        "status": "applied" if not errors else "applied-with-errors",
        "mutates": True,
        "projects_updated": updated_projects,
        "decision_records_created": created_decisions,
        "decision_records_updated": updated_decisions,
        "runbooks_updated": updated_runbooks,
        "errors": sorted(set(errors)),
    }


def _identity_conflicts(vault: Path) -> list[dict[str, Any]]:
    from .vault import parse_frontmatter

    by_canonical: dict[str, list[str]] = {}
    by_cwd: dict[str, list[str]] = {}
    for path in sorted((vault / "10 Projects").glob("*/Project.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        relative = path.relative_to(vault).as_posix()
        canonical = str(metadata.get("canonical_id") or "").strip()
        cwd = str(metadata.get("source_cwd") or "").strip()
        if canonical:
            by_canonical.setdefault(canonical, []).append(relative)
        if cwd:
            normalized = str(Path(cwd).expanduser().resolve(strict=False))
            by_cwd.setdefault(normalized, []).append(relative)
    conflicts: list[dict[str, Any]] = []
    for value, paths in by_canonical.items():
        if len(paths) > 1:
            conflicts.append({"kind": "canonical-id", "value": value, "paths": paths})
    for value, paths in by_cwd.items():
        if len(paths) > 1:
            conflicts.append({"kind": "source-cwd", "value": value, "paths": paths})
    return conflicts


def knowledge_status(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    from .vault import parse_frontmatter

    checked_at = _utc(now or datetime.now(UTC))
    findings = scan_freshness(settings.vault_path, now=checked_at)
    counts: dict[str, int] = {}
    for item in findings:
        counts[item.state] = counts.get(item.state, 0) + 1
    decisions: list[dict[str, Any]] = []
    for path in sorted((settings.vault_path / "40 Decisions").rglob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        decision_id = str(metadata.get("decision_id") or "")
        if not decision_id:
            continue
        finding = next(
            (
                item
                for item in findings
                if item.path == path.relative_to(settings.vault_path).as_posix()
            ),
            None,
        )
        decisions.append(
            {
                "decision_id": decision_id,
                "title": str(metadata.get("title") or path.stem),
                "project": str(metadata.get("project") or ""),
                "status": str(metadata.get("status") or "unknown"),
                "freshness": finding.state if finding else "unknown",
                "affects": len(metadata.get("affects", [])),
                "path": path.relative_to(settings.vault_path).as_posix(),
            }
        )
    return {
        "schema": 1,
        "checked_at": checked_at.isoformat(),
        "freshness": {
            "counts": counts,
            "notes": [asdict(item) for item in findings],
        },
        "decisions": decisions,
        "identity_conflicts": _identity_conflicts(settings.vault_path),
        "read_only": True,
    }


def render_knowledge_report(status: dict[str, Any], *, permalink: str) -> str:
    from .vault import MANAGED_BY

    findings = status["freshness"]["notes"]
    attention = [
        item
        for item in findings
        if item["state"] in {"review-due", "unknown", "unverified", "invalid"}
    ]
    freshness_lines = (
        "\n".join(
            f"- **{item['state']}** [[{Path(item['path']).with_suffix('').as_posix()}|{item['title']}]]"
            f" — {item['detail']}"
            for item in attention
        )
        or "- None."
    )
    decision_lines = (
        "\n".join(
            f"- `{item['decision_id']}` — {item['title']} "
            f"(`{item['status']}`, freshness `{item['freshness']}`, {item['affects']} direct targets)"
            for item in status["decisions"]
        )
        or "- None."
    )
    conflict_lines = (
        "\n".join(
            f"- **{item['kind']}** `{item['value']}`: "
            + ", ".join(f"`{path}`" for path in item["paths"])
            for item in status["identity_conflicts"]
        )
        or "- None."
    )
    metadata = yaml.safe_dump(
        {
            "title": "Knowledge Status",
            "type": "system-report",
            "updated": status["checked_at"],
            "managed_by": MANAGED_BY,
            "permalink": permalink,
        },
        sort_keys=False,
    ).strip()
    output = f"""---
{metadata}
---

# Knowledge Status

## Freshness Summary

{", ".join(f"{key}: {value}" for key, value in sorted(status["freshness"]["counts"].items())) or "No freshness-bearing notes."}

## Freshness Attention

{freshness_lines}

## Decision Registry

{decision_lines}

Use `obsidian-sidecar decision-impact DECISION_ID` for a read-only blast-radius preview. No downstream note is changed automatically.

## Project Identity Conflicts

{conflict_lines}
""".rstrip()
    if contains_secret(output):
        raise ValueError("knowledge report contains an apparent secret")
    return output


def write_knowledge_report(settings: Settings) -> Path:
    from .vault import _atomic_write, vault_permalink

    path = settings.vault_path / "_System" / "Knowledge" / "latest.md"
    status = knowledge_status(settings)
    content = render_knowledge_report(
        status,
        permalink=vault_permalink(
            path.relative_to(settings.vault_path), settings.basic_memory_project
        ),
    )
    _atomic_write(path, content)
    return path
