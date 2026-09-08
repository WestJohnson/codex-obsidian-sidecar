from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from threading import Event, local

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


@pytest.mark.parametrize("publication_contended", [False, True])
def test_overlapping_benchmarks_keep_their_own_results_and_exit_codes(
    settings, monkeypatch, capsys, publication_contended
):
    from obsidian_sidecar import cli
    from obsidian_sidecar.queueing import load_event

    result_path = settings.state_dir / "benchmark-results/latest.json"
    report_path = settings.vault_path / "_System/Health/benchmark-latest.md"
    cases = {
        "failed": [benchmark.CaseResult("failed-A", 100, True, False, "failed", 1)],
        "passed": [benchmark.CaseResult("passed-B", 100, True, True, "verified", 1)],
    }
    role = local()
    saved_failure, release_failure, second_cases_done = Event(), Event(), Event()
    outputs = {}
    save = benchmark.save_event

    def pause_failed_save(path, output):
        save(path, output)
        if (
            path == result_path
            and role.name == "failed"
            and not saved_failure.is_set()
        ):
            saved_failure.set()
            assert release_failure.wait(5)

    def finish_cases(active_settings):
        if role.name == "passed":
            second_cases_done.set()
        output = benchmark._finish_benchmark(active_settings, cases[role.name])
        outputs[role.name] = output
        return output

    def run(name):
        role.name = name
        return cli.main(["benchmark"])

    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(benchmark, "save_event", pause_failed_save)
    monkeypatch.setattr(benchmark, "run_benchmark", finish_cases)
    lease = (
        CloudLease(settings.vault_path, ttl_seconds=600)
        if publication_contended
        else nullcontext()
    )
    with lease, ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(run, "failed")
        try:
            assert saved_failure.wait(5)
            passed = pool.submit(run, "passed")
            assert second_cases_done.wait(5)
            with pytest.raises(FutureTimeout):
                passed.result(timeout=0.1)
            assert load_event(result_path)["passed"] is False
        finally:
            release_failure.set()
        assert failed.result(timeout=5) == 1
        assert passed.result(timeout=5) == 0
    assert outputs["failed"]["passed"] is False
    assert outputs["failed"]["score"] == 0
    assert outputs["failed"]["critical_failures"] == ["failed-A"]
    assert outputs["failed"]["cases"][0]["name"] == "failed-A"
    assert outputs["passed"]["passed"] is True
    assert outputs["passed"]["score"] == 100
    assert outputs["passed"]["cases"][0]["name"] == "passed-B"
    assert load_event(result_path) == outputs["passed"]
    expected_publication = "deferred" if publication_contended else "published"
    for output in outputs.values():
        assert output["report_publication"]["status"] == expected_publication
    assert result_path.stat().st_mode & 0o777 == 0o600
    if publication_contended:
        assert not report_path.exists()
    else:
        assert "`passed-B`" in report_path.read_text()
    capsys.readouterr()


@pytest.mark.parametrize("new_passed", [False, True])
def test_publish_only_cannot_overwrite_newer_acceptance_result(
    settings, monkeypatch, capsys, new_passed
):
    from obsidian_sidecar import cli
    from obsidian_sidecar.queueing import load_event

    result_path = settings.state_dir / "benchmark-results/latest.json"
    report_path = settings.vault_path / "_System/Health/benchmark-latest.md"
    old_cases = [
        benchmark.CaseResult("old-A", 100, True, not new_passed, "original", 1)
    ]
    new_cases = [
        benchmark.CaseResult("new-B", 100, True, new_passed, "new result", 2)
    ]
    with CloudLease(settings.vault_path, ttl_seconds=600):
        original = benchmark._finish_benchmark(settings, old_cases)
    assert original["report_publication"]["status"] == "deferred"
    publishing, release_publication, new_cases_done = Event(), Event(), Event()
    write = benchmark._atomic_write

    def pause_publication(path, content):
        if path == report_path and not publishing.is_set():
            publishing.set()
            assert release_publication.wait(5)
        write(path, content)

    def finish_new_cases():
        new_cases_done.set()
        return benchmark._finish_benchmark(settings, new_cases)

    def forbidden(_settings):
        pytest.fail("publish-only must not rerun benchmark cases")

    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(benchmark, "run_benchmark", forbidden)
    monkeypatch.setattr(benchmark, "_atomic_write", pause_publication)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(cli.main, ["benchmark", "--publish-only"])
        try:
            assert publishing.wait(5)
            newer = pool.submit(finish_new_cases)
            assert new_cases_done.wait(5)
            with pytest.raises(FutureTimeout):
                newer.result(timeout=0.1)
            assert load_event(result_path) == original
        finally:
            release_publication.set()
        assert publication.result(timeout=5) == (0 if original["passed"] else 1)
        latest = newer.result(timeout=5)
    published = json.loads(capsys.readouterr().out)
    assert published == {**original, "report_publication": {"status": "published"}}
    assert latest["passed"] is new_passed
    assert latest["score"] == (100 if new_passed else 0)
    assert latest["cases"][0]["name"] == "new-B"
    assert latest["report_publication"]["status"] == "published"
    assert load_event(result_path) == latest
    assert "`new-B`" in report_path.read_text()
    assert "`old-A`" not in report_path.read_text()
