from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_sidecar.config import Settings


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def record_runtime_evidence(tmp_path: Path):
    """Export synthetic API results and persisted runtime contracts on request."""

    def record(name: str, value: dict) -> None:
        directory = os.environ.get("SIDECAR_TEST_EVIDENCE_DIR")
        if not directory:
            return
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        content = json.dumps(value, indent=2, default=str)
        (output / f"{name}.json").write_text(
            content.replace(str(tmp_path), "<TEST_ROOT>") + "\n",
            encoding="utf-8",
        )

    return record


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(
        "obsidian_sidecar.maintenance.basic_memory_binary", lambda: None
    )
    monkeypatch.setattr(
        "obsidian_sidecar.maintenance._command_status", lambda *_: "unavailable"
    )
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    vault.mkdir()
    value = Settings(
        vault_path=vault,
        state_dir=state,
        codex_bin=Path("/bin/false"),
        debounce_seconds=0,
        minimum_confidence=0.65,
        auto_git_backup=False,
    )
    value.ensure_runtime_dirs()
    return value


@pytest.fixture
def transcript_path(tmp_path: Path) -> Path:
    target = tmp_path / "fixture-transcript.jsonl"
    target.write_text(
        (FIXTURES / "transcript.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return target


@pytest.fixture
def valid_curation() -> dict:
    return json.loads((FIXTURES / "valid-curation.json").read_text(encoding="utf-8"))
