from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Callable

from .checkpoints import checkpoint_path, load_checkpoint
from .config import Settings
from .coordination import LocalWriterLease, cloud_lease_status
from .curator import CodexLunaCurator, StaticCurator
from .maintenance import commit_git_backup, inspect_vault
from .queueing import enqueue_event, ready_groups
from .security import REDACTION, contains_secret, redact_text
from .transcript import build_curation_packet, extract_messages
from .validation import validate_curation
from .vault import parse_frontmatter, write_curation, write_quarantine
from .worker import process_ready


FIXTURES = files("obsidian_sidecar").joinpath("fixtures")


@dataclass
class CaseResult:
    name: str
    weight: int
    critical: bool
    passed: bool
    detail: str
    duration_seconds: float


def _fixture_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _test_settings(base: Path, source: Settings) -> Settings:
    value = Settings(
        vault_path=base / "vault",
        state_dir=base / "state",
        codex_bin=source.codex_bin,
        model=source.model,
        reasoning_effort=source.reasoning_effort,
        debounce_seconds=0,
        curator_timeout_seconds=source.curator_timeout_seconds,
        minimum_confidence=source.minimum_confidence,
        basic_memory_project=source.basic_memory_project,
        auto_git_backup=False,
    )
    value.vault_path.mkdir(parents=True)
    value.ensure_runtime_dirs()
    return value


def _event(transcript: Path, cwd: Path) -> dict:
    return {
        "session_id": "benchmark-session-001",
        "turn_id": "benchmark-turn-001",
        "transcript_path": str(transcript),
        "cwd": str(cwd),
        "captured_at": "2026-07-14T08:01:00Z",
        "hook_event_name": "Stop",
    }


def _run(
    command: list[str], *, timeout: int = 120, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout, cwd=cwd
    )


@contextmanager
def _live_writer_guard(settings: Settings):
    active, reason, _ = cloud_lease_status(settings.vault_path)
    if active:
        raise RuntimeError(f"cloud maintenance lease is {reason}")
    with LocalWriterLease(
        settings.vault_path,
        ttl_seconds=max(900, settings.curator_timeout_seconds + 300),
    ):
        active, reason, _ = cloud_lease_status(settings.vault_path)
        if active:
            raise RuntimeError(f"cloud maintenance lease is {reason}")
        yield


