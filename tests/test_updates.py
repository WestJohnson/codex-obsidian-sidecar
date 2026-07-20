from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from obsidian_sidecar import updates


def _metadata(version: str) -> dict:
    return {
        "info": {"version": version},
        "releases": {
            version: [
                {
                    "filename": f"sidecar-{version}.whl",
                    "digests": {"sha256": "a" * 64},
                }
            ]
        },
    }


def test_update_check_reports_exact_release_and_hash() -> None:
    result = updates.check_update(
        "https://pypi.org/pypi/codex-obsidian-sidecar/json",
        current_version="0.2.0",
        fetcher=lambda url: _metadata("0.3.0"),
    )

    assert result["update_available"] is True
    assert result["latest_version"] == "0.3.0"
    assert result["release_hashes"] == ["a" * 64]
    assert result["automatic_apply"] is False


def test_update_metadata_requires_https() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        updates._fetch_json("http://updates.example.test/release.json")


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
    result = updates.apply_update(
        {
            "update_available": True,
            "current_version": "0.2.0",
            "latest_version": "0.3.0",
        },
        executable=Path("/usr/local/bin/obsidian-sidecar"),
        runner=runner,
    )

    assert result["status"] == "updated"
    assert "codex-obsidian-sidecar==0.3.0" in calls[0]
    assert "--default-index" in calls[0]
    assert len(calls) == 2


def test_failed_verification_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
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

    with pytest.raises(RuntimeError, match="rollback succeeded"):
        updates.apply_update(
            {
                "update_available": True,
                "current_version": "0.2.0",
                "latest_version": "0.3.0",
            },
            executable=Path("/usr/local/bin/obsidian-sidecar"),
            runner=runner,
        )

    assert "codex-obsidian-sidecar==0.2.0" in calls[-1]
