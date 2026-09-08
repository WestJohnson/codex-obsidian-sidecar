from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Settings
from .queueing import capture_health, load_event, runtime_problem, save_event, utc_now
from .security import contains_secret
from .vault import (
    MANAGED_BY,
    IGNORED_VAULT_PARTS,
    _atomic_write,
    ensure_vault_layout,
    parse_frontmatter,
    vault_permalink,
)


WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


@dataclass
class VaultHealth:
    checked_at: str
    markdown_files: int = 0
    managed_notes: int = 0
    valid_managed_notes: int = 0
    malformed_frontmatter: list[str] = field(default_factory=list)
    missing_required_fields: dict[str, list[str]] = field(default_factory=dict)
    unresolved_links: dict[str, list[str]] = field(default_factory=dict)
    orphan_managed_notes: list[str] = field(default_factory=list)
    duplicate_session_ids: dict[str, list[str]] = field(default_factory=dict)
    stale_project_indexes: list[str] = field(default_factory=list)
    freshness_current: list[str] = field(default_factory=list)
    freshness_unverified: list[str] = field(default_factory=list)
    freshness_review_due: list[str] = field(default_factory=list)
    freshness_unknown: list[str] = field(default_factory=list)
    freshness_invalid: list[str] = field(default_factory=list)
    decision_records: int = 0
    possible_secret_files: list[str] = field(default_factory=list)
    queue_pending: int = 0
    queue_failed: int = 0
    capture_pending: int = 0
    capture_stalled: int = 0
    capture_failed: int = 0
    runtime_problem: str | None = None
    indexing_problem: str | None = None
    obsidian_cli: str = "unknown"
    basic_memory: str = "unknown"
    git_backup: str = "unknown"

    @property
    def critical_failures(self) -> int:
        return (
            len(self.malformed_frontmatter)
            + len(self.missing_required_fields)
            + len(self.duplicate_session_ids)
            + len(self.freshness_invalid)
            + len(self.possible_secret_files)
        )

    @property
    def warnings(self) -> int:
        return (
            len(self.unresolved_links)
            + len(self.orphan_managed_notes)
            + len(self.stale_project_indexes)
            + len(self.freshness_review_due)
            + len(self.freshness_unknown)
            + self.queue_failed
            + self.capture_stalled
            + self.capture_failed
        )

    @property
    def score(self) -> int:
        penalty = self.critical_failures * 15 + self.warnings * 3
        if self.obsidian_cli not in {"ok", "not-required"}:
            penalty += 5
        if self.basic_memory not in {"ok", "not-required"}:
            penalty += 5
        if self.git_backup not in {"ok", "clean"}:
            penalty += 5
        ceiling = (
            79
            if (
                self.capture_stalled
                or self.capture_failed
                or self.runtime_problem
                or self.indexing_problem
            )
            else 100
        )
        return max(0, min(ceiling, 100 - penalty))


def _markdown_files(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*.md")
        if not IGNORED_VAULT_PARTS.intersection(path.parts)
    )


def _resolve_link(
    target: str, source: Path, vault: Path, known: dict[str, list[Path]]
) -> bool:
    candidate = (vault / target).with_suffix(".md")
    if candidate.exists():
        return True
    relative = (source.parent / target).with_suffix(".md")
    if relative.exists():
        return True
    return len(known.get(Path(target).name.casefold(), [])) == 1


def _command_status(command: list[str], ok_markers: tuple[str, ...] = ()) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    combined = f"{result.stdout}\n{result.stderr}".casefold()
    if any(
        marker.casefold() in combined for marker in ("not enabled", "error", "failed")
    ):
        return "unavailable"
    if result.returncode == 0 and (
        not ok_markers or any(marker.casefold() in combined for marker in ok_markers)
    ):
        return "ok"
    return "unavailable"


