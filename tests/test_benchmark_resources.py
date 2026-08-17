from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from obsidian_sidecar import benchmark
from obsidian_sidecar.benchmark import (
    FIXTURES,
    _background_service_health,
    _find_obsidian_cli,
    _fixture_json,
)
from obsidian_sidecar.config import Settings


def test_runtime_benchmark_fixtures_are_package_resources() -> None:
    assert "fixture-session-001" in FIXTURES.joinpath("transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert _fixture_json("valid-curation.json")["project_slug"] == "rainbow-joes"


def test_find_obsidian_cli_prefers_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: "/usr/bin/obsidian")

    assert _find_obsidian_cli() == "/usr/bin/obsidian"


def test_find_obsidian_cli_allows_optional_integration_to_be_absent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)
    monkeypatch.setattr(benchmark.Path, "is_file", lambda self: False)

    assert _find_obsidian_cli() is None


def test_background_service_health_uses_systemd_on_linux(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        vault_path=tmp_path / "vault",
        state_dir=tmp_path / "state",
        codex_bin=Path("/bin/false"),
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> CompletedProcess[str]:
        calls.append(command)
        if "is-enabled" in command:
            return CompletedProcess(command, 0, "enabled\n", "")
        if "is-active" in command:
            return CompletedProcess(command, 0, "active\n", "")
        return CompletedProcess(command, 0, "Result=success\nExecMainStatus=0\n", "")

    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(benchmark, "_run", fake_run)

    detail = _background_service_health(settings)

    assert "enabled and active" in detail
    assert calls == [
        [
            "/usr/bin/systemctl",
            "--user",
            "is-enabled",
            f"{settings.service_label}.timer",
        ],
        [
            "/usr/bin/systemctl",
            "--user",
            "is-active",
            f"{settings.service_label}.timer",
        ],
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            f"{settings.service_label}.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ],
    ]