def _app_server_request(codex_bin: Path, method: str, params: dict) -> dict:
    process = subprocess.Popen(
        [str(codex_bin), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def send(identifier: int, request_method: str, request_params: dict) -> None:
        process.stdin.write(
            json.dumps(
                {"id": identifier, "method": request_method, "params": request_params}
            )
            + "\n"
        )
        process.stdin.flush()

    def receive(identifier: int, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            value = json.loads(line)
            if value.get("id") == identifier:
                return value
        raise TimeoutError(f"Codex app-server did not answer request {identifier}")

    try:
        send(
            1,
            "initialize",
            {
                "clientInfo": {"name": "obsidian-sidecar-benchmark", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        initialized = receive(1)
        assert "result" in initialized, initialized
        send(2, method, params)
        response = receive(2)
        assert "result" in response, response
        return response["result"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_benchmark(settings: Settings) -> dict:
    results: list[CaseResult] = []
    live_cache: dict[str, dict] = {}

    def record(
        name: str, weight: int, critical: bool, function: Callable[[], str]
    ) -> None:
        started = time.monotonic()
        passed = False
        detail = ""
        try:
            detail = function()
            passed = True
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
        if len(detail) > 2_000:
            detail = f"{detail[:400]}\n... diagnostic truncated ...\n{detail[-1_550:]}"
        results.append(
            CaseResult(
                name=name,
                weight=weight,
                critical=critical,
                passed=passed,
                detail=detail,
                duration_seconds=round(time.monotonic() - started, 3),
            )
        )

    with tempfile.TemporaryDirectory(prefix="obsidian-sidecar-benchmark-") as temp_name:
        base = Path(temp_name)
        transcript = base / "transcript.jsonl"
        transcript.write_text(
            (FIXTURES / "transcript.jsonl").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        test_settings = _test_settings(base, settings)
        valid = _fixture_json("valid-curation.json")
        event = _event(transcript, base)

        def transcript_boundary() -> str:
            _, messages = extract_messages(transcript)
            combined = "\n".join(item.text for item in messages)
            forbidden = (
                "Secret internal instructions",
                "private-reasoning",
                "tool output must not be retained",
                "Intermediate commentary",
                "AGENTS.md instructions",
                "abcdefghijklmnopqrstuvwx",
            )
            assert [item.role for item in messages] == ["user", "assistant"]
            assert not any(value in combined for value in forbidden)
            assert REDACTION in combined
            return "Only user input and final answer survived; secret was redacted."

        record("transcript-boundary", 10, True, transcript_boundary)

        def secret_redaction() -> str:
            sample = (
                "sk-api-"
                + "abcdefghijklmnopqrstuvwxyz123456"
                + " password=correct-horse-battery-staple"
            )
            redacted, kinds = redact_text(sample)
            assert redacted.count(REDACTION) == 2
            assert not contains_secret(redacted)
            assert len(kinds) == 2
            return "Multiple credential classes were removed without retaining values."

        record("secret-redaction", 10, True, secret_redaction)

        packet = build_curation_packet(event)

        def grounding_guard() -> str:
            bad = deepcopy(valid)
            bad["changes"][0]["evidence_ids"] = ["a999"]
            validation = validate_curation(bad, packet, minimum_confidence=0.65)
            assert not validation.valid
            assert any("unknown evidence" in error for error in validation.errors)
            return "Unknown evidence references were rejected."

        record("grounding-guard", 8, True, grounding_guard)

        def queue_batching() -> str:
            for turn in ("one", "two", "three"):
                value = dict(event)
                value["turn_id"] = turn
                enqueue_event(test_settings, value)
            groups = ready_groups(test_settings, force=True)
            assert len(groups) == 1 and len(groups[0]) == 3
            return "Three turn events collapsed into one session curation group."

        record("queue-batching", 4, False, queue_batching)
        shutil.rmtree(test_settings.queue_dir)
        test_settings.queue_dir.mkdir()

        def idempotent_write() -> str:
            first = write_curation(test_settings, valid, packet, review_required=False)
            changed = deepcopy(valid)
            changed["summary"] = "Updated benchmark summary."
            second = write_curation(
                test_settings, changed, packet, review_required=False
            )
            assert first.note_path == second.note_path
            assert (
                len(list((test_settings.vault_path / "60 Sessions").rglob("*.md"))) == 1
            )
            metadata, body = parse_frontmatter(
                first.note_path.read_text(encoding="utf-8")
            )
            assert metadata["session_id"] == event["session_id"]
            assert "Updated benchmark summary" in body
            return "Repeated session write updated one atomically verified note."

        record("atomic-idempotent-write", 8, True, idempotent_write)

        def quarantine() -> str:
            target = write_quarantine(
                test_settings,
                session_id=event["session_id"],
                reason="benchmark invalid evidence",
                curation={"safe": True},
            )
            assert target.exists()
            assert "benchmark invalid evidence" in target.read_text(encoding="utf-8")
            return "Invalid curation was isolated from the knowledge graph."

        record("quarantine-path", 5, False, quarantine)

        def doctor_detection() -> str:
            bad = test_settings.vault_path / "broken.md"
            fake_key = "sk-api-" + "abcdefghijklmnopqrstuvwxyz123456"
            bad.write_text(
                f"---\ntitle: Broken\ntype: note\n---\n[[does-not-exist]]\n{fake_key}\n",
                encoding="utf-8",
            )
            health = inspect_vault(test_settings)
            assert "broken.md" in health.unresolved_links
            assert "broken.md" in health.possible_secret_files
            assert health.score < 80
            bad.unlink()
            return "Doctor lowered health below threshold for link and secret failures."

        record("vault-doctor-detection", 6, False, doctor_detection)

        def git_backup() -> str:
            result = commit_git_backup(test_settings, "test: benchmark snapshot")
            assert result == "ok"
            shown = _run(
                ["git", "-C", str(test_settings.vault_path), "show", "HEAD:.gitignore"],
                timeout=20,
            )
            assert shown.returncode == 0 and ".obsidian/workspace*" in shown.stdout
            return "A restorable Git snapshot was created after secret scanning."

        record("git-backup-snapshot", 4, False, git_backup)

        def obsidian_cli() -> str:
            marker = "sidecar-obsidian-e2e-7f9c"
            fixture = (
                settings.vault_path / "_System" / "Search Tests" / "obsidian-cli-e2e.md"
            )
            with _live_writer_guard(settings):
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text(
                    f"# Obsidian CLI E2E\n\n{marker}\n", encoding="utf-8"
                )
            time.sleep(1)
            result = _run(
                [
                    "/opt/homebrew/bin/obsidian",
                    f"vault={settings.vault_path.name}",
                    "search",
                    f"query={marker}",
                    "format=json",
                ],
                timeout=30,
            )
            combined = f"{result.stdout}\n{result.stderr}"
            assert result.returncode == 0
            assert "not enabled" not in combined.casefold()
            assert "obsidian-cli-e2e" in combined
            return "Official Obsidian CLI found a newly written fixture note."

        record("obsidian-cli-search", 5, True, obsidian_cli)

        def basic_memory_search() -> str:
            marker = "caldera memory bridge architecture"
            fixture = (
                settings.vault_path / "_System" / "Search Tests" / "basic-memory-e2e.md"
            )
            with _live_writer_guard(settings):
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text(
                    "---\ntitle: Caldera Retrieval Fixture\ntype: search-test\n---\n"
                    "The durable sidecar uses a caldera memory bridge architecture for this retrieval test.\n",
                    encoding="utf-8",
                )
            bm = shutil.which("bm") or str(Path.home() / ".local" / "bin" / "bm")
            reindex = _run(
                [bm, "reindex", "--project", settings.basic_memory_project],
                timeout=180,
            )
            assert reindex.returncode == 0, reindex.stderr
            status = _run(
                [
                    bm,
                    "status",
                    "--project",
                    settings.basic_memory_project,
                    "--wait",
                    "--timeout",
                    "90",
                    "--json",
                ],
                timeout=120,
            )
            assert status.returncode == 0, status.stderr
            lexical = _run(
                [
                    bm,
                    "tool",
                    "search-notes",
                    marker,
                    "--project",
                    settings.basic_memory_project,
                    "--local",
                ],
                timeout=90,
            )
            assert lexical.returncode == 0, lexical.stderr
            assert "Caldera Retrieval Fixture" in lexical.stdout, lexical.stdout
            hybrid = _run(
                [
                    bm,
                    "tool",
                    "search-notes",
                    "caldera persistent memory architecture",
                    "--hybrid",
                    "--project",
                    settings.basic_memory_project,
                    "--local",
                ],
                timeout=240,
            )
            assert hybrid.returncode == 0, hybrid.stderr
            assert "Caldera Retrieval Fixture" in hybrid.stdout, hybrid.stdout
            return "Basic Memory returned the fixture through lexical and semantic-hybrid retrieval."

        record("basic-memory-retrieval", 10, True, basic_memory_search)

        def installed_integration() -> str:
            response = _app_server_request(
                settings.codex_bin,
                "hooks/list",
                {"cwds": [str(Path.cwd())]},
            )
            entries = response.get("data", [])
            assert len(entries) == 1, entries
            assert not entries[0].get("errors"), entries[0].get("errors")
            sidecar_bin = shutil.which("obsidian-sidecar")
            assert sidecar_bin, "obsidian-sidecar is not on PATH"
            expected_command = f"{sidecar_bin} capture-hook"
            hooks = [
                item
                for item in entries[0].get("hooks", [])
                if item.get("eventName") == "stop"
                and item.get("command") == expected_command
            ]
            assert len(hooks) == 1, entries[0].get("hooks")
            hook = hooks[0]
            assert hook.get("enabled") is True
            assert hook.get("trustStatus") == "trusted", hook.get("trustStatus")

            launchd = _run(
                [
                    "launchctl",
                    "print",
                    f"gui/{os.getuid()}/{settings.service_label}",
                ],
                timeout=20,
            )
            assert launchd.returncode == 0, launchd.stderr
            assert "last exit code = 0" in launchd.stdout
            return "The installed Stop hook is enabled and trusted, and launchd last exited cleanly."

        record("installed-integration-health", 5, True, installed_integration)

        def live_luna() -> str:
            curation = CodexLunaCurator(test_settings).curate(packet)
            validation = validate_curation(
                curation,
                packet,
                minimum_confidence=test_settings.minimum_confidence,
            )
            assert validation.valid, "; ".join(validation.errors)
            assert not curation.get("skip")
            serialized = json.dumps(curation)
            assert "abcdefghijklmnopqrstuvwx" not in serialized
            assert "tool output must not be retained" not in serialized
            live_cache["curation"] = curation
            return f"Luna returned grounded structured output at confidence {curation['confidence']}."

        record("live-luna-curation", 15, True, live_luna)

        def live_pipeline() -> str:
            curation = live_cache.get("curation")
            assert curation is not None, (
                "live Luna case did not produce reusable output"
            )
            pipeline_base = base / "pipeline"
            pipeline_settings = _test_settings(pipeline_base, settings)
            pipeline_transcript = pipeline_base / "transcript.jsonl"
            pipeline_transcript.write_text(
                transcript.read_text(encoding="utf-8"), encoding="utf-8"
            )
            pipeline_event = _event(pipeline_transcript, pipeline_base)
            enqueue_event(pipeline_settings, pipeline_event)
            summary = process_ready(
                pipeline_settings,
                force=True,
                curator=StaticCurator(curation),
            )
            assert summary.failed == 0, asdict(summary)
            assert summary.notes_written == 1, asdict(summary)
            assert summary.checkpoint_updates == 1, asdict(summary)
            assert summary.reindex_result == "ok", asdict(summary)
            checkpoint = load_checkpoint(
                pipeline_settings, pipeline_event["session_id"]
            )
            assert checkpoint is not None, "pipeline did not persist a checkpoint"
            assert (
                checkpoint["cursor"]["byte_offset"]
                == pipeline_transcript.stat().st_size
            )
            stored_checkpoint = checkpoint_path(
                pipeline_settings, pipeline_event["session_id"]
            )
            assert stored_checkpoint.stat().st_mode & 0o777 == 0o600
            health = inspect_vault(pipeline_settings)
            assert health.critical_failures == 0, asdict(health)
            assert health.managed_notes >= 2, asdict(health)
            return (
                "Capture, curation validation, atomic write, private checkpoint, and "
                "doctor completed; installed indexing and retrieval passed its "
                "separate critical case."
            )

        record("live-complete-pipeline", 10, True, live_pipeline)

    score = sum(case.weight for case in results if case.passed)
    critical_failures = [
        case.name for case in results if case.critical and not case.passed
    ]
    passed = score >= 80 and not critical_failures
    output = {
        "benchmark": "obsidian-sidecar-e2e-v1",
        "ran_at": datetime.now(UTC).isoformat(),
        "threshold": 80,
        "score": score,
        "passed": passed,
        "critical_failures": critical_failures,
        "cases": [asdict(case) for case in results],
    }
    results_dir = settings.state_dir / "benchmark-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / "latest.json"
    result_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    report_path = settings.vault_path / "_System" / "Health" / "benchmark-latest.md"
    lines = [
        "---",
        "title: Obsidian Sidecar Benchmark",
        "type: system-health",
        f"updated: {output['ran_at']}",
        "managed_by: codex-obsidian-sidecar",
        "---",
        "",
        "# Obsidian Sidecar Benchmark",
        "",
        f"**Score:** {score}/100  ",
        "**Threshold:** 80/100  ",
        f"**Result:** {'PASS' if passed else 'FAIL'}",
        "",
        "## Cases",
        "",
    ]
    for case in results:
        lines.append(
            f"- {'PASS' if case.passed else 'FAIL'} `{case.name}` ({case.weight} points): {case.detail}"
        )
    with _live_writer_guard(settings):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
