from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def event_key(event: dict[str, Any]) -> str:
    stable = "|".join(
        str(event.get(key) or "")
        for key in ("session_id", "turn_id", "hook_event_name", "transcript_path")
    )
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
            enqueue_event(settings, payload)
    except Exception as exc:  # A memory hook must never block the active Codex turn.
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        with (settings.log_dir / "capture-errors.log").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(f"{utc_now()} {type(exc).__name__}: {exc}\n")
    return 0


def load_event(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Queue event is not an object: {path}")
    return value


def save_event(path: Path, event: dict[str, Any]) -> None:
    _atomic_json(path, event)


def ready_groups(settings: Settings, *, force: bool = False) -> list[list[Path]]:
    now = datetime.now(UTC).timestamp()
    by_session: dict[str, list[Path]] = defaultdict(list)
    for path in settings.queue_dir.glob("*.json"):
        try:
            event = load_event(path)
        except Exception:
            move_event(path, settings.failed_dir, "invalid-json")
            continue
        session_id = str(event.get("session_id") or path.stem)
        by_session[session_id].append(path)
    ready: list[list[Path]] = []
    for paths in by_session.values():
        paths.sort(key=lambda item: item.stat().st_mtime)
        newest_age = now - paths[-1].stat().st_mtime
        if force or newest_age >= settings.debounce_seconds:
            ready.append(paths)
    return sorted(ready, key=lambda group: group[-1].stat().st_mtime)


def move_event(path: Path, destination: Path, suffix: str | None = None) -> Path:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = path.name if not suffix else f"{path.stem}--{suffix}{path.suffix}"
    target = destination / name
    if target.exists():
        target.unlink()
    shutil.move(str(path), target)
    return target
