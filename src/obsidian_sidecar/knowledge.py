from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
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
DECISION_REVIEW_BLOCK_START = "<!-- SIDECAR:DECISION-REVIEW:START -->"
DECISION_REVIEW_BLOCK_END = "<!-- SIDECAR:DECISION-REVIEW:END -->"
DECISION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}/[a-z0-9][a-z0-9-]{0,79}$")
DECISION_TYPES = frozenset(
    {
        "operator-decision",
        "implemented-choice",
        "recommendation",
        "observation",
        "legacy-unclassified",
    }
)
ACTIVE_DECISION_TYPES = frozenset({"operator-decision", "implemented-choice"})
TERMINAL_DECISION_STATUSES = frozenset({"superseded", "rejected"})
DECISION_TYPE_RANK = {
    "legacy-unclassified": 0,
    "observation": 1,
    "recommendation": 2,
    "implemented-choice": 3,
    "operator-decision": 4,
}
DECISION_AUTHORITY_RANK = {
    "legacy": 0,
    "checkpoint": 1,
    "agent": 2,
    "repository-evidence": 3,
    "operator": 4,
}
DECISION_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)
DECISION_NEGATIONS = frozenset(
    {"avoid", "defer", "disable", "exclude", "never", "no", "not", "without"}
)
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


@dataclass(frozen=True)
class DecisionSimilarity:
    score: float
    sequence: float
    jaccard: float
    containment: float
    auto_reuse: bool
    review: bool


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


def _normalized_decision_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.casefold())).strip()


def _decision_tokens(text: str) -> set[str]:
    return {
        value
        for value in _normalized_decision_text(text).split()
        if value not in DECISION_STOPWORDS
    }


def _decision_similarity(left: str, right: str) -> DecisionSimilarity:
    normalized_left = _normalized_decision_text(left)
    normalized_right = _normalized_decision_text(right)
    if not normalized_left or not normalized_right:
        return DecisionSimilarity(0.0, 0.0, 0.0, 0.0, False, False)
    if normalized_left == normalized_right:
        return DecisionSimilarity(1.0, 1.0, 1.0, 1.0, True, True)
    left_tokens = _decision_tokens(left)
    right_tokens = _decision_tokens(right)
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    smaller = min(len(left_tokens), len(right_tokens))
    jaccard = intersection / union if union else 0.0
    containment = intersection / smaller if smaller else 0.0
    sequence = SequenceMatcher(None, normalized_left, normalized_right).ratio()
    score = max(sequence, jaccard, (jaccard + containment) / 2)
    left_negations = left_tokens & DECISION_NEGATIONS
    right_negations = right_tokens & DECISION_NEGATIONS
    left_numbers = {
        value for value in left_tokens if any(char.isdigit() for char in value)
    }
    right_numbers = {
        value for value in right_tokens if any(char.isdigit() for char in value)
    }
    polarity_matches = left_negations == right_negations
    numbers_match = left_numbers == right_numbers
    enough_terms = smaller >= 4
    auto_reuse = (
        enough_terms
        and polarity_matches
        and numbers_match
        and (
            (jaccard >= 0.78 and containment >= 0.9)
            or (sequence >= 0.9 and jaccard >= 0.6)
        )
    )
    review = (
        enough_terms
        and polarity_matches
        and (
            (jaccard >= 0.5 and containment >= 0.7)
            or (sequence >= 0.78 and jaccard >= 0.45)
        )
    )
    return DecisionSimilarity(
        round(score, 4),
        round(sequence, 4),
        round(jaccard, 4),
        round(containment, 4),
        auto_reuse,
        review or auto_reuse,
    )


def _decision_body_value(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body
    )
    return match.group(1).strip() if match else ""


def _decision_fingerprint(text: str) -> str:
    return hashlib.sha256(
        re.sub(r"\s+", " ", text.strip()).casefold().encode("utf-8")
    ).hexdigest()


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
    elif note_type == "decision" and metadata.get("status") in {
        "superseded",
        "rejected",
    }:
        state = str(metadata["status"])
        detail = f"decision is {state}"
    elif note_type == "decision" and metadata.get("status") in {
        "proposed",
        "needs-review",
        "informational",
    }:
        state = "non-authoritative"
        detail = (
            f"decision status {metadata['status']} is retained for optional "
            "discovery and does not require freshness maintenance"
        )
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


def _change_evidence(curation: dict[str, Any]) -> set[str]:
    return {
        str(evidence_id)
        for item in curation.get("changes", [])
        if isinstance(item, dict)
        for evidence_id in item.get("evidence_ids", [])
    }


