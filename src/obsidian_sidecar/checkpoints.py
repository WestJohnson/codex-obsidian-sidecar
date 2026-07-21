from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .security import contains_secret, redact_text


CHECKPOINT_VERSION = 1
CURATION_LIST_FIELDS = (
    "decisions",
    "unresolved",
    "next_actions",
    "verification",
    "changes",
)


def checkpoint_path(settings: Settings, session_id: str) -> Path:
    token = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return settings.checkpoint_dir / f"{token}.json"


def _log_checkpoint_error(settings: Settings, session_id: str, error: Exception) -> None:
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        clean, _ = redact_text(str(error))
        session_token = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
        line = {
            "at": datetime.now(UTC).isoformat(),
            "session": session_token,
            "error": type(error).__name__,
            "detail": clean[:500],
        }
        path = settings.log_dir / "checkpoint-errors.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        os.chmod(path, 0o600)
    except OSError:
        return


def _validate_checkpoint(value: Any, session_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("checkpoint is not a JSON object")
    if value.get("version") != CHECKPOINT_VERSION:
        raise ValueError("checkpoint version is unsupported")
    if value.get("session_id") != session_id:
        raise ValueError("checkpoint session does not match")
    if not isinstance(value.get("cursor"), dict):
        raise ValueError("checkpoint cursor is missing")
    if not isinstance(value.get("curation"), dict):
        raise ValueError("checkpoint curation is missing")
    serialized = json.dumps(value, ensure_ascii=False)
    if contains_secret(serialized):
        raise ValueError("checkpoint contains apparent secret material")
    return value


def load_checkpoint(settings: Settings, session_id: str) -> dict[str, Any] | None:
    if not settings.checkpoint_enabled:
        return None
    path = checkpoint_path(settings, session_id)
    if not path.is_file():
        return None
    try:
        return _validate_checkpoint(
            json.loads(path.read_text(encoding="utf-8")), session_id
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _log_checkpoint_error(settings, session_id, error)
        return None


def _atomic_checkpoint(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if contains_secret(serialized):
        raise ValueError("refusing to persist checkpoint with apparent secret material")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def save_checkpoint(
    settings: Settings,
    packet: dict[str, Any],
    curation: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
) -> Path | None:
    if not settings.checkpoint_enabled:
        return None
    session_id = str(packet.get("session_id") or "")
    checkpoint_meta = packet.get("checkpoint")
    if not session_id or not isinstance(checkpoint_meta, dict):
        raise ValueError("packet has no checkpoint cursor")
    cursor = checkpoint_meta.get("cursor")
    if not isinstance(cursor, dict):
        raise ValueError("packet checkpoint cursor is invalid")
    artifacts: list[dict[str, str]] = []
    for item in packet.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        artifacts.append(
            {
                "label": str(item.get("label") or Path(path).name)[:200],
                "path": path,
            }
        )
    value = {
        "version": CHECKPOINT_VERSION,
        "session_id": session_id,
        "transcript_path": str(cursor.get("transcript_path") or ""),
        "cursor": cursor,
        "captured_at": str(packet.get("captured_at") or datetime.now(UTC).isoformat()),
        "updated_at": datetime.now(UTC).isoformat(),
        "update_count": int((previous or {}).get("update_count", 0)) + 1,
        "curation": curation,
        "artifacts": artifacts,
    }
    _validate_checkpoint(value, session_id)
    path = checkpoint_path(settings, session_id)
    _atomic_checkpoint(path, value)
    return path


def _strip_evidence(item: dict[str, Any]) -> dict[str, str]:
    value = {"text": str(item.get("text") or "").strip()[:1000]}
    rationale = str(item.get("rationale") or "").strip()
    disposition = str(item.get("disposition") or "").strip()
    if rationale:
        value["rationale"] = rationale[:1000]
    if disposition:
        value["disposition"] = disposition[:40]
    return value


def checkpoint_evidence(
    checkpoint: dict[str, Any], *, maximum_chars: int
) -> str:
    """Render bounded prior state without stale packet evidence identifiers."""

    source = checkpoint.get("curation")
    if not isinstance(source, dict):
        raise ValueError("checkpoint has no curation object")
    compact: dict[str, Any] = {
        "checkpoint_version": checkpoint.get("version"),
        "checkpoint_updated_at": checkpoint.get("updated_at")
        or checkpoint.get("captured_at"),
    }
    for field, limit in (
        ("title", 120),
        ("project_name", 100),
        ("project_slug", 64),
        ("current_phase", 200),
        ("summary", 1200),
        ("objective", 1200),
        ("outcome", 1600),
        ("resume_context", 1200),
    ):
        compact[field] = str(source.get(field) or "").strip()[:limit]
    topics = source.get("topics", [])
    compact["topics"] = (
        [str(value)[:50] for value in topics[:12]]
        if isinstance(topics, list)
        else []
    )

    for field in CURATION_LIST_FIELDS:
        compact[field] = []
        values = source.get(field, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            candidate = _strip_evidence(item)
            trial = {**compact, field: [*compact[field], candidate]}
            if len(json.dumps(trial, ensure_ascii=False)) > maximum_chars:
                break
            compact[field].append(candidate)

    return json.dumps(compact, ensure_ascii=False, sort_keys=True)


def _project_name(body: str, fallback: str) -> str:
    match = re.search(r"(?m)^\*\*Project:\*\* \[\[[^|\]]+\|([^\]]+)\]\]", body)
    return match.group(1).strip() if match else fallback


def _disposition(text: str) -> str:
    lowered = text.casefold()
    if any(value in lowered for value in ("monitor", "observe", "watch")):
        return "monitor"
    if any(value in lowered for value in ("accepted", "known risk")):
        return "accepted"
    if any(value in lowered for value in ("drop", "no longer", "won't do")):
        return "dropped"
    if any(value in lowered for value in ("schedule", "pending review", "planned")):
        return "scheduled"
    return "blocker"


def seed_checkpoint_from_vault(
    settings: Settings,
    *,
    session_id: str,
    transcript_path: Path,
) -> dict[str, Any] | None:
    """Build an in-memory first checkpoint from the latest managed session note."""

    if not settings.checkpoint_enabled:
        return None
    from .knowledge import _parse_evidenced_items, _section, _session_artifacts
    from .vault import MANAGED_BY, parse_frontmatter

    candidates: list[tuple[str, Path, dict[str, Any], str]] = []
    roots = (
        settings.vault_path / "60 Sessions",
        settings.vault_path / "00 Inbox" / "Needs Review",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                metadata, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                metadata.get("session_id") != session_id
                or metadata.get("managed_by") != MANAGED_BY
            ):
                continue
            stamp = str(metadata.get("updated") or metadata.get("date") or "")
            candidates.append((stamp, path, metadata, body))
    if not candidates:
        return None
    _, _, metadata, body = max(candidates, key=lambda item: item[0])
    project_slug = str(metadata.get("project") or "unknown-project")
    unresolved = _parse_evidenced_items(
        _section(body, "Unresolved"), rationale=False
    )
    for item in unresolved:
        text = str(item.get("text") or "")
        prefix = re.match(r"^\*\*([^:*]+):\*\*\s*(.*)$", text)
        if prefix and prefix.group(1).casefold() in {
            "blocker",
            "scheduled",
            "monitor",
            "accepted",
            "dropped",
        }:
            item["disposition"] = prefix.group(1).casefold()
            item["text"] = prefix.group(2).strip()
        else:
            item["disposition"] = _disposition(text)
    next_actions = _parse_evidenced_items(
        _section(body, "Next Actions"), rationale=False
    )
    current_phase = _section(body, "Current Phase")
    if not current_phase:
        current_phase = "continuing" if unresolved or next_actions else "complete"
    resume_context = _section(body, "Resume Context")
    if not resume_context:
        resume_context = (
            str(next_actions[0].get("text") or "")
            if next_actions
            else "Review the latest verified outcome before resuming."
        )
    tags = metadata.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    try:
        confidence = float(metadata.get("confidence") or 0.65)
    except (TypeError, ValueError):
        confidence = 0.65
    curation = {
        "skip": False,
        "title": str(metadata.get("title") or "Recovered session checkpoint"),
        "project_name": _project_name(body, project_slug),
        "project_slug": project_slug,
        "current_phase": current_phase[:200],
        "summary": _section(body, "Summary"),
        "objective": _section(body, "Objective"),
        "outcome": _section(body, "Outcome"),
        "resume_context": resume_context[:2000],
        "decisions": _parse_evidenced_items(
            _section(body, "Decisions"), rationale=True
        ),
        "changes": _parse_evidenced_items(
            _section(body, "Changes"), rationale=False
        ),
        "verification": _parse_evidenced_items(
            _section(body, "Verification"), rationale=False
        ),
        "unresolved": unresolved,
        "next_actions": next_actions,
        "topics": [str(value) for value in tags if str(value) != "work-session"][:12],
        "confidence": confidence,
    }
    cwd = Path(str(metadata.get("source_cwd") or transcript_path.parent))
    value = {
        "version": CHECKPOINT_VERSION,
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "cursor": {
            "transcript_path": str(transcript_path),
            "byte_offset": None,
            "after_timestamp": str(metadata.get("updated") or ""),
        },
        "captured_at": str(metadata.get("updated") or metadata.get("date") or ""),
        "updated_at": str(metadata.get("updated") or ""),
        "update_count": 0,
        "curation": curation,
        "artifacts": _session_artifacts(body, cwd),
    }
    try:
        return _validate_checkpoint(value, session_id)
    except ValueError as error:
        _log_checkpoint_error(settings, session_id, error)
        return None
