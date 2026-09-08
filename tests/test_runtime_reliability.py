from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from obsidian_sidecar.alerts import alert_status
from obsidian_sidecar.coordination import (
    CloudLease,
    LeaseBusy,
    LocalWriterLease,
    cloud_lease_status,
)
from obsidian_sidecar.curator import StaticCurator
from obsidian_sidecar.installer import _config_bytes, SetupOptions, setup_plan
from obsidian_sidecar.maintenance import VaultHealth
from obsidian_sidecar.queueing import (
    capture_hook,
    enqueue_event,
    load_event,
    recover_captures,
)
from obsidian_sidecar.worker import (
    ProcessLock,
    daemon_once,
    process_ready,
    run_maintenance,
    _run_git_checkpoint,
)


SESSION = "11111111-2222-4333-8444-555555555555"


def incomplete_hook(settings, monkeypatch, tmp_path, *, payload=None):
    root = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(root))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                payload
                or {
                    "session_id": SESSION,
                    "cwd": "/tmp/project",
                    "content": "do not retain me",
                }
            )
        ),
    )
    assert capture_hook(settings) == 0
    return root


def make_transcript(root, *, session=SESSION, directory="sessions", cwd="/tmp/project"):
    path = root / directory / f"rollout-test-{SESSION}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": session, "cwd": cwd}})
        + "\n"
    )
    return path


@pytest.mark.parametrize("directory", ["sessions", "archived_sessions"])
def test_capture_recovers_only_exact_session(
    settings, monkeypatch, tmp_path, directory
):
    root = incomplete_hook(settings, monkeypatch, tmp_path)
    pending = next(settings.capture_pending_dir.glob("*.json"))
    assert "do not retain me" not in pending.read_text()
    assert recover_captures(settings) == 0
    transcript = make_transcript(root, directory=directory)
    assert recover_captures(settings) == 1
    event = load_event(next(settings.queue_dir.glob("*.json")))
    assert event["transcript_path"] == str(transcript)
    assert event["session_id"] == SESSION
    assert not list(settings.capture_pending_dir.glob("*.json"))
    assert recover_captures(settings) == 0


@pytest.mark.parametrize("mismatch", ["session", "cwd", "ambiguous", "symlink"])
def test_capture_does_not_guess(settings, monkeypatch, tmp_path, mismatch):
    root = incomplete_hook(settings, monkeypatch, tmp_path)
    if mismatch == "session":
        make_transcript(root, session="different")
    elif mismatch == "cwd":
        make_transcript(root, cwd="/another/project")
    elif mismatch == "ambiguous":
        make_transcript(root)
        make_transcript(root, directory="archived_sessions")
    else:
        outside = make_transcript(tmp_path / "outside")
        (root / "sessions").mkdir(parents=True)
        (root / "sessions" / outside.name).symlink_to(outside)
    assert recover_captures(settings) == 0
    assert not list(settings.queue_dir.glob("*.json"))


def test_unresolved_capture_becomes_visible_not_discarded(
    settings, monkeypatch, tmp_path
):
    incomplete_hook(settings, monkeypatch, tmp_path)
    path = next(settings.capture_pending_dir.glob("*.json"))
    now = datetime.now(UTC)
    assert alert_status(settings, now=now)["healthy"]
    os.utime(path, (now.timestamp() - 1801,) * 2)
    status = alert_status(settings, now=now)
    assert not status["healthy"]
    assert status["capture"]["stalled"] == 1
    assert recover_captures(settings, now=now + timedelta(days=2)) == 0
    assert len(list(settings.capture_failed_dir.glob("*.json"))) == 1
    health = VaultHealth(
        checked_at=now.isoformat(),
        capture_failed=1,
        obsidian_cli="ok",
        basic_memory="ok",
        git_backup="ok",
    )
    assert health.score < 80


