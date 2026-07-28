from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
ARTIFACTS = RELEASE / "artifacts"
AGENT_FILES = (
    Path("AGENTS.md"),
    Path("README.md"),
    Path("LICENSE"),
    Path("SECURITY.md"),
    Path("CHANGELOG.md"),
    Path("config.example.json"),
    Path("docs/INSTALL.md"),
    Path("docs/KNOWLEDGE_STATE.md"),
    Path("docs/UPDATES.md"),
)
SECRET_PATTERNS = (
    re.compile(rb"sk-(?:api-|or-v1-)?[A-Za-z0-9_-]{32,}"),
    re.compile(rb"(?:ghp|github_pat)_[A-Za-z0-9_]{24,}"),
    re.compile(rb"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
)


def _run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    init = (ROOT / "src/obsidian_sidecar/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
    if not match or match.group(1) != version:
        raise RuntimeError("pyproject.toml and package versions do not match")
    return version


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _scan(name: str, data: bytes) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(data):
            raise RuntimeError(f"secret-like material found in release member {name}")


def _scan_archive(path: Path) -> None:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.file_size > 2_000_000:
                    continue
                _scan(f"{path.name}:{info.filename}", archive.read(info))
        return
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if not member.isfile() or member.size > 2_000_000:
                    continue
                handle = archive.extractfile(member)
                if handle:
                    _scan(f"{path.name}:{member.name}", handle.read())
        return
    _scan(path.name, path.read_bytes())


def _add_tree(archive: zipfile.ZipFile, source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = target / path.relative_to(source)
        data = path.read_bytes()
        _scan(relative.as_posix(), data)
        info = zipfile.ZipInfo(relative.as_posix(), (1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        archive.writestr(info, data)


def _agent_bundle(version: str, wheel: Path) -> Path:
    target = ARTIFACTS / f"codex-obsidian-sidecar-{version}-agent-bundle.zip"
    prefix = Path(f"codex-obsidian-sidecar-{version}")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in AGENT_FILES:
            data = (ROOT / relative).read_bytes()
            _scan(relative.as_posix(), data)
            info = zipfile.ZipInfo(
                (prefix / relative).as_posix(), (1980, 1, 1, 0, 0, 0)
            )
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
        _add_tree(
            archive,
            ROOT / ".agents",
            prefix / ".agents",
        )
        wheel_data = wheel.read_bytes()
        wheel_info = zipfile.ZipInfo(
            (prefix / "artifacts" / wheel.name).as_posix(),
            (1980, 1, 1, 0, 0, 0),
        )
        wheel_info.external_attr = 0o644 << 16
        archive.writestr(wheel_info, wheel_data)
    return target


def export(*, skip_tests: bool = False) -> dict[str, object]:
    version = _version()
    if RELEASE.exists():
        shutil.rmtree(RELEASE)
    ARTIFACTS.mkdir(parents=True)
    if not skip_tests:
        _run(["uv", "run", "--extra", "dev", "pytest"])
    _run(["uv", "build", "--out-dir", str(ARTIFACTS)])
    wheels = sorted(ARTIFACTS.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("release must contain exactly one wheel")
    bundle = _agent_bundle(version, wheels[0])
    files = sorted(
        path
        for path in ARTIFACTS.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    for path in files:
        _scan_archive(path)
    entries = [
        {
            "name": path.name,
            "sha256": _hash(path),
            "size": path.stat().st_size,
        }
        for path in files
    ]
    checksums = RELEASE / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{entry['sha256']}  artifacts/{entry['name']}\n" for entry in entries),
        encoding="utf-8",
    )
    manifest = {
        "schema": 1,
        "package": "codex-obsidian-sidecar",
        "version": version,
        "channel": "stable",
        "created_at": datetime.now(UTC).isoformat(),
        "artifacts": entries,
        "agent_bundle": bundle.name,
        "update": {
            "metadata": (
                "https://ai.westhawaiimarketing.com/charmfile/releases/"
                "sidecar/index.json"
            ),
            "apply": "obsidian-sidecar update --yes",
            "automatic_apply": False,
        },
        "security": {
            "secret_scan": "passed",
            "sha256sums": checksums.name,
            "github_attestation_required_for_public_release": True,
            "self_hosted_https_release_required": True,
        },
    }
    (RELEASE / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export(skip_tests=args.skip_tests), indent=2))


if __name__ == "__main__":
    main()
