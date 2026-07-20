from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from . import __version__
from .config import Settings
from .vault import _atomic_write


PACKAGE_NAME = "codex-obsidian-sidecar"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple"


def _validate_index_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Update metadata URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Update metadata URL must not contain credentials")


def _fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    _validate_index_url(url)
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"codex-obsidian-sidecar/{__version__}",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - HTTPS checked
        if response.status != 200:
            raise RuntimeError(f"Update metadata returned HTTP {response.status}")
        value = json.loads(response.read(1_000_000))
    if not isinstance(value, dict):
        raise ValueError("Update metadata is not a JSON object")
    return value


def check_update(
    index_url: str,
    *,
    current_version: str = __version__,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value = (fetcher or _fetch_json)(index_url)
    info = value.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise ValueError("Update metadata does not contain info.version")
    try:
        current = Version(current_version)
        latest = Version(info["version"])
    except InvalidVersion as error:
        raise ValueError("Update metadata contains an invalid version") from error
    releases = value.get("releases", {})
    files = releases.get(str(latest), []) if isinstance(releases, dict) else []
    hashes = sorted(
        {
            str(item.get("digests", {}).get("sha256"))
            for item in files
            if isinstance(item, dict)
            and isinstance(item.get("digests"), dict)
            and item.get("digests", {}).get("sha256")
        }
    )
    return {
        "schema": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "package": PACKAGE_NAME,
        "source": index_url,
        "current_version": str(current),
        "latest_version": str(latest),
        "update_available": latest > current,
        "release_hashes": hashes,
        "install_method": "uv-tool-exact-version",
        "automatic_apply": False,
    }


def apply_update(
    update: dict[str, Any],
    *,
    executable: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not update.get("update_available"):
        return {**update, "status": "current"}
    latest = str(update.get("latest_version", ""))
    current = str(update.get("current_version", ""))
    try:
        Version(latest)
        Version(current)
    except InvalidVersion as error:
        raise ValueError("Refusing update with an invalid version") from error
    uv = shutil.which("uv")
    if not uv:
        raise FileNotFoundError("uv is required to apply sidecar updates")
    discovered = shutil.which("obsidian-sidecar")
    if executable is None and not discovered:
        raise FileNotFoundError("obsidian-sidecar executable is not on PATH")
    binary = executable or Path(str(discovered))
    command = [
        uv,
        "tool",
        "install",
        "--force",
        "--default-index",
        PYPI_SIMPLE_INDEX,
        f"{PACKAGE_NAME}=={latest}",
    ]
    result = runner(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"uv update failed: {result.stderr.strip()[:800]}")
    verified = runner(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if verified.returncode == 0 and latest in verified.stdout:
        return {**update, "status": "updated", "verified_version": latest}
    rollback = runner(
        [
            uv,
            "tool",
            "install",
            "--force",
            "--default-index",
            PYPI_SIMPLE_INDEX,
            f"{PACKAGE_NAME}=={current}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    raise RuntimeError(
        "Updated executable failed version verification; rollback "
        + ("succeeded" if rollback.returncode == 0 else "failed")
    )


def maybe_check_for_update(settings: Settings) -> dict[str, Any] | None:
    if not settings.update_checks_enabled or settings.runtime_role != "local":
        return None
    status_path = settings.state_dir / "update-status.json"
    if (
        status_path.exists()
        and time.time() - status_path.stat().st_mtime
        < settings.update_check_interval_seconds
    ):
        try:
            cached = json.loads(status_path.read_text(encoding="utf-8"))
            return cached if isinstance(cached, dict) else None
        except (OSError, json.JSONDecodeError):
            pass
    try:
        result = check_update(settings.update_index_url)
    except Exception as error:
        result = {
            "schema": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "status": "unavailable",
            "error": type(error).__name__,
            "automatic_apply": False,
        }
    _atomic_write(status_path, json.dumps(result, indent=2) + "\n")
    return result