@pytest.mark.parametrize("payload", ["not json private-content", "[]", "null"])
def test_invalid_hook_input_is_fail_open_and_private(settings, monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert capture_hook(settings) == 0
    failure = next(settings.capture_failed_dir.glob("*.json"))
    assert "private-content" not in failure.read_text()
    assert not alert_status(settings)["healthy"]


def test_hook_remains_nonblocking_if_storage_is_broken(settings, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not-json"))
    monkeypatch.setattr(
        "obsidian_sidecar.queueing._atomic_json",
        lambda *_: (_ for _ in ()).throw(OSError("full")),
    )
    assert capture_hook(settings) == 0


def test_busy_local_writer_defers_without_consuming_capture(
    settings, transcript_path, valid_curation
):
    event = enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "transcript_path": str(transcript_path),
            "turn_id": "busy",
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )
    before = event.read_bytes()
    with LocalWriterLease(settings.vault_path, ttl_seconds=600):
        result = process_ready(
            settings, force=True, curator=StaticCurator(valid_curation)
        )
        assert result.deferred_reason == "local-writer-active"
        assert result.failed == 0
        assert event.read_bytes() == before
        assert run_maintenance(settings)["deferred_reason"] == "local-writer-active"
        assert _run_git_checkpoint(settings) == {
            "status": "deferred",
            "reason": "local-writer-active",
        }
    assert not (settings.state_dir / "git-checkpoint.json").exists()
    result = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert result.failed == 0
    assert result.processed_events == 1


@pytest.mark.parametrize("lease_class", [CloudLease, LocalWriterLease])
def test_deferred_maintenance_does_not_advance_backup_clock(settings, lease_class):
    configured = replace(settings, auto_git_backup=True)
    with lease_class(settings.vault_path, ttl_seconds=600):
        result = daemon_once(configured)
    assert result["maintenance"]["backup_result"] == "deferred"
    assert not (settings.state_dir / "git-checkpoint.json").exists()


def test_failed_backup_retries_next_tick(settings, monkeypatch):
    monkeypatch.setattr(
        "obsidian_sidecar.worker.commit_git_backup", lambda *_: "unavailable"
    )
    assert _run_git_checkpoint(settings)["status"] == "unavailable"
    assert not (settings.state_dir / "git-checkpoint.json").exists()


@pytest.mark.parametrize("value", [[], None, 42, {"expires_at": "broken"}])
def test_malformed_lease_fails_closed_without_type_crash(settings, value):
    path = settings.vault_path / "_System/Coordination/cloud-maintenance.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value))
    assert cloud_lease_status(settings.vault_path)[:2] == (
        True,
        "malformed-fail-closed",
    )
    with pytest.raises(LeaseBusy):
        with CloudLease(settings.vault_path, ttl_seconds=600):
            pass


def test_process_lock_does_not_expire_while_owner_alive(settings):
    path = settings.lock_dir / "worker.lock"
    with ProcessLock(path, stale_seconds=0) as first:
        assert first.acquired
        with ProcessLock(path, stale_seconds=0) as second:
            assert not second.acquired
    with ProcessLock(path) as third:
        assert third.acquired


def test_setup_policy_override_is_explicit_and_preserved(settings, tmp_path):
    config = tmp_path / "config.json"
    options = SetupOptions(
        vault_path=settings.vault_path,
        state_dir=settings.state_dir,
        codex_bin=settings.codex_bin,
        config_path=config,
        executable=settings.codex_bin,
        install_service=False,
        freshness_project_days=90,
    )
    config.write_bytes(_config_bytes(options))
    assert (
        json.loads(_config_bytes(replace(options, freshness_project_days=None)))[
            "freshness_project_days"
        ]
        == 90
    )
    with pytest.raises(ValueError, match="freshness days"):
        setup_plan(replace(options, freshness_project_days=0))


def test_stop_without_turn_id_retains_each_cutoff(settings, transcript_path):
    event = {"session_id": SESSION, "transcript_path": str(transcript_path)}
    first = enqueue_event(settings, {**event, "captured_at": "2026-07-14T08:01:00Z"})
    second = enqueue_event(settings, {**event, "captured_at": "2026-07-14T08:02:00Z"})
    assert first != second
    assert len(list(settings.queue_dir.glob("*.json"))) == 2


def test_worker_status_reports_exceptions_without_private_detail(settings, monkeypatch):
    from obsidian_sidecar import worker
    from obsidian_sidecar.queueing import runtime_problem

    def broken(_):
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(worker, "_daemon_once", broken)
    with pytest.raises(RuntimeError):
        daemon_once(settings)
    state = settings.state_dir / "worker-status.json"
    assert "private failure detail" not in state.read_text()
    assert runtime_problem(settings) == "worker-error"
    assert not alert_status(settings)["healthy"]
    monkeypatch.setattr(worker, "_daemon_once", lambda _: {"processing": {}})
    daemon_once(settings)
    assert runtime_problem(settings) is None


def test_runtime_staleness_and_long_deferral_are_visible(settings, monkeypatch):
    from obsidian_sidecar import worker
    from obsidian_sidecar.queueing import runtime_problem, save_event

    now = datetime.now(UTC)
    status_path = settings.state_dir / "worker-status.json"
    save_event(
        status_path,
        {"checked_at": (now - timedelta(hours=1)).isoformat(), "status": "ok"},
    )
    assert runtime_problem(settings, now=now) == "worker-stale"
    save_event(
        status_path,
        {
            "checked_at": now.isoformat(),
            "status": "deferred",
            "deferred_since": (now - timedelta(hours=1)).isoformat(),
        },
    )
    monkeypatch.setattr(
        worker,
        "_daemon_once",
        lambda _: {"processing": {"deferred_reason": "local-writer-active"}},
    )
    daemon_once(settings)
    assert runtime_problem(settings, now=now) == "worker-stalled"
    assert alert_status(settings, now=now)["alerts"][0]["code"] == "worker-stalled"
    monkeypatch.setattr(worker, "_daemon_once", lambda _: {"processing": {}})
    daemon_once(settings)
    assert runtime_problem(settings, now=now) is None


def test_daemon_overlap_does_not_publish_a_false_heartbeat(settings):
    with ProcessLock(settings.lock_dir / "daemon.lock"):
        assert daemon_once(settings)["reason"] == "local-daemon-lock"
    assert not (settings.state_dir / "worker-status.json").exists()


def test_basic_memory_observation_is_not_index_readiness(settings, monkeypatch):
    import subprocess
    from obsidian_sidecar import maintenance

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"total_files": 1, "observed_files": [{"path": "note.md"}]}),
            "",
        )

    monkeypatch.setattr(maintenance, "basic_memory_binary", lambda: "/bin/bm")
    monkeypatch.setattr(maintenance.subprocess, "run", run)
    assert maintenance.basic_memory_status(settings) == "observation-only"
    assert maintenance.reindex_basic_memory(settings) == "ok"
    assert calls[-1] == [
        "/bin/bm",
        "reindex",
        "--project",
        settings.basic_memory_project,
        "--search",
    ]


def test_basic_memory_reindex_failure_is_not_success(settings, monkeypatch):
    import subprocess
    from obsidian_sidecar import maintenance

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1 if command[1] == "reindex" else 0,
            json.dumps({"observed_files": []}),
            "",
        )

    monkeypatch.setattr(maintenance, "basic_memory_binary", lambda: "/bin/bm")
    monkeypatch.setattr(maintenance.subprocess, "run", run)
    assert maintenance.reindex_basic_memory(settings) == "error"
