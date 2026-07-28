from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from obsidian_sidecar import updates


INDEX_URL = (
    "https://ai.westhawaiimarketing.com/charmfile/releases/sidecar/index.json"
)


def _file(version: str, digest: str = "a" * 64) -> dict:
    filename = f"codex_obsidian_sidecar-{version}-py3-none-any.whl"
    return {
        "filename": filename,
        "url": (
            "https://ai.westhawaiimarketing.com/charmfile/releases/"
            f"sidecar/{version}/artifacts/{filename}"
        ),
        "digests": {"sha256": digest},
        "size": 1234,
    }


def _metadata(version: str, *, rollback: str | None = None) -> dict:
    releases = {version: [_file(version)]}
    if rollback is not None:
        releases[rollback] = [_file(rollback, "b" * 64)]
    return {
        "schema": 1,
        "package": "codex-obsidian-sidecar",
        "info": {"version": version},
        "releases": releases,
    }


def _artifact(version: str, digest: str = "a" * 64) -> dict:
    value = _file(version, digest)
    return {
        "filename": value["filename"],
        "url": value["url"],
        "sha256": value["digests"]["sha256"],
        "size": value["size"],
    }


def test_update_check_reports_exact_release_and_hash() -> None:
    result = updates.check_update(
        INDEX_URL,
        current_version="0.2.0",
        fetcher=lambda url: _metadata("0.3.0", rollback="0.2.0"),
    )

    assert result["update_available"] is True
    assert result["latest_version"] == "0.3.0"
    assert result["release_hashes"] == ["a" * 64]
    assert result["artifact"]["url"].endswith(
        "/0.3.0/artifacts/codex_obsidian_sidecar-0.3.0-py3-none-any.whl"
    )
    assert result["rollback_artifact"]["sha256"] == "b" * 64
    assert result["install_method"] == "self-hosted-verified-wheel"
    assert result["automatic_apply"] is False


def test_update_metadata_requires_https() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        updates._fetch_json("http://updates.example.test/release.json")


def test_update_check_reports_unpublished_package_without_failing() -> None:
    def missing(url: str) -> dict:
        raise HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    result = updates.check_update(
        INDEX_URL,
        current_version="0.3.0",
        fetcher=missing,
    )

    assert result["status"] == "not-published"
    assert result["current_version"] == "0.3.0"
    assert result["update_available"] is False
    assert result["install_method"] == "self-hosted-wheel-until-published"


def test_update_rejects_cross_origin_artifact() -> None:
    metadata = _metadata("0.3.0")
    metadata["releases"]["0.3.0"][0]["url"] = (
        "https://downloads.example.test/sidecar.whl"
    )
    with pytest.raises(ValueError, match="metadata origin"):
        updates.check_update(
            INDEX_URL,
            current_version="0.2.0",
            fetcher=lambda url: metadata,
        )


def test_download_artifact_verifies_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = b"verified wheel"

    class Response(io.BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        updates,
        "urlopen",
        lambda request, timeout: Response(payload),
    )
    artifact = _artifact("0.3.0", hashlib.sha256(payload).hexdigest())
    result = updates._download_artifact(artifact, tmp_path)

    assert result.read_bytes() == payload


def test_download_artifact_rejects_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        updates,
        "urlopen",
        lambda request, timeout: Response(b"wrong"),
    )
    with pytest.raises(ValueError, match="SHA-256"):
        updates._download_artifact(_artifact("0.3.0"), tmp_path)


def test_update_applies_exact_version_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        output = "obsidian-sidecar 0.3.0\n" if "--version" in command else ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/local/bin/uv")

    def downloader(artifact: dict, directory: Path) -> Path:
        wheel = directory / str(artifact["filename"])
        wheel.write_bytes(b"wheel")
        return wheel

    result = updates.apply_update(
        {
            "update_available": True,
            "current_version": "0.2.0",
            "latest_version": "0.3.0",
            "artifact": _artifact("0.3.0"),
            "rollback_artifact": _artifact("0.2.0"),
        },
        executable=Path("/usr/local/bin/obsidian-sidecar"),
        runner=runner,
        downloader=downloader,
    )

    assert result["status"] == "updated"
    assert calls[0][-1].endswith(
        "codex_obsidian_sidecar-0.3.0-py3-none-any.whl"
    )
    assert "--default-index" not in calls[0]
    assert len(calls) == 2


def test_failed_verification_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if "--version" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="broken")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/local/bin/uv")

    def downloader(artifact: dict, directory: Path) -> Path:
        wheel = directory / str(artifact["filename"])
        wheel.write_bytes(b"wheel")
        return wheel

    with pytest.raises(RuntimeError, match="rollback succeeded"):
        updates.apply_update(
            {
                "update_available": True,
                "current_version": "0.2.0",
                "latest_version": "0.3.0",
                "artifact": _artifact("0.3.0"),
                "rollback_artifact": _artifact("0.2.0"),
            },
            executable=Path("/usr/local/bin/obsidian-sidecar"),
            runner=runner,
            downloader=downloader,
        )

    assert calls[-1][-1].endswith(
        "codex_obsidian_sidecar-0.2.0-py3-none-any.whl"
    )


def test_update_refuses_without_verified_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/local/bin/uv")
    with pytest.raises(ValueError, match="rollback artifact"):
        updates.apply_update(
            {
                "update_available": True,
                "current_version": "0.2.0",
                "latest_version": "0.3.0",
                "artifact": _artifact("0.3.0"),
                "rollback_artifact": None,
            },
            executable=Path("/usr/local/bin/obsidian-sidecar"),
        )