def basic_memory_binary() -> str | None:
    discovered = shutil.which("bm")
    if discovered:
        return discovered
    fallback = Path.home() / ".local" / "bin" / "bm"
    return str(fallback) if fallback.exists() else None


def basic_memory_status(settings: Settings) -> str:
    binary = basic_memory_binary()
    if not binary:
        return "unavailable"
    try:
        result = subprocess.run(
            [binary, "status", "--project", settings.basic_memory_project, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    combined = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode != 0:
        return "unavailable"
    try:
        value = json.loads(result.stdout)
        if isinstance(value, dict):
            if isinstance(value.get("observed_files"), list):
                # BM 0.23 reports disk observations, not index readiness.
                return "observation-only"
            if all(key in value for key in ("new", "modified", "deleted")):
                return (
                    "stale"
                    if any(
                        value.get(key)
                        for key in ("new", "modified", "deleted", "moves")
                    )
                    else "ok"
                )
    except ValueError:
        pass
    if "no changes" in combined:
        return "ok"
    if any(
        marker in combined
        for marker in ("new files", "modified files", "deleted files")
    ):
        return "stale"
    return "unknown"


def indexing_problem(settings: Settings) -> str | None:
    path = settings.state_dir / "index-status.json"
    try:
        state = load_event(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "index-status-invalid"
    if state.get("status") == "ok":
        return None
    if state.get("status") == "running":
        try:
            started = datetime.fromisoformat(state["checked_at"])
            pid = state["pid"]
            if (
                started.tzinfo is not None
                and 0 <= (datetime.now(UTC) - started).total_seconds() < 600
                and isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
            ):
                try:
                    os.kill(pid, 0)
                except PermissionError:
                    pass  # A live owner may belong to another local user.
                return None
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            pass
    return "basic-memory-index-error"


@contextmanager
def _index_state_lock(settings: Settings) -> Iterator[None]:
    settings.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.lock_dir / "index-state.flock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def pending_index_write(settings: Settings) -> Iterator[None]:
    with _index_state_lock(settings):
        _mark_index_pending(settings)
        yield


def mark_index_pending(settings: Settings) -> None:
    with pending_index_write(settings):
        pass


def _mark_index_pending(settings: Settings) -> None:
    path = settings.state_dir / "index-status.json"
    try:
        previous = load_event(path)
    except (OSError, ValueError):
        previous = {}
    save_event(
        path,
        {
            "checked_at": utc_now(),
            "status": "pending",
            "full": previous.get("status") != "ok" and previous.get("full") is True,
        },
    )


def reindex_basic_memory(settings: Settings, *, full: bool = False) -> str:
    with _index_state_lock(settings):
        return _record_index_result(settings, full=full)


def _record_index_result(settings: Settings, *, full: bool) -> str:
    path = settings.state_dir / "index-status.json"
    try:
        previous = load_event(path)
    except (OSError, ValueError):
        previous = {}
    full = full or (previous.get("status") != "ok" and previous.get("full") is True)
    state = {
        "checked_at": utc_now(),
        "status": "running",
        "full": full,
        "pid": os.getpid(),
    }
    save_event(path, state)
    try:
        result = _reindex_basic_memory(settings, full=full)
    except Exception:
        save_event(path, {**state, "checked_at": utc_now(), "status": "error"})
        raise
    save_event(path, {**state, "checked_at": utc_now(), "status": result})
    return result


def _reindex_basic_memory(settings: Settings, *, full: bool = False) -> str:
    binary = basic_memory_binary()
    if not binary:
        return "unavailable"
    if full:
        command = [
            binary,
            "reindex",
            "--project",
            settings.basic_memory_project,
            "--full",
            "--search",
        ]
        timeout = 240
    else:
        command = [
            binary,
            "status",
            "--project",
            settings.basic_memory_project,
            "--wait",
            "--timeout",
            "90",
            "--json",
        ]
        timeout = 120
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        return "error"
    if not full:
        try:
            value = json.loads(result.stdout)
        except ValueError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("observed_files"), list):
            # Since BM 0.23, --wait is a compatibility no-op. Request the
            # synchronous search-only project-index pass, never embeddings.
            try:
                indexed = subprocess.run(
                    [
                        binary,
                        "reindex",
                        "--project",
                        settings.basic_memory_project,
                        "--search",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=240,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return "unavailable"
            return "ok" if indexed.returncode == 0 else "error"
    return "ok"


def inspect_vault(settings: Settings, *, create_layout: bool = True) -> VaultHealth:
    if create_layout:
        ensure_vault_layout(settings.vault_path)
    health = VaultHealth(checked_at=datetime.now(UTC).isoformat())
    files = _markdown_files(settings.vault_path)
    health.markdown_files = len(files)
    known: dict[str, list[Path]] = {}
    backlinks: dict[Path, int] = {path: 0 for path in files}
    metadata_by_path: dict[Path, dict[str, Any]] = {}
    sessions: dict[str, list[Path]] = {}

    for path in files:
        known.setdefault(path.stem.casefold(), []).append(path)
        relative = path.relative_to(settings.vault_path).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if contains_secret(text):
            health.possible_secret_files.append(relative)
        try:
            metadata, _ = parse_frontmatter(text)
        except (ValueError, yaml.YAMLError):
            health.malformed_frontmatter.append(relative)
            continue
        metadata_by_path[path] = metadata
        if metadata.get("managed_by") != MANAGED_BY:
            continue
        health.managed_notes += 1
        required = {"title", "type", "managed_by"}
        if metadata.get("type") == "work-session":
            required |= {"project", "date", "session_id", "confidence"}
        elif metadata.get("type") == "project":
            required |= {"project", "canonical_id", "freshness"}
        elif metadata.get("type") == "decision":
            required |= {
                "project",
                "decision_id",
                "decision_type",
                "authority",
                "status",
                "freshness",
                "impact",
                "affects",
                "possible_duplicates",
                "sources",
            }
            health.decision_records += 1
        elif metadata.get("type") in {"runbook", "operational-instruction"}:
            required.add("freshness")
        missing = sorted(key for key in required if metadata.get(key) in (None, ""))
        if missing:
            health.missing_required_fields[relative] = missing
        else:
            health.valid_managed_notes += 1
        session_id = metadata.get("session_id")
        if session_id:
            sessions.setdefault(str(session_id), []).append(path)

    for path in files:
        relative = path.relative_to(settings.vault_path).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        missing_targets: list[str] = []
        for target in WIKI_LINK.findall(text):
            if not _resolve_link(target.strip(), path, settings.vault_path, known):
                missing_targets.append(target.strip())
                continue
            exact = (settings.vault_path / target.strip()).with_suffix(".md")
            if exact.exists():
                backlinks[exact] = backlinks.get(exact, 0) + 1
        if missing_targets:
            health.unresolved_links[relative] = sorted(set(missing_targets))

    for path, metadata in metadata_by_path.items():
        if (
            metadata.get("managed_by") != MANAGED_BY
            or metadata.get("type") != "work-session"
        ):
            continue
        if backlinks.get(path, 0) == 0:
            health.orphan_managed_notes.append(
                path.relative_to(settings.vault_path).as_posix()
            )
        project = metadata.get("project")
        project_path = settings.vault_path / "10 Projects" / str(project) / "Project.md"
        if not project_path.exists():
            health.stale_project_indexes.append(
                str(project_path.relative_to(settings.vault_path))
            )
        else:
            expected = path.relative_to(settings.vault_path).with_suffix("").as_posix()
            if expected not in project_path.read_text(
                encoding="utf-8", errors="replace"
            ):
                health.stale_project_indexes.append(
                    str(project_path.relative_to(settings.vault_path))
                )

    health.duplicate_session_ids = {
        session_id: [str(path.relative_to(settings.vault_path)) for path in paths]
        for session_id, paths in sessions.items()
        if len(paths) > 1
    }
    health.orphan_managed_notes.sort()
    health.stale_project_indexes = sorted(set(health.stale_project_indexes))
    from .knowledge import scan_freshness

    for finding in scan_freshness(settings.vault_path):
        if not finding.managed:
            continue
        target = {
            "current": health.freshness_current,
            "unverified": health.freshness_unverified,
            "review-due": health.freshness_review_due,
            "unknown": health.freshness_unknown,
            "invalid": health.freshness_invalid,
        }.get(finding.state)
        if target is not None:
            target.append(finding.path)
    health.queue_pending = len(list(settings.queue_dir.glob("*.json")))
    health.queue_failed = len(list(settings.failed_dir.glob("*.json")))
    captures = capture_health(settings)
    health.capture_pending = captures["pending"]
    health.capture_stalled = captures["stalled"]
    health.capture_failed = captures["failed"]
    health.runtime_problem = runtime_problem(settings)
    health.indexing_problem = indexing_problem(settings)
    if settings.runtime_role == "cloud":
        health.obsidian_cli = "not-required"
        health.basic_memory = "not-required"
    else:
        obsidian_binary = shutil.which("obsidian") or "/opt/homebrew/bin/obsidian"
        health.obsidian_cli = _command_status(
            [
                obsidian_binary,
                f"vault={settings.vault_path.name}",
                "vault",
                "info=path",
            ],
        )
        if sys.platform.startswith("linux") and not Path(obsidian_binary).exists():
            health.obsidian_cli = "not-required"
        health.basic_memory = basic_memory_status(settings)
        if health.indexing_problem:
            health.basic_memory = "error"
    health.git_backup = git_status(settings.vault_path)
    return health


def render_health(health: VaultHealth, *, permalink: str | None = None) -> str:
    status = (
        "PASS" if health.score >= 80 and health.critical_failures == 0 else "ATTENTION"
    )

    def listing(values: list[str]) -> str:
        return "\n".join(f"- `{value}`" for value in values) if values else "- None."

    unresolved = [
        f"{path}: {', '.join(targets)}"
        for path, targets in health.unresolved_links.items()
    ]
    duplicates = [
        f"{session}: {', '.join(paths)}"
        for session, paths in health.duplicate_session_ids.items()
    ]
    missing = [
        f"{path}: {', '.join(fields)}"
        for path, fields in health.missing_required_fields.items()
    ]
    metadata: dict[str, Any] = {
        "title": "Vault Health",
        "type": "system-health",
        "updated": health.checked_at,
        "managed_by": MANAGED_BY,
    }
    if permalink:
        metadata["permalink"] = permalink
    frontmatter = yaml.safe_dump(metadata, sort_keys=False).strip()
    return f"""---
{frontmatter}
---

# Vault Health

**Status:** {status}  
**Score:** {health.score}/100  
**Critical failures:** {health.critical_failures}  
**Warnings:** {health.warnings}

## Runtime

- Obsidian CLI: `{health.obsidian_cli}`
- Basic Memory: `{health.basic_memory}`
- Search indexing: {health.indexing_problem or "ok"}
- Git backup: `{health.git_backup}`
- Queue pending: {health.queue_pending}
- Queue failed: {health.queue_failed}
- Capture recovery pending: {health.capture_pending}
- Capture recovery stalled: {health.capture_stalled}
- Capture recovery failed: {health.capture_failed}
- Worker runtime: {health.runtime_problem or "ok"}

## Content

- Markdown files: {health.markdown_files}
- Managed notes: {health.managed_notes}
- Valid managed notes: {health.valid_managed_notes}
- Decision records: {health.decision_records}

## Freshness

- Current: {len(health.freshness_current)}
- Unverified: {len(health.freshness_unverified)}
- Review due: {len(health.freshness_review_due)}
- Unknown: {len(health.freshness_unknown)}
- Invalid: {len(health.freshness_invalid)}

### Review Due

{listing(health.freshness_review_due)}

### Unverified

{listing(health.freshness_unverified)}

### Unknown Or Missing

{listing(health.freshness_unknown)}

### Invalid Envelopes

{listing(health.freshness_invalid)}

## Malformed Frontmatter

{listing(health.malformed_frontmatter)}

## Missing Required Fields

{listing(missing)}

## Unresolved Links

{listing(unresolved)}

## Orphan Managed Notes

{listing(health.orphan_managed_notes)}

## Duplicate Sessions

{listing(duplicates)}

## Stale Project Indexes

{listing(health.stale_project_indexes)}

## Possible Secrets

{listing(health.possible_secret_files)}
""".rstrip()


def write_health_report(settings: Settings, health: VaultHealth) -> tuple[Path, Path]:
    health_dir = settings.vault_path / "_System" / "Health"
    health_dir.mkdir(parents=True, exist_ok=True)
    prefix = "cloud-" if settings.runtime_role == "cloud" else ""
    latest = health_dir / f"{prefix}latest.md"
    history = health_dir / "history" / f"{prefix}{health.checked_at[:10]}.md"
    _atomic_write(
        latest,
        render_health(
            health,
            permalink=vault_permalink(
                latest.relative_to(settings.vault_path), settings.basic_memory_project
            ),
        ),
    )
    _atomic_write(
        history,
        render_health(
            health,
            permalink=vault_permalink(
                history.relative_to(settings.vault_path), settings.basic_memory_project
            ),
        ),
    )
    json_path = settings.state_dir / "health.json"
    _atomic_write(
        json_path,
        json.dumps(
            {
                **asdict(health),
                "critical_failures": health.critical_failures,
                "warnings": health.warnings,
                "score": health.score,
            },
            indent=2,
        )
        + "\n",
    )
    return latest, history


def git_status(vault: Path) -> str:
    if not (vault / ".git").exists():
        return "not-initialized"
    result = subprocess.run(
        ["git", "-C", str(vault), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return "error"
    return "clean" if not result.stdout.strip() else "pending"


def initialize_git_backup(vault: Path) -> None:
    if not (vault / ".git").exists():
        subprocess.run(
            ["git", "-C", str(vault), "init"], check=True, capture_output=True
        )
    subprocess.run(
        ["git", "-C", str(vault), "config", "user.name", "Codex Obsidian Sidecar"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(vault), "config", "user.email", "obsidian-sidecar@localhost"],
        check=True,
    )
    ignore = vault / ".gitignore"
    desired = """.DS_Store
.obsidian/workspace*
.obsidian/cache/
.trash/
_System/Quarantine/
_System/Coordination/
"""
    if not ignore.exists():
        _atomic_write(ignore, desired)
    else:
        existing = ignore.read_text(encoding="utf-8")
        additions = [
            line
            for line in desired.splitlines()
            if line and line not in existing.splitlines()
        ]
        if additions:
            _atomic_write(
                ignore, existing.rstrip() + "\n" + "\n".join(additions) + "\n"
            )


def commit_git_backup(
    settings: Settings, message: str = "chore(memory): update Obsidian vault"
) -> str:
    initialize_git_backup(settings.vault_path)
    health = inspect_vault(settings)
    if health.possible_secret_files:
        return "blocked-secrets"
    subprocess.run(["git", "-C", str(settings.vault_path), "add", "-A"], check=True)
    staged = subprocess.run(
        ["git", "-C", str(settings.vault_path), "diff", "--cached", "--quiet"],
        check=False,
    )
    if staged.returncode == 0:
        return "clean"
    result = subprocess.run(
        ["git", "-C", str(settings.vault_path), "commit", "-m", message],
        capture_output=True,
        text=True,
        check=False,
    )
    return "ok" if result.returncode == 0 else "error"
