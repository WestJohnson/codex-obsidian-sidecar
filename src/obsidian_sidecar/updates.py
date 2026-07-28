from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version

from . import __version__
from .config import Settings
from .vault import _atomic_write


PACKAGE_NAME = "codex-obsidian-sidecar"
MAX_ARTIFACT_BYTES = 100_000_000


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


def _wheel_artifact(
    metadata: dict[str, Any],
    version: Version,
    *,
    index_url: str,
    required: bool,
) -> dict[str, Any] | None:
    releases = metadata.get("releases", {})
    files = releases.get(str(version), []) if isinstance(releases, dict) else []
    index_origin = urlparse(index_url)
    candidates: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        url = item.get("url")
        digests = item.get("digests")
        if (
            not isinstance(filename, str)
            or not filename.endswith(".whl")
            or not isinstance(url, str)
            or not isinstance(digests, dict)
        ):
            continue
        sha256 = digests.get("sha256")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in sha256)
        ):
            continue
        _validate_index_url(url)
        artifact_origin = urlparse(url)
        if artifact_origin.netloc != index_origin.netloc:
            raise ValueError("Release artifact URL must use the metadata origin")
        size = item.get("size")
        candidates.append(
            {
                "filename": filename,
                "url": url,
                "sha256": sha256.lower(),
                "size": int(size) if isinstance(size, int) and size >= 0 else None,
            }
        )
    if len(candidates) > 1:
        raise ValueError(f"Release {version} contains multiple wheel artifacts")
    if candidates:
        return candidates[0]
    if required:
        raise ValueError(f"Release {version} does not contain a verified wheel URL")
    return None


def check_update(
    index_url: str,
    *,
    current_version: str = __version__,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        value = (fetcher or _fetch_json)(index_url)
    except HTTPError as error:
        if error.code != 404:
            raise
        current = Version(current_version)
        return {
            "schema": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "package": PACKAGE_NAME,
            "source": index_url,
            "current_version": str(current),
            "latest_version": None,
            "update_available": False,
            "release_hashes": [],
            "artifact": None,
            "rollback_artifact": None,
            "install_method": "self-hosted-wheel-until-published",
            "automatic_apply": False,
            "status": "not-published",
        }
    if value.get("schema") != 1 or value.get("package") != PACKAGE_NAME:
        raise ValueError("Update metadata has an invalid schema or package")
    info = value.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise ValueError("Update metadata does not contain info.version")
    try:
        current = Version(current_version)
        latest = Version(info["version"])
    except InvalidVersion as error:
        raise ValueError("Update metadata contains an invalid version") from error
    artifact = _wheel_artifact(
        value,
        latest,
        index_url=index_url,
        required=True,
    )
    rollback_artifact = _wheel_artifact(
        value,
        current,
        index_url=index_url,
        required=False,
    )
    return {
        "schema": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "package": PACKAGE_NAME,
        "source": index_url,
        "current_version": str(current),
        "latest_version": str(latest),
        "update_available": latest > current,
        "release_hashes": [artifact["sha256"]],
        "artifact": artifact,
        "rollback_artifact": rollback_artifact,
        "install_method": "self-hosted-verified-wheel",
        "automatic_apply": False,
    }


def _download_artifact(
    artifact: dict[str, Any],
    directory: Path,
    *,
    timeout: int = 60,
) -> Path:
    url = str(artifact.get("url", ""))
    filename = str(artifact.get("filename", ""))
    expected = str(artifact.get("sha256", "")).lower()
    _validate_index_url(url)
    if not filename.endswith(".whl") or Path(filename).name != filename:
        raise ValueError("Release artifact filename must be a wheel basename")
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("Release artifact requires an exact SHA-256 digest")
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"codex-obsidian-sidecar/{__version__}",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - HTTPS checked
        if response.status != 200:
            raise RuntimeError(f"Release artifact returned HTTP {response.status}")
        payload = response.read(MAX_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ValueError("Release artifact exceeds the maximum supported size")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError("Release artifact SHA-256 verification failed")
    target = directory / filename
    target.write_bytes(payload)
    return target


def apply_update(
    update: dict[str, Any],
    *,
    executable: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    downloader: Callable[[dict[str, Any], Path], Path] | None = None,
) -> dict[str, Any]:
    if update.get("status") == "not-published":
        return update
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
    artifact = update.get("artifact")
    rollback_artifact = update.get("rollback_artifact")
    if not isinstance(artifact, dict):
        raise ValueError("Update metadata is missing the verified release artifact")
    if not isinstance(rollback_artifact, dict):
        raise ValueError(
            "Refusing update because no verified rollback artifact is available"
        )
    fetch = downloader or _download_artifact
    with tempfile.TemporaryDirectory(prefix="obsidian-sidecar-update-") as temp_name:
        directory = Path(temp_name)
        release_wheel = fetch(artifact, directory)
        rollback_wheel = fetch(rollback_artifact, directory)
        result = runner(
            [uv, "tool", "install", "--force", str(release_wheel)],
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
                str(rollback_wheel),
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
