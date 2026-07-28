from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from packaging.version import Version


PACKAGE_NAME = "codex-obsidian-sidecar"
DEFAULT_BASE_URL = (
    "https://ai.westhawaiimarketing.com/charmfile/releases/sidecar"
)
WHEEL_NAME = re.compile(
    r"^codex_obsidian_sidecar-(?P<version>[0-9A-Za-z.+-]+)-py3-none-any\.whl$"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_index(wheels: list[Path], base_url: str) -> dict:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Release base URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("Release base URL must not contain credentials")
    releases: dict[str, list[dict]] = {}
    for wheel in wheels:
        match = WHEEL_NAME.fullmatch(wheel.name)
        if not match:
            raise ValueError(f"Unsupported wheel filename: {wheel.name}")
        version = str(Version(match.group("version")))
        if version in releases:
            raise ValueError(f"Duplicate wheel version: {version}")
        releases[version] = [
            {
                "filename": wheel.name,
                "url": (
                    f"{base_url.rstrip('/')}/{version}/artifacts/{wheel.name}"
                ),
                "digests": {"sha256": _sha256(wheel)},
                "size": wheel.stat().st_size,
            }
        ]
    if not releases:
        raise ValueError("At least one release wheel is required")
    latest = str(max((Version(version) for version in releases)))
    return {
        "schema": 1,
        "package": PACKAGE_NAME,
        "generated_at": datetime.now(UTC).isoformat(),
        "info": {"version": latest},
        "releases": dict(
            sorted(releases.items(), key=lambda item: Version(item[0]))
        ),
        "source": {
            "git": (
                "https://ai.westhawaiimarketing.com/charmfile/git/"
                "codex-obsidian-sidecar.git"
            ),
            "release_channel": base_url.rstrip("/"),
        },
    }
