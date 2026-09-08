from __future__ import annotations

import fcntl
import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LEASE_PATH = Path("_System/Coordination/cloud-maintenance.json")
LOCAL_WRITER_PATH = Path("_System/Coordination/local-writer.json")


class LeaseBusy(RuntimeError):
    """Expected contention; callers must defer without consuming a retry."""

    def __init__(self, relative_path: Path, reason: str) -> None:
        kind = (
            "local-writer"
            if relative_path == LOCAL_WRITER_PATH
            else "cloud-maintenance"
        )
        self.reason = f"{kind}-{reason}"
        super().__init__(f"{kind} lease is {reason}")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _lease_status(
    vault: Path, relative_path: Path, *, now: datetime | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    path = vault / relative_path
    if not path.exists():
        return False, "none", None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        expires_at = _parse_time(str(value["expires_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return True, "malformed-fail-closed", None
    current = now or datetime.now(UTC)
    if expires_at <= current:
        return False, "expired", value
    return True, "active", value


def cloud_lease_status(
    vault: Path, *, now: datetime | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    return _lease_status(vault, LEASE_PATH, now=now)


def local_writer_status(
    vault: Path, *, now: datetime | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    return _lease_status(vault, LOCAL_WRITER_PATH, now=now)


class SharedLease:
    def __init__(
        self,
        vault: Path,
        *,
        ttl_seconds: int,
        relative_path: Path,
        owner: str,
        now: datetime | None = None,
    ) -> None:
        self.vault = vault
        self.ttl_seconds = ttl_seconds
        self.started_at = now or datetime.now(UTC)
        self.token = uuid.uuid4().hex
        self.path = vault / relative_path
        self.relative_path = relative_path
        self.owner = owner
        self.acquired = False
        self.host_descriptor: int | None = None

    def __enter__(self) -> "SharedLease":
        self.vault.mkdir(parents=True, exist_ok=True)
        self.host_descriptor = os.open(self.vault, os.O_RDONLY)
        try:
            fcntl.flock(self.host_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.host_descriptor)
            self.host_descriptor = None
            active, reason, _ = _lease_status(
                self.vault, self.relative_path, now=self.started_at
            )
            raise LeaseBusy(
                self.relative_path, reason if active else "host-lock"
            ) from error
        except BaseException:
            self._release_host_lock()
            raise
        try:
            return self._acquire()
        except BaseException:
            self._release_host_lock()
            raise

    def _acquire(self) -> "SharedLease":
        value = {
            "schema": 1,
            "token": self.token,
            "owner": self.owner,
            "host": socket.gethostname(),
            "started_at": self.started_at.isoformat(),
            "expires_at": (
                self.started_at + timedelta(seconds=self.ttl_seconds)
            ).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        acquire_lock = self.path.parent / ".lease-acquire.lock"
        with acquire_lock.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            active, reason, _ = _lease_status(
                self.vault, self.relative_path, now=self.started_at
            )
            if active:
                raise LeaseBusy(self.relative_path, reason)
            if self.path.exists():
                self.path.unlink()
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as error:
                raise LeaseBusy(self.relative_path, "acquired-concurrently") from error
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(value, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        self.acquired = True
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self._release_lease()
        finally:
            self.acquired = False
            self._release_host_lock()

    def _release_host_lock(self) -> None:
        if self.host_descriptor is not None:
            os.close(self.host_descriptor)
            self.host_descriptor = None

    def _release_lease(self) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(value, dict) and value.get("token") == self.token:
            self.path.unlink(missing_ok=True)


class CloudLease(SharedLease):
    def __init__(
        self,
        vault: Path,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> None:
        super().__init__(
            vault,
            ttl_seconds=ttl_seconds,
            relative_path=LEASE_PATH,
            owner="obsidian-cloud-maintenance",
            now=now,
        )


class LocalWriterLease(SharedLease):
    def __init__(
        self,
        vault: Path,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> None:
        super().__init__(
            vault,
            ttl_seconds=ttl_seconds,
            relative_path=LOCAL_WRITER_PATH,
            owner="codex-obsidian-sidecar-local-worker",
            now=now,
        )


class MachineProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "MachineProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.handle.close()
            self.handle = None
            raise LeaseBusy(LEASE_PATH, "host-lock") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is None:
            return
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
