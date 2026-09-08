from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def has_usable_transcript_path(event: dict[str, Any]) -> bool:
    value = event.get("transcript_path")
    return isinstance(value, str) and bool(value.strip())


def capture_cutoff(event: dict[str, Any]) -> datetime:
    value = event.get("captured_at")
    if not isinstance(value, str):
        raise ValueError("Capture timestamp must be an ISO timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Capture timestamp must include a timezone")
    return parsed.astimezone(UTC)


def event_key(event: dict[str, Any]) -> str:
    stable = "|".join(
        str(event.get(key) or "")
        for key in ("session_id", "turn_id", "hook_event_name", "transcript_path")
    )
    if not event.get("turn_id"):
        stable += "|" + str(event.get("captured_at") or "")
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def enqueue_event(settings: Settings, event: dict[str, Any]) -> Path:
    if not has_usable_transcript_path(event):
        raise ValueError("Hook event has no transcript_path")
    normalized = {
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
        "transcript_path": event.get("transcript_path"),
        "cwd": event.get("cwd"),
        "model": event.get("model"),
        "permission_mode": event.get("permission_mode"),
        "hook_event_name": event.get("hook_event_name", "Stop"),
        "captured_at": event.get("captured_at") or utc_now(),
        "attempts": int(event.get("attempts", 0)),
    }
    key = event_key(normalized)
    target = settings.queue_dir / f"{key}.json"
    if not target.exists():
        _atomic_json(target, normalized)
    return target


def capture_hook(settings: Settings) -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            if not has_usable_transcript_path(payload):
                # Preserve only routing metadata, never the hook's text or tool output.
                record = {
                    key: payload[key]
                    for key in ("session_id", "turn_id", "cwd")
                    if isinstance(payload.get(key), str)
                }
                record.update({"captured_at": utc_now(), "hook_event_name": "Stop"})
                record["reason"] = "missing-transcript-path"
                record["codex_home"] = str(_codex_home())
                _atomic_json(
                    settings.capture_pending_dir / f"{uuid.uuid4().hex}.json", record
                )
                return 0
            enqueue_event(settings, payload)
        else:
            raise ValueError("Hook input must be an object")
    except Exception as exc:  # A memory hook must never block the active Codex turn.
        try:
            _atomic_json(
                settings.capture_failed_dir / f"{uuid.uuid4().hex}.json",
                {
                    "captured_at": utc_now(),
                    "reason": "invalid-hook-input",
                    "error": type(exc).__name__,
                },
            )
        except OSError:
            # Do not break Codex even if state storage is unavailable.
            pass
    return 0


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def _find_transcript(event: dict[str, Any]) -> Path | None:
    """Resolve only an exact session ID and matching metadata, never the latest file."""
    session_id = event.get("session_id")
    try:
        if str(uuid.UUID(session_id)) != session_id:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    root = Path(event.get("codex_home") or _codex_home()).expanduser().resolve()
    matches: set[Path] = set()
    for directory in (root / "sessions", root / "archived_sessions"):
        for path in directory.rglob(f"*{session_id}.jsonl"):
            resolved = path.resolve()
            if not resolved.is_relative_to(directory.resolve()):
                continue
            try:
                with resolved.open(encoding="utf-8") as handle:
                    header = json.loads(handle.readline(65_536))
                metadata = header.get("payload", {})
                if (
                    header.get("type") != "session_meta"
                    or metadata.get("id") != session_id
                ):
                    continue
                if event.get("cwd") and metadata.get("cwd") != event["cwd"]:
                    continue
            except (OSError, ValueError, AttributeError):
                continue
            matches.add(resolved)
    return next(iter(matches)) if len(matches) == 1 else None