def _decision_type(
    decision: dict[str, Any], curation: dict[str, Any]
) -> str:
    explicit = str(decision.get("decision_type") or "").strip()
    if explicit in DECISION_TYPES and explicit != "legacy-unclassified":
        return explicit
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    if any(value.startswith("u") for value in evidence):
        return "operator-decision"
    combined = (
        f"{decision.get('text', '')} {decision.get('rationale', '')}".casefold()
    )
    recommendation_signals = (
        "recommend ",
        "recommendation",
        "consider ",
        "evaluate ",
        "explore ",
        "research ",
        "candidate",
        "option",
    )
    if any(value in combined for value in recommendation_signals):
        return "recommendation"
    if evidence & _change_evidence(curation):
        return "implemented-choice"
    if str(curation.get("current_phase") or "").casefold() == "research":
        return "recommendation"
    return "legacy-unclassified"


def _decision_authority(
    decision: dict[str, Any], existing: dict[str, Any] | None = None
) -> str:
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    if any(value.startswith("u") for value in evidence):
        return "operator"
    if any(value.startswith("g") for value in evidence):
        return "repository-evidence"
    if any(value.startswith("a") for value in evidence):
        return "agent"
    if "c1" in evidence:
        prior = str((existing or {}).get("authority") or "")
        return prior if prior else "checkpoint"
    return str((existing or {}).get("authority") or "legacy")


def _merge_decision_authority(existing: str, incoming: str) -> str:
    return max(
        (
            existing if existing in DECISION_AUTHORITY_RANK else "legacy",
            incoming if incoming in DECISION_AUTHORITY_RANK else "legacy",
        ),
        key=lambda value: DECISION_AUTHORITY_RANK[value],
    )


def _merge_decision_type(existing: str, incoming: str) -> str:
    if existing not in DECISION_TYPES:
        return incoming
    if incoming not in DECISION_TYPES:
        return existing
    return max(
        (existing, incoming),
        key=lambda value: DECISION_TYPE_RANK[value],
    )


def _decision_verified(
    decision: dict[str, Any],
    curation: dict[str, Any],
    decision_type: str,
) -> bool:
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    if decision_type == "operator-decision":
        return any(value.startswith("u") for value in evidence) or "c1" in evidence
    if decision_type in {"implemented-choice", "observation"}:
        return bool(evidence & _verification_evidence(curation))
    return False


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


def _decision_impact(
    decision: dict[str, Any],
    packet: dict[str, Any],
    vault: Path,
    project_path: Path,
) -> dict[str, list[str]]:
    direct: list[str] = []
    evidence = {str(value) for value in decision.get("evidence_ids", [])}
    fingerprint = _decision_fingerprint(str(decision.get("text") or ""))
    cwd = Path(str(packet.get("cwd") or ".")).expanduser().resolve(strict=False)
    for artifact in packet.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        artifact_evidence = str(artifact.get("evidence_id") or "")
        fingerprints = {
            str(value)
            for value in artifact.get("decision_fingerprints", [])
            if isinstance(value, str)
        }
        associated = (
            fingerprint in fingerprints
            if fingerprints
            else artifact_evidence in evidence and artifact_evidence != "c1"
        )
        if not associated:
            continue
        path_value = str(artifact.get("path") or "").strip()
        if path_value:
            direct.append(_relationship_target(path_value, cwd, vault))
    return {
        "direct": sorted(set(direct)),
        "inferred": [],
        "related": [
            "vault:" + project_path.relative_to(vault).as_posix()
        ],
    }


def _normalized_impact(
    existing: dict[str, Any], project_target: str
) -> dict[str, list[str]]:
    value = existing.get("impact")
    if isinstance(value, dict):
        return {
            key: sorted(
                {
                    str(target)
                    for target in value.get(key, [])
                    if isinstance(target, str) and target
                }
            )
            for key in ("direct", "inferred", "related")
        }
    legacy = {
        str(target)
        for target in existing.get("affects", [])
        if isinstance(target, str) and target
    }
    return {
        "direct": [],
        "inferred": sorted(legacy - {project_target}),
        "related": sorted({project_target} | (legacy & {project_target})),
    }


def _decision_status(
    decision_type: str,
    text: str,
    rationale: str,
    *,
    duplicate_review: bool = False,
) -> str:
    if duplicate_review:
        return "needs-review"
    if decision_type == "recommendation":
        return "proposed"
    if decision_type == "observation":
        return "informational"
    if decision_type == "legacy-unclassified":
        return "needs-review"
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


