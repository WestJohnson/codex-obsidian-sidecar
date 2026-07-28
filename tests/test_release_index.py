from pathlib import Path

import pytest

from obsidian_sidecar.release_index import build_index


def _wheel(directory: Path, version: str, payload: bytes) -> Path:
    path = (
        directory
        / f"codex_obsidian_sidecar-{version}-py3-none-any.whl"
    )
    path.write_bytes(payload)
    return path


def test_build_update_index_orders_releases_and_selects_latest(
    tmp_path: Path,
) -> None:
    old = _wheel(tmp_path, "0.5.1", b"old")
    current = _wheel(tmp_path, "0.6.0", b"current")
    result = build_index(
        [current, old],
        "https://updates.example.test/charmfile/releases/sidecar",
    )

    assert result["schema"] == 1
    assert result["package"] == "codex-obsidian-sidecar"
    assert result["info"]["version"] == "0.6.0"
    assert list(result["releases"]) == ["0.5.1", "0.6.0"]
    assert result["releases"]["0.6.0"][0]["url"].endswith(
        "/0.6.0/artifacts/codex_obsidian_sidecar-0.6.0-py3-none-any.whl"
    )
    assert len(result["releases"]["0.5.1"][0]["digests"]["sha256"]) == 64


def test_build_update_index_requires_https(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path, "0.6.0", b"current")
    with pytest.raises(ValueError, match="must use HTTPS"):
        build_index([wheel], "http://updates.example.test/sidecar")