def recover_captures(settings: Settings, *, now: datetime | None = None) -> int:
    """Retry incomplete hooks locally; unresolvable entries remain visible for repair."""
    recovered = 0
    current = now or datetime.now(UTC)
    for path in sorted(settings.capture_pending_dir.glob("*.json")):
        try:
            event = load_event(path)
            transcript = _find_transcript(event)
            if transcript is not None:
                # Unique recovery turn prevents a newer incomplete hook being merged
                # into an older pending event with an earlier evidence cutoff.
                event["turn_id"] = event.get("turn_id") or f"recovered-{path.stem}"
                event["transcript_path"] = str(transcript)
                enqueue_event(settings, event)
                move_event(path, settings.state_dir / "capture-recovered")
                recovered += 1
                continue
            captured_at = datetime.fromisoformat(
                event["captured_at"].replace("Z", "+00:00")
            )
            if (current - captured_at).total_seconds() >= 86_400:
                move_event(path, settings.capture_failed_dir)
        except (OSError, ValueError, KeyError, TypeError):
            if path.exists():
                move_event(path, settings.capture_failed_dir, "invalid-metadata")
    return recovered


def capture_health(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, int]:
    current = (now or datetime.now(UTC)).timestamp()
    ages = []
    for path in settings.capture_pending_dir.glob("*.json"):
        try:
            ages.append(current - path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return {
        "pending": len(ages),
        "stalled": sum(age >= 1_800 for age in ages),
        "failed": len(list(settings.capture_failed_dir.glob("*.json"))),
    }


def runtime_problem(settings: Settings, *, now: datetime | None = None) -> str | None:
    for name in ("worker", "cloud-maintenance"):
        problem = _runtime_problem(settings, name, now=now)
        if problem:
            return problem
    return None


def _runtime_problem(
    settings: Settings, name: str, *, now: datetime | None = None
) -> str | None:
    path = settings.state_dir / f"{name}-status.json"
    if not path.exists():
        return None  # Not yet deployed, or a cloud-only runtime.
    try:
        state = load_event(path)
        if state.get("status") == "error" or state.get("failure"):
            return f"{name}-error"
        current = now or datetime.now(UTC)
        checked_at = datetime.fromisoformat(state["checked_at"])
        if name == "worker" and (current - checked_at).total_seconds() >= 1800:
            return f"{name}-stale"
        if state.get("deferred_since"):
            since = datetime.fromisoformat(state["deferred_since"])
            if (current - since).total_seconds() >= 1800:
                return f"{name}-stalled"
    except (OSError, ValueError, KeyError, TypeError):
        return f"{name}-status-invalid"
    return None


def load_event(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Queue event is not an object: {path}")
    return value


def save_event(path: Path, event: dict[str, Any]) -> None:
    _atomic_json(path, event)


def ready_groups(settings: Settings, *, force: bool = False) -> list[list[Path]]:
    now = datetime.now(UTC).timestamp()
    by_session: dict[tuple[str, str], list[Path]] = defaultdict(list)
    cutoffs: dict[Path, datetime] = {}
    arrivals: dict[Path, float] = {}
    for path in settings.queue_dir.glob("*.json"):
        try:
            event = load_event(path)
            arrivals[path] = path.stat().st_mtime
        except FileNotFoundError:
            continue
        except Exception:
            move_event(path, settings.failed_dir, "invalid-json")
            continue
        session_id = str(event.get("session_id") or path.stem)
        try:
            cutoffs[path] = capture_cutoff(event)
            if not has_usable_transcript_path(event):
                raise ValueError("Missing transcript path")
            transcript = str(Path(event["transcript_path"]).expanduser().resolve())
        except (OSError, ValueError):
            cutoffs[path] = datetime.min.replace(tzinfo=UTC)
            transcript = f"invalid:{path.name}"
        by_session[session_id, transcript].append(path)
    ready: list[list[Path]] = []
    for paths in by_session.values():
        paths.sort(key=lambda item: (cutoffs[item], item.name))
        newest_age = now - max(arrivals[path] for path in paths)
        if force or newest_age >= settings.debounce_seconds:
            ready.append(paths)
    return sorted(ready, key=lambda group: max(arrivals[path] for path in group))


def move_event(path: Path, destination: Path, suffix: str | None = None) -> Path:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = path.name if not suffix else f"{path.stem}--{suffix}{path.suffix}"
    target = destination / name
    if target.exists():
        target.unlink()
    shutil.move(str(path), target)
    return target
