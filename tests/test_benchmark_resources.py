from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from obsidian_sidecar import benchmark
from obsidian_sidecar.benchmark import (
    FIXTURES,
    _background_service_health,
    _benchmark_working_directory,
    _find_obsidian_cli,
    _find_sidecar_cli,
    _fixture_json,
    _obsidian_cli_search_succeeded,
)
from obsidian_sidecar.config import Settings
from obsidian_sidecar.coordination import CloudLease, LocalWriterLease


def test_runtime_benchmark_fixtures_are_package_resources() -> None:
    assert "fixture-session-001" in FIXTURES.joinpath("transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert _fixture_json("valid-curation.json")["project_slug"] == "rainbow-joes"


def test_find_obsidian_cli_prefers_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: "/usr/bin/obsidian")

    assert _find_obsidian_cli() == "/usr/bin/obsidian"


def test_find_obsidian_cli_returns_none_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)
    monkeypatch.setattr(benchmark.Path, "is_file", lambda self: False)

    assert _find_obsidian_cli() is None


def test_find_sidecar_cli_falls_back_to_absolute_invocation(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "obsidian-sidecar"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: None)
    monkeypatch.setattr(benchmark.sys, "argv", [str(executable)])

    assert _find_sidecar_cli() == str(executable.absolute())


def test_benchmark_working_directory_has_schema_safe_stable_name(
    tmp_path: Path,
) -> None:
    workspace = _benchmark_working_directory(tmp_path)

    assert workspace.name == "rainbow-joes"
    assert workspace.is_dir()


def test_obsidian_cli_search_requires_a_clean_matching_result() -> None:
    command = ["obsidian", "search"]

    assert _obsidian_cli_search_succeeded(
        CompletedProcess(command, 0, '[{"file":"obsidian-cli-e2e.md"}]', ""),
        "obsidian-cli-e2e",
    )
    assert not _obsidian_cli_search_succeeded(
        CompletedProcess(command, 133, "Loaded main app package", "core dumped"),
        "obsidian-cli-e2e",
    )


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
        return CompletedProcess(
            command,
            0,
            (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=1\n"
                "ExecMainStartTimestamp=Mon 2026-08-17 12:00:00 UTC\n"
            ),
            "",
        )

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
            "--property=ExecMainCode",
            "--property=ExecMainStartTimestamp",
        ],
    ]


def test_background_service_health_accepts_arch_oneshot_main_code_zero(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        vault_path=tmp_path / "vault",
        state_dir=tmp_path / "state",
        codex_bin=Path("/bin/false"),
    )

    def fake_run(command: list[str], **kwargs) -> CompletedProcess[str]:
        if "is-enabled" in command:
            return CompletedProcess(command, 0, "enabled\n", "")
        if "is-active" in command:
            return CompletedProcess(command, 0, "active\n", "")
        return CompletedProcess(
            command,
            0,
            (
                "Result=success\n"
                "ExecMainStatus=0\n"
                "ExecMainCode=0\n"
                "ExecMainStartTimestamp=Wed 2026-08-19 12:18:56 HST\n"
            ),
            "",
        )

    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(benchmark, "_run", fake_run)

    assert "last exited cleanly" in _background_service_health(settings)


def test_background_service_health_rejects_never_run_linux_service(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(
        vault_path=tmp_path / "vault",
        state_dir=tmp_path / "state",
        codex_bin=Path("/bin/false"),
    )

    def fake_run(command: list[str], **kwargs) -> CompletedProcess[str]:
        if "is-enabled" in command or "is-active" in command:
            return CompletedProcess(command, 0, "active\n", "")
        return CompletedProcess(
            command,
            0,
            "Result=success\nExecMainStatus=0\nExecMainCode=\nExecMainStartTimestamp=\n",
            "",
        )

    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    monkeypatch.setattr(benchmark.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(benchmark, "_run", fake_run)

    try:
        _background_service_health(settings)
    except AssertionError:
        return
    raise AssertionError("never-run systemd service should fail health check")


@pytest.mark.parametrize("lease_class", [LocalWriterLease, CloudLease])
@pytest.mark.parametrize("failed_case", [None, "critical", "optional"])
def test_report_publication_defers_without_losing_acceptance_result(
    settings, monkeypatch, capsys, lease_class, failed_case
):
    from obsidian_sidecar import cli

    cases = [benchmark.CaseResult("successful-case", 100, True, True, "verified", 1)]
    if failed_case:
        cases.append(
            benchmark.CaseResult(
                "failed-case", 0, failed_case == "critical", False, "failed", 1
            )
        )
    result_path = settings.state_dir / "benchmark-results/latest.json"
    report_path = settings.vault_path / "_System/Health/benchmark-latest.md"
    with lease_class(settings.vault_path, ttl_seconds=600) as lease:
        before = lease.path.read_bytes()
        output = benchmark._finish_benchmark(settings, cases)
        assert output["report_publication"]["status"] == "deferred"
        assert output["passed"] is (failed_case is None)
        assert output["score"] == 100
        assert json.loads(result_path.read_text()) == output
        assert result_path.stat().st_mode & 0o777 == 0o600
        assert not report_path.exists()
        assert lease.path.read_bytes() == before

    def no_live_cases(_settings):
        pytest.fail("Report retry must not rerun benchmark cases")

    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(benchmark, "run_benchmark", no_live_cases)
    assert cli.main(["benchmark", "--publish-only"]) == (
        0 if failed_case is None else 1
    )
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["report_publication"]["status"] == "published"
    assert recovered["cases"] == output["cases"]
    assert recovered["ran_at"] == output["ran_at"]
    assert recovered["passed"] == output["passed"]
    assert (
        "**Result:** PASS" if failed_case is None else "**Result:** FAIL"
    ) in report_path.read_text()