def _decision_records(
    directory: Path,
) -> list[tuple[Path, dict[str, Any], str, str]]:
    from .vault import parse_frontmatter

    records: list[tuple[Path, dict[str, Any], str, str]] = []
    for path in sorted(directory.glob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        decision_id = str(metadata.get("decision_id") or "")
        statement = _decision_body_value(body, "Decision")
        if not decision_id or not statement:
            continue
        rationale = _decision_body_value(body, "Rationale")
        records.append((path, metadata, statement, rationale))
    return records


def _duplicate_candidates(
    records: list[tuple[Path, dict[str, Any], str, str]],
    statement: str,
    *,
    exclude: Path | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for path, metadata, existing_statement, rationale in records:
        if exclude is not None and path == exclude:
            continue
        if str(metadata.get("status") or "") in TERMINAL_DECISION_STATUSES:
            continue
        similarity = _decision_similarity(statement, existing_statement)
        if not similarity.review:
            continue
        candidates.append(
            {
                "path": path,
                "metadata": metadata,
                "statement": existing_statement,
                "rationale": rationale,
                "similarity": similarity,
            }
        )
    return sorted(
        candidates,
        key=lambda item: (
            item["similarity"].auto_reuse,
            item["similarity"].score,
            str(item["metadata"].get("created") or ""),
        ),
        reverse=True,
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
    impact = metadata.get("impact") if isinstance(metadata.get("impact"), dict) else {}
    direct = "\n".join(_target_line(value) for value in impact.get("direct", []))
    inferred = "\n".join(
        _target_line(value) for value in impact.get("inferred", [])
    )
    related = "\n".join(_target_line(value) for value in impact.get("related", []))
    sources = "\n".join(_target_line(value) for value in metadata.get("sources", []))
    supersedes = "\n".join(f"- `{value}`" for value in metadata.get("supersedes", []))
    duplicate_candidates = "\n".join(
        f"- `{item.get('decision_id')}` ({float(item.get('similarity', 0)):.2f})"
        for item in metadata.get("possible_duplicates", [])
        if isinstance(item, dict)
    )
    body = f"""
# {metadata["title"]}

## Classification

- Decision type: `{metadata.get("decision_type", "legacy-unclassified")}`
- Authority: `{metadata.get("authority", "legacy")}`
- Promotion status: `{metadata.get("status", "needs-review")}`

## Decision

{statement}

## Rationale

{rationale or "No rationale recorded."}

## Blast Radius

### Direct

{direct or "- None recorded."}

### Inferred

{inferred or "- None recorded."}

### Related Context

{related or "- None recorded."}

## Duplicate Review

{duplicate_candidates or "- None recorded."}

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
        requested_id = decision_id_for(project, statement)
        if not DECISION_ID.fullmatch(requested_id):
            raise ValueError(f"invalid derived decision id: {requested_id}")
        requested_key = requested_id.split("/", 1)[1]
        requested_path = directory / f"{requested_key}.md"
        records = _decision_records(directory)
        exact_record = next(
            (item for item in records if item[0] == requested_path), None
        )
        candidates = _duplicate_candidates(
            records, statement, exclude=requested_path if exact_record else None
        )
        auto_match = next(
            (
                item
                for item in candidates
                if item["similarity"].auto_reuse
                and str(item["metadata"].get("status") or "")
                not in TERMINAL_DECISION_STATUSES
            ),
            None,
        )
        selected = exact_record or (
            (
                auto_match["path"],
                auto_match["metadata"],
                auto_match["statement"],
                auto_match["rationale"],
            )
            if auto_match
            else None
        )
        path = selected[0] if selected else requested_path
        existing: dict[str, Any] = dict(selected[1]) if selected else {}
        existing_statement = selected[2] if selected else statement
        existing_rationale = selected[3] if selected else rationale
        if not existing and path.exists():
            try:
                existing, body = parse_frontmatter(path.read_text(encoding="utf-8"))
                existing_statement = (
                    _decision_body_value(body, "Decision") or statement
                )
                existing_rationale = (
                    _decision_body_value(body, "Rationale") or rationale
                )
            except (ValueError, yaml.YAMLError):
                existing = {}
        decision_id = str(existing.get("decision_id") or requested_id)
        created_at = str(existing.get("created") or captured_at)
        existing_status = str(existing.get("status") or "")
        incoming_type = _decision_type(decision, curation)
        decision_type = _merge_decision_type(
            str(existing.get("decision_type") or "legacy-unclassified"),
            incoming_type,
        )
        authority = _merge_decision_authority(
            str(existing.get("authority") or "legacy"),
            _decision_authority(decision, existing),
        )
        record_candidates = _duplicate_candidates(
            records,
            existing_statement,
            exclude=path,
        )
        possible_duplicates = [
            {
                "decision_id": str(item["metadata"].get("decision_id") or ""),
                "similarity": item["similarity"].score,
            }
            for item in record_candidates[:5]
            if str(item["metadata"].get("decision_id") or "")
        ]
        duplicate_review = bool(possible_duplicates)
        status = (
            existing_status
            if existing_status in TERMINAL_DECISION_STATUSES
            else _decision_status(
                decision_type,
                statement,
                rationale,
                duplicate_review=duplicate_review,
            )
        )
        sources = sorted(
            {str(value) for value in existing.get("sources", []) if value}
            | {source}
        )
        project_target = (
            "vault:" + project_path.relative_to(settings.vault_path).as_posix()
        )
        prior_impact = _normalized_impact(existing, project_target)
        current_impact = _decision_impact(
            decision, packet, settings.vault_path, project_path
        )
        impact = {
            key: sorted(set(prior_impact[key]) | set(current_impact[key]))
            for key in ("direct", "inferred", "related")
        }
        verified_at = (
            captured_at
            if _decision_verified(decision, curation, decision_type)
            else None
        )
        variants = {
            str(value)
            for value in existing.get("statement_variants", [])
            if isinstance(value, str) and value
        }
        if _normalized_decision_text(statement) != _normalized_decision_text(
            existing_statement
        ):
            variants.add(statement)
        metadata: dict[str, Any] = {
            **existing,
            "title": existing_statement.rstrip(".")[:120],
            "type": "decision",
            "decision_id": decision_id,
            "project": project,
            "decision_type": decision_type,
            "authority": authority,
            "status": status,
            "created": created_at,
            "updated": captured_at,
            "statement_hash": _decision_fingerprint(existing_statement),
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
            "impact": impact,
            "affects": impact["direct"],
            "statement_variants": sorted(variants)[:10],
            "possible_duplicates": possible_duplicates,
            "supersedes": sorted(
                {
                    str(value)
                    for value in existing.get("supersedes", [])
                    if value
                }
            ),
        }
        metadata["permalink"] = vault_permalink(
            path.relative_to(settings.vault_path), settings.basic_memory_project
        )
        content = _render_decision(
            metadata,
            existing_statement,
            existing_rationale or rationale,
        )
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
                "auto_reused": bool(auto_match and path == auto_match["path"]),
                "status": status,
                "decision_type": decision_type,
                "possible_duplicates": possible_duplicates,
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
    active_links: list[tuple[str, str]] = []
    review_links: list[tuple[str, str]] = []
    for path in sorted((vault / "40 Decisions" / project).glob("*.md")):
        try:
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        relative = path.relative_to(vault).with_suffix("").as_posix()
        title = str(metadata.get("title") or path.stem)
        status = str(metadata.get("status") or "unknown")
        decision_type = str(
            metadata.get("decision_type") or "legacy-unclassified"
        )
        authority = str(metadata.get("authority") or "legacy")
        item = (
            str(metadata.get("updated") or ""),
            (
                f"- [[{relative}|{title}]] — `{status}`; "
                f"`{decision_type}`; authority `{authority}`"
            ),
        )
        if status in {"active", "provisional"}:
            active_links.append(item)
        elif status in {"proposed", "needs-review"}:
            review_links.append(item)
    active_links.sort(reverse=True)
    review_links.sort(reverse=True)
    active_listing = (
        "\n".join(value for _, value in active_links) or "- None recorded."
    )
    review_listing = (
        "\n".join(value for _, value in review_links) or "- None recorded."
    )
    text = project_path.read_text(encoding="utf-8")
    active_block = (
        f"{DECISION_BLOCK_START}\n{active_listing}\n{DECISION_BLOCK_END}"
    )
    active_pattern = re.compile(
        re.escape(DECISION_BLOCK_START) + r".*?" + re.escape(DECISION_BLOCK_END),
        re.DOTALL,
    )
    if active_pattern.search(text):
        updated = active_pattern.sub(active_block, text)
    else:
        updated = text.rstrip() + f"\n\n## Decision Index\n\n{active_block}\n"
    review_block = (
        f"{DECISION_REVIEW_BLOCK_START}\n{review_listing}\n"
        f"{DECISION_REVIEW_BLOCK_END}"
    )
    review_pattern = re.compile(
        re.escape(DECISION_REVIEW_BLOCK_START)
        + r".*?"
        + re.escape(DECISION_REVIEW_BLOCK_END),
        re.DOTALL,
    )
    if review_pattern.search(updated):
        updated = review_pattern.sub(review_block, updated)
    else:
        updated = (
            updated.rstrip()
            + f"\n\n## Decision Proposals and Reviews\n\n{review_block}\n"
        )
    if updated == text:
        return False
    _atomic_write(project_path, updated)
    return True


def _replace_managed_block(
    text: str, start: str, end: str, content: str
) -> str:
    block = f"{start}\n{content}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    return pattern.sub(block, text) if pattern.search(text) else text


def _open_work_disposition(item: dict[str, Any], *, fallback: str) -> str:
    explicit = str(item.get("disposition") or "").casefold()
    if explicit in {"blocker", "scheduled", "monitor", "accepted", "dropped"}:
        return explicit
    text = str(item.get("text") or "")
    prefix = re.match(r"^\*\*([^:*]+):\*\*\s*(.*)$", text)
    if prefix:
        value = prefix.group(1).casefold()
        if value in {"blocker", "scheduled", "monitor", "accepted", "dropped"}:
            item["text"] = prefix.group(2).strip()
            return value
    return fallback


def _ensure_project_rollup_blocks(body: str) -> str:
    from .vault import (
        PROJECT_STATE_BLOCK_END,
        PROJECT_STATE_BLOCK_START,
        PROJECT_WORK_BLOCK_END,
        PROJECT_WORK_BLOCK_START,
    )

    if PROJECT_STATE_BLOCK_START not in body:
        state_placeholder = (
            f"{PROJECT_STATE_BLOCK_START}\n- No current state recorded.\n"
            f"{PROJECT_STATE_BLOCK_END}"
        )
        current_context = re.compile(
            r"(?ms)^## Current Context\s*\n.*?(?=^## |\Z)"
        )
        if current_context.search(body):
            body = current_context.sub(
                f"## Current Context\n\n{state_placeholder}\n\n",
                body,
            )
        else:
            body = (
                f"## Current Context\n\n{state_placeholder}\n\n"
                + body.lstrip()
            )
    if PROJECT_WORK_BLOCK_START not in body:
        work_placeholder = (
            f"## Ranked Open Work\n\n{PROJECT_WORK_BLOCK_START}\n"
            f"- None recorded.\n{PROJECT_WORK_BLOCK_END}\n\n"
        )
        sessions_heading = re.search(r"(?m)^## Sessions\s*$", body)
        if sessions_heading:
            body = (
                body[: sessions_heading.start()]
                + work_placeholder
                + body[sessions_heading.start() :]
            )
        else:
            body = body.rstrip() + "\n\n" + work_placeholder
    return body


def update_project_state(vault: Path, project_path: Path) -> bool:
    from .vault import (
        MANAGED_BY,
        PROJECT_STATE_BLOCK_END,
        PROJECT_STATE_BLOCK_START,
        PROJECT_WORK_BLOCK_END,
        PROJECT_WORK_BLOCK_START,
        parse_frontmatter,
    )

    try:
        project_metadata, project_body = parse_frontmatter(
            project_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, yaml.YAMLError):
        return False
    project = _slug(str(project_metadata.get("project") or project_path.parent.name))
    sessions: list[tuple[str, Path, dict[str, Any], str]] = []
    for path in sorted((vault / "60 Sessions").rglob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if (
            metadata.get("managed_by") != MANAGED_BY
            or metadata.get("type") != "work-session"
            or metadata.get("project") != project
        ):
            continue
        stamp = str(metadata.get("updated") or metadata.get("date") or "")
        sessions.append((stamp, path, metadata, body))
    sessions.sort(key=lambda item: item[0], reverse=True)
    if not sessions:
        body = _ensure_project_rollup_blocks(project_body)
        body = _replace_managed_block(
            body,
            PROJECT_STATE_BLOCK_START,
            PROJECT_STATE_BLOCK_END,
            (
                "- **Phase:** `no-session-history`\n"
                "- **Latest outcome:** No managed session has been captured.\n"
                "- **Resume:** Review the project hub and repository evidence "
                "before resuming.\n"
                "- **Verification evidence:** 0 recorded items\n"
                "- **Source:** No managed session yet."
            ),
        )
        body = _replace_managed_block(
            body,
            PROJECT_WORK_BLOCK_START,
            PROJECT_WORK_BLOCK_END,
            "- None recorded.",
        )
        freshness = project_metadata.get("freshness")
        updated = (
            str(freshness.get("observed_at") or "")
            if isinstance(freshness, dict)
            else ""
        )
        project_metadata["current_state"] = {
            "phase": "no-session-history",
            "updated": updated
            or datetime.fromtimestamp(project_path.stat().st_mtime, UTC).isoformat(),
            "source": "vault:"
            + project_path.relative_to(vault).as_posix(),
            "open_items": 0,
            "displayed_items": 0,
            "blockers": 0,
        }
        return _write_frontmatter(project_path, project_metadata, body)

    latest_stamp, latest_path, latest_metadata, latest_body = sessions[0]
    phase = re.sub(
        r"\s+", " ", _section(latest_body, "Current Phase") or "continuing"
    ).strip()
    outcome = re.sub(
        r"\s+",
        " ",
        _section(latest_body, "Outcome") or "No outcome recorded.",
    ).strip()
    resume = (
        _section(latest_body, "Resume Context")
        or "Review the latest project evidence before resuming."
    )
    resume = re.sub(r"\s+", " ", resume).strip()
    verification_count = len(
        _parse_evidenced_items(
            _section(latest_body, "Verification"), rationale=False
        )
    )
    latest_relative = latest_path.relative_to(vault)
    latest_link = latest_relative.with_suffix("").as_posix()
    state_lines = [
        f"- **Phase:** `{phase}`",
        f"- **Latest outcome:** {outcome}",
        f"- **Resume:** {resume}",
        f"- **Verification evidence:** {verification_count} recorded item"
        + ("" if verification_count == 1 else "s"),
        f"- **Source:** [[{latest_link}|{latest_metadata.get('title') or latest_path.stem}]]",
    ]

    ranked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stamp, path, metadata, body in sessions:
        relative = path.relative_to(vault).with_suffix("").as_posix()
        source_title = str(metadata.get("title") or path.stem)
        values = [
            (
                item,
                _open_work_disposition(item, fallback="blocker"),
                "Unresolved",
            )
            for item in _parse_evidenced_items(
                _section(body, "Unresolved"), rationale=False
            )
        ]
        values.extend(
            (
                item,
                _open_work_disposition(item, fallback="scheduled"),
                "Next Actions",
            )
            for item in _parse_evidenced_items(
                _section(body, "Next Actions"), rationale=False
            )
        )
        for item, disposition, heading in values:
            if disposition == "dropped":
                continue
            text = str(item.get("text") or "").strip()
            normalized = _normalized_decision_text(text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ranked.append(
                {
                    "text": text,
                    "disposition": disposition,
                    "stamp": stamp,
                    "source": relative,
                    "source_title": source_title,
                    "heading": heading,
                }
            )
    priority = {"blocker": 0, "scheduled": 1, "monitor": 2, "accepted": 3}
    ranked.sort(key=lambda item: priority.get(item["disposition"], 4))
    work_lines = []
    for index, item in enumerate(ranked[:12], start=1):
        label = str(item["disposition"]).replace("-", " ").title()
        work_lines.append(
            f"{index}. **{label}:** {item['text']} "
            f"([[{item['source']}#{item['heading']}|source]])"
        )
    if not work_lines:
        work_lines = ["- None recorded."]

    project_body = _ensure_project_rollup_blocks(project_body)
    body = _replace_managed_block(
        project_body,
        PROJECT_STATE_BLOCK_START,
        PROJECT_STATE_BLOCK_END,
        "\n".join(state_lines),
    )
    body = _replace_managed_block(
        body,
        PROJECT_WORK_BLOCK_START,
        PROJECT_WORK_BLOCK_END,
        "\n".join(work_lines),
    )
    project_metadata["current_state"] = {
        "phase": phase,
        "updated": latest_stamp,
        "source": "vault:" + latest_relative.as_posix(),
        "open_items": len(ranked),
        "displayed_items": min(len(ranked), 12),
        "blockers": sum(
            1 for item in ranked if item["disposition"] == "blocker"
        ),
    }
    return _write_frontmatter(project_path, project_metadata, body)


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
                "schema": 2,
                "status": "not-found",
                "decision_id": decision_id,
                "available": sorted(decisions),
                "read_only": True,
            }

    root_path, root = decisions[decision_id]
    project = str(root.get("project") or decision_id.split("/", 1)[0])
    cwd = _project_cwd(settings.vault_path, project)
    project_target = (
        "vault:10 Projects/" + project + "/Project.md"
    )
    impact = _normalized_impact(root, project_target)
    direct: list[dict[str, Any]] = []
    inferred: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    for relationship, targets, destination in (
        ("affects-direct", impact["direct"], direct),
        ("affects-inferred", impact["inferred"], inferred),
        ("related-context", impact["related"], related),
        ("source", root.get("sources", []), related),
    ):
        for target in targets:
            if isinstance(target, str):
                destination.append(
                    {
                        "relationship": relationship,
                        **_resolve_target(target, settings.vault_path, cwd),
                    }
                )
    for target in root.get("supersedes", []):
        if isinstance(target, str):
            related.append(
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
    affected_keys = {str(item["target"]) for item in direct}
    return {
        "schema": 2,
        "status": "ok",
        "read_only": True,
        "decision": {
            "decision_id": decision_id,
            "title": str(root.get("title") or root_path.stem),
            "status": str(root.get("status") or "unknown"),
            "decision_type": str(
                root.get("decision_type") or "legacy-unclassified"
            ),
            "authority": str(root.get("authority") or "legacy"),
            "project": project,
            "path": str(root_path),
            "freshness": asdict(finding) if finding else None,
        },
        "blast_radius": {
            "affected_count": len(affected_keys),
            "direct_count": len(direct),
            "inferred_count": len(inferred),
            "related_count": len(related) + len(incoming) + len(expanded),
            "direct": direct,
            "inferred": inferred,
            "related": related,
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
            rationale_value = ""
            decision_type = ""
            cursor = index + 1
            while cursor < len(lines) and not lines[cursor].startswith("- "):
                detail = lines[cursor]
                if detail.startswith("  - Rationale:"):
                    rationale_value = detail.split(":", 1)[1].strip()
                elif detail.startswith("  - Type:"):
                    decision_type = detail.split(":", 1)[1].strip().strip("`")
                cursor += 1
            item["rationale"] = rationale_value
            item["decision_type"] = (
                decision_type
                if decision_type in DECISION_TYPES
                else "legacy-unclassified"
            )
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
        changes = _parse_evidenced_items(_section(body, "Changes"), rationale=False)
        verification = _parse_evidenced_items(
            _section(body, "Verification"), rationale=False
        )
        records.append(
            {
                "path": path,
                "metadata": metadata,
                "body": body,
                "current_phase": _section(body, "Current Phase"),
                "decisions": decisions,
                "changes": changes,
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
    from .vault import (
        MANAGED_BY,
        PROJECT_STATE_BLOCK_START,
        PROJECT_WORK_BLOCK_START,
        parse_frontmatter,
    )

    projects = 0
    projects_to_update = 0
    project_hubs_to_update = 0
    sessions_scanned = 0
    decisions_found = 0
    new_decision_ids: set[str] = set()
    existing_ids: set[str] = set()
    decisions_to_classify = 0
    decisions_with_legacy_impact = 0
    possible_duplicate_pairs: set[tuple[str, str]] = set()
    decision_records: dict[str, list[tuple[str, str]]] = {}
    runbooks_to_update = 0
    errors: list[str] = []
    for path in sorted((settings.vault_path / "40 Decisions").rglob("*.md")):
        try:
            metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        decision_id = str(metadata.get("decision_id") or "")
        if not decision_id:
            continue
        existing_ids.add(decision_id)
        if (
            metadata.get("decision_type") not in DECISION_TYPES
            or metadata.get("authority") not in DECISION_AUTHORITY_RANK
        ):
            decisions_to_classify += 1
        if not isinstance(metadata.get("impact"), dict):
            decisions_with_legacy_impact += 1
        project = str(metadata.get("project") or path.parent.name)
        statement = _decision_body_value(body, "Decision")
        if (
            statement
            and str(metadata.get("status") or "")
            not in TERMINAL_DECISION_STATUSES
        ):
            decision_records.setdefault(project, []).append((decision_id, statement))
    for records in decision_records.values():
        for index, (left_id, left) in enumerate(records):
            for right_id, right in records[index + 1 :]:
                if _decision_similarity(left, right).review:
                    possible_duplicate_pairs.add(tuple(sorted((left_id, right_id))))
    for project_path in sorted(
        (settings.vault_path / "10 Projects").glob("*/Project.md")
    ):
        try:
            metadata, body = parse_frontmatter(
                project_path.read_text(encoding="utf-8")
            )
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
        if (
            PROJECT_STATE_BLOCK_START not in body
            or PROJECT_WORK_BLOCK_START not in body
            or not isinstance(metadata.get("current_state"), dict)
        ):
            project_hubs_to_update += 1
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
        "schema": 2,
        "status": "planned",
        "mutates": False,
        "projects": projects,
        "projects_to_update": projects_to_update,
        "project_hubs_to_update": project_hubs_to_update,
        "sessions_scanned": sessions_scanned,
        "decisions_found": decisions_found,
        "decision_records_new": len(new_decision_ids - existing_ids),
        "decision_records_existing": len(new_decision_ids & existing_ids),
        "decision_records_to_classify": decisions_to_classify,
        "decision_records_with_legacy_impact": decisions_with_legacy_impact,
        "possible_duplicate_pairs": len(possible_duplicate_pairs),
        "runbooks_to_update": runbooks_to_update,
        "errors": errors,
    }


def _backfill_decision_records(settings: Settings) -> set[Path]:
    """Upgrade every managed decision without inventing missing authority."""

    from .vault import MANAGED_BY, _atomic_write, vault_permalink

    changed: set[Path] = set()
    for directory in sorted((settings.vault_path / "40 Decisions").glob("*")):
        if not directory.is_dir():
            continue
        records = _decision_records(directory)
        for path, existing, statement, rationale in records:
            if existing.get("managed_by") != MANAGED_BY:
                continue
            project = _slug(str(existing.get("project") or directory.name))
            project_path = (
                settings.vault_path / "10 Projects" / project / "Project.md"
            )
            project_target = (
                "vault:" + project_path.relative_to(settings.vault_path).as_posix()
            )
            existing_status = str(existing.get("status") or "")
            candidates = (
                []
                if existing_status in TERMINAL_DECISION_STATUSES
                else _duplicate_candidates(records, statement, exclude=path)
            )
            possible_duplicates = [
                {
                    "decision_id": str(item["metadata"].get("decision_id") or ""),
                    "similarity": item["similarity"].score,
                }
                for item in candidates[:5]
                if str(item["metadata"].get("decision_id") or "")
            ]
            decision_type = str(
                existing.get("decision_type") or "legacy-unclassified"
            )
            if decision_type not in DECISION_TYPES:
                decision_type = "legacy-unclassified"
            authority = str(existing.get("authority") or "legacy")
            if authority not in DECISION_AUTHORITY_RANK:
                authority = "legacy"
            status = (
                existing_status
                if existing_status in TERMINAL_DECISION_STATUSES
                else _decision_status(
                    decision_type,
                    statement,
                    rationale,
                    duplicate_review=bool(possible_duplicates),
                )
            )
            impact = _normalized_impact(existing, project_target)
            impact["related"] = sorted(
                set(impact["related"]) | {project_target}
            )
            observed_at = (
                str(existing.get("updated") or existing.get("created") or "")
                or datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
            )
            sources = sorted(
                {
                    str(value)
                    for value in existing.get("sources", [])
                    if isinstance(value, str) and value
                }
            )
            freshness_source = (
                sources[-1]
                if sources
                else "vault:" + path.relative_to(settings.vault_path).as_posix()
            )
            metadata = {
                **existing,
                "title": str(existing.get("title") or statement.rstrip(".")[:120]),
                "type": "decision",
                "decision_id": str(
                    existing.get("decision_id") or decision_id_for(project, statement)
                ),
                "project": project,
                "decision_type": decision_type,
                "authority": authority,
                "status": status,
                "updated": observed_at,
                "statement_hash": _decision_fingerprint(statement),
                "managed_by": MANAGED_BY,
                "freshness": _merge_freshness(
                    existing.get("freshness"),
                    settings=settings,
                    note_type="decision",
                    observed_at=observed_at,
                    verified_at=None,
                    source=freshness_source,
                ),
                "sources": sources,
                "impact": impact,
                "affects": impact["direct"],
                "statement_variants": sorted(
                    {
                        str(value)
                        for value in existing.get("statement_variants", [])
                        if isinstance(value, str) and value
                    }
                )[:10],
                "possible_duplicates": possible_duplicates,
                "supersedes": sorted(
                    {
                        str(value)
                        for value in existing.get("supersedes", [])
                        if isinstance(value, str) and value
                    }
                ),
                "permalink": vault_permalink(
                    path.relative_to(settings.vault_path),
                    settings.basic_memory_project,
                ),
            }
            content = _render_decision(metadata, statement, rationale)
            if contains_secret(content):
                raise ValueError(
                    f"backfilled decision contains an apparent secret: {path.name}"
                )
            if path.read_text(encoding="utf-8") != content:
                _atomic_write(path, content)
                changed.add(path)
    return changed


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
    updated_projects: set[Path] = set()
    created_decisions: set[Path] = set()
    updated_decisions: set[Path] = set()
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
        project_before = project_path.read_bytes()
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
        _write_frontmatter(project_path, metadata, body)

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
                "current_phase": record["current_phase"],
                "decisions": record["decisions"],
                "changes": record["changes"],
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
            created_decisions.update(
                Path(item["path"]) for item in results if item["created"]
            )
            updated_decisions.update(
                Path(item["path"])
                for item in results
                if item["changed"] and not item["created"]
            )
        update_project_decision_index(settings.vault_path, project_path)
        update_project_state(settings.vault_path, project_path)
        if project_path.read_bytes() != project_before:
            updated_projects.add(project_path)

    try:
        updated_decisions.update(_backfill_decision_records(settings))
    except (OSError, ValueError, yaml.YAMLError) as error:
        errors.append(f"decision backfill: {error}")
    for project_path in sorted(
        (settings.vault_path / "10 Projects").glob("*/Project.md")
    ):
        try:
            project_before = project_path.read_bytes()
            update_project_decision_index(settings.vault_path, project_path)
            if project_path.read_bytes() != project_before:
                updated_projects.add(project_path)
        except OSError as error:
            errors.append(f"{project_path}: {error}")

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

    updated_decisions -= created_decisions
    return {
        **plan,
        "status": "applied" if not errors else "applied-with-errors",
        "mutates": True,
        "projects_updated": len(updated_projects),
        "decision_records_created": len(created_decisions),
        "decision_records_updated": len(updated_decisions),
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
                "decision_type": str(
                    metadata.get("decision_type") or "legacy-unclassified"
                ),
                "authority": str(metadata.get("authority") or "legacy"),
                "direct": len(_normalized_impact(metadata, "")["direct"]),
                "inferred": len(_normalized_impact(metadata, "")["inferred"]),
                "possible_duplicates": len(
                    [
                        item
                        for item in metadata.get("possible_duplicates", [])
                        if isinstance(item, dict)
                    ]
                ),
                "path": path.relative_to(settings.vault_path).as_posix(),
            }
        )
    return {
        "schema": 2,
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
            f"(`{item['status']}`, `{item['decision_type']}`, authority "
            f"`{item['authority']}`, freshness `{item['freshness']}`, "
            f"{item['direct']} direct / {item['inferred']} inferred targets, "
            f"{item['possible_duplicates']} duplicate candidates)"
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
