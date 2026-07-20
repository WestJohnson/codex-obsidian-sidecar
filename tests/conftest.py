from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_sidecar.config import Settings


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
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
