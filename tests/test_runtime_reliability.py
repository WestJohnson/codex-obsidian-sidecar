from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    capture_health,
    enqueue_event,
    load_event,
    ready_groups,
    recover_captures,
    save_event,
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


@pytest.mark.parametrize(
    "mismatch",
    [
        "session",
        "cwd",
        "ambiguous",
        "symlink",
        "directory-symlink",
        "archived-directory-symlink",
    ],
)
def test_capture_does_not_guess(settings, monkeypatch, tmp_path, mismatch):
    root = incomplete_hook(settings, monkeypatch, tmp_path)
    pending = next(settings.capture_pending_dir.glob("*.json"))
    original_capture = pending.read_bytes()
    if mismatch == "session":
        make_transcript(root, session="different")
    elif mismatch == "cwd":
        make_transcript(root, cwd="/another/project")
    elif mismatch == "ambiguous":
        make_transcript(root)
        make_transcript(root, directory="archived_sessions")
    elif mismatch == "symlink":
        outside = make_transcript(tmp_path / "outside")
        (root / "sessions").mkdir(parents=True)
        (root / "sessions" / outside.name).symlink_to(outside)
    else:
        outside = make_transcript(tmp_path / "outside")
        root.mkdir()
        directory = (
            "archived_sessions"
            if mismatch == "archived-directory-symlink"
            else "sessions"
        )
        (root / directory).symlink_to(outside.parent, target_is_directory=True)
    recovered = recover_captures(settings)
    if mismatch == "directory-symlink" and os.environ.get("SIDECAR_TEST_EVIDENCE_DIR"):
        evidence_dir = Path(os.environ["SIDECAR_TEST_EVIDENCE_DIR"])
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence = {
            "scope": "Synthetic external transcript behind a sessions directory symlink",
            "recorded_codex_home": str(root),
            "sessions_resolved_to": str((root / "sessions").resolve()),
            "expected_recovered_count": 0,
            "actual_recovered_count": recovered,
            "queued_events": [
                load_event(path) for path in settings.queue_dir.glob("*.json")
            ],
        }
        (evidence_dir / "capture-directory-symlink.json").write_text(
            json.dumps(evidence, indent=2).replace(str(tmp_path), "<TEST_ROOT>") + "\n"
        )
    assert recovered == 0
    assert not list(settings.queue_dir.glob("*.json"))
    assert pending.read_bytes() == original_capture


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


def test_recovered_capture_uses_latest_evidence_cutoff(
    settings, monkeypatch, tmp_path, transcript_path, valid_curation
):
    from obsidian_sidecar.checkpoints import load_checkpoint

    monkeypatch.setattr(
        "obsidian_sidecar.queueing.utc_now", lambda: "2026-07-14T08:01:00Z"
    )
    root = incomplete_hook(settings, monkeypatch, tmp_path)
    transcript = make_transcript(root)
    transcript.write_text(
        transcript_path.read_text()
        .replace("fixture-session-001", SESSION)
        .replace("/tmp/rainbow-joes", "/tmp/project")
    )
    with transcript.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Retain the later capture evidence.",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    newer = enqueue_event(
        settings,
        {
            "session_id": SESSION,
            "turn_id": "newer",
            "transcript_path": str(transcript),
            "captured_at": "2026-07-14T08:03:00Z",
        },
    )
    os.utime(newer, (datetime.now(UTC).timestamp() - 60,) * 2)
    assert recover_captures(settings) == 1
    assert ready_groups(settings, force=True)[0][-1] == newer
    packets = []

    class RecordingCurator:
        def curate(self, packet):
            packets.append(packet)
            return valid_curation

    monkeypatch.setattr("obsidian_sidecar.worker.reindex_basic_memory", lambda _: "ok")
    result = process_ready(settings, force=True, curator=RecordingCurator())
    assert result.failed == 0
    assert result.processed_events == 2
    assert packets[0]["captured_at"] == "2026-07-14T08:03:00Z"
    assert any(
        "Retain the later capture evidence." in item["text"]
        for item in packets[0]["evidence"]
    )
    assert (
        load_checkpoint(settings, SESSION)["cursor"]["byte_offset"]
        == transcript.stat().st_size
    )


@pytest.mark.parametrize("cutoff", [None, "bad-time", "2026-07-14T08:01:00"])
def test_invalid_cutoff_is_preserved_as_failed_event(
    settings, transcript_path, valid_curation, monkeypatch, cutoff
):
    event = {
        "session_id": "fixture-session-001",
        "transcript_path": str(transcript_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    enqueue_event(settings, event)
    invalid = settings.queue_dir / "invalid-cutoff.json"
    save_event(invalid, {**event, "captured_at": cutoff})
    monkeypatch.setattr("obsidian_sidecar.worker.reindex_basic_memory", lambda _: "ok")
    result = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert result.processed_events == 1
    assert result.failed == 1
    assert load_event(settings.failed_dir / invalid.name)["captured_at"] == cutoff


def test_debounce_uses_latest_arrival_even_when_its_cutoff_is_older(
    settings, transcript_path
):
    configured = replace(settings, debounce_seconds=180)
    event = {"session_id": SESSION, "transcript_path": str(transcript_path)}
    newer = enqueue_event(configured, {**event, "captured_at": "2026-07-14T08:03:00Z"})
    os.utime(newer, (datetime.now(UTC).timestamp() - 600,) * 2)
    older = enqueue_event(configured, {**event, "captured_at": "2026-07-14T08:01:00Z"})
    assert ready_groups(configured) == []
    assert ready_groups(configured, force=True) == [[older, newer]]


@pytest.mark.parametrize("checkpoint_enabled", [True, False])
def test_only_events_covered_by_written_cursor_are_retired(
    settings, transcript_path, valid_curation, monkeypatch, checkpoint_enabled
):
    from obsidian_sidecar import worker

    configured = replace(settings, checkpoint_enabled=checkpoint_enabled)
    early_offset = transcript_path.stat().st_size
    with transcript_path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-14T08:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Later evidence remains queued.",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    event = {
        "session_id": "fixture-session-001",
        "transcript_path": str(transcript_path),
    }
    earlier = enqueue_event(
        configured, {**event, "captured_at": "2026-07-14T08:01:00Z"}
    )
    later = enqueue_event(configured, {**event, "captured_at": "2026-07-14T08:03:00Z"})
    build_packet = worker.build_curation_packet

    def partial_packet(event, **kwargs):
        packet = build_packet(event, **kwargs)
        packet["checkpoint"]["cursor"]["byte_offset"] = early_offset
        packet["checkpoint"]["has_more"] = True
        return packet

    monkeypatch.setattr(worker, "build_curation_packet", partial_packet)
    monkeypatch.setattr(worker, "reindex_basic_memory", lambda _: "ok")
    result = process_ready(
        configured, force=True, curator=StaticCurator(valid_curation)
    )
    assert result.failed == 0
    assert result.processed_events == 1
    assert not earlier.exists()
    assert later.exists()
    assert load_event(later)["attempts"] == 0


def test_capture_health_tolerates_a_concurrently_recovered_file(settings, monkeypatch):
    pending = settings.capture_pending_dir / "pending.json"
    save_event(pending, {"captured_at": "2026-07-14T08:01:00Z"})
    original_stat = Path.stat

    def disappeared(path, *args, **kwargs):
        if path == pending:
            pending.unlink()
            raise FileNotFoundError(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappeared)
    assert capture_health(settings) == {"pending": 0, "failed": 0, "stalled": 0}


def test_live_host_lock_outlasts_expired_lease_across_processes(settings):
    expired = datetime.now(UTC) - timedelta(hours=1)
    command = [
        sys.executable,
        "-c",
        """
import sys
from pathlib import Path
from obsidian_sidecar.coordination import LeaseBusy, LocalWriterLease
try:
    with LocalWriterLease(Path(sys.argv[1]), ttl_seconds=60):
        print('acquired')
except LeaseBusy as error:
    print(error.reason)
""",
        str(settings.vault_path),
    ]
    with LocalWriterLease(settings.vault_path, ttl_seconds=1, now=expired) as lease:
        before = lease.path.read_bytes()
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "local-writer-host-lock"
        assert lease.path.read_bytes() == before
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "acquired"


def test_manual_backup_blocks_worker_after_lease_expiry(
    settings, transcript_path, valid_curation, monkeypatch
):
    from obsidian_sidecar import worker

    configured = replace(settings, auto_git_backup=True)
    event = enqueue_event(
        configured,
        {
            "session_id": "fixture-session-001",
            "transcript_path": str(transcript_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )
    before = event.read_bytes()

    def long_backup(_settings):
        path = settings.vault_path / "_System/Coordination/local-writer.json"
        record = load_event(path)
        record["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        save_event(path, record)
        processing = process_ready(
            settings, force=True, curator=StaticCurator(valid_curation)
        )
        assert processing.deferred_reason == "local-writer-host-lock"
        assert _run_git_checkpoint(settings)["reason"] == "local-writer-host-lock"
        assert event.read_bytes() == before
        return "ok"

    monkeypatch.setattr(worker, "commit_git_backup", long_backup)
    monkeypatch.setattr(worker, "reindex_basic_memory", lambda _: "ok")
    assert run_maintenance(configured)["backup_result"] == "ok"
    assert (
        process_ready(
            settings, force=True, curator=StaticCurator(valid_curation)
        ).processed_events
        == 1
    )


@pytest.mark.parametrize("failure", ["error", "unavailable"])
def test_idle_tick_retries_failed_indexing_without_recuration(
    settings, transcript_path, valid_curation, monkeypatch, failure
):
    from obsidian_sidecar import maintenance, worker
    from obsidian_sidecar.checkpoints import load_checkpoint

    index_calls = []
    curations = []

    def index(_settings, *, full=False):
        index_calls.append(full)
        return failure if len(index_calls) == 1 else "ok"

    class RecordingCurator:
        def curate(self, packet):
            curations.append(packet)
            return valid_curation

    monkeypatch.setattr(maintenance, "_reindex_basic_memory", index)
    monkeypatch.setattr(maintenance, "basic_memory_status", lambda _: "ok")
    monkeypatch.setattr(maintenance, "_command_status", lambda *_: "ok")
    monkeypatch.setattr(worker, "CodexLunaCurator", lambda _: RecordingCurator())
    save_event(settings.state_dir / "health.json", {"score": 100})
    save_event(
        settings.state_dir / "maintenance-success.json",
        {"completed_at": datetime.now(UTC).isoformat()},
    )
    save_event(settings.state_dir / "git-checkpoint.json", {"status": "ok"})
    enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "transcript_path": str(transcript_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )
    first = daemon_once(settings)
    assert first["processing"]["processed_events"] == 1
    assert first["processing"]["reindex_result"] == failure
    assert load_event(settings.state_dir / "worker-status.json")["status"] == "error"
    health = load_event(settings.state_dir / "health.json")
    assert health["indexing_problem"] == "basic-memory-index-error"
    assert health["score"] < 80
    assert "basic-memory-index-error" in {
        item["code"] for item in alert_status(settings)["alerts"]
    }
    checkpoint = load_checkpoint(settings, "fixture-session-001")
    note_path = Path(first["processing"]["note_paths"][0])
    note_before = note_path.read_bytes()
    second = daemon_once(settings)
    assert second["processing"]["groups_seen"] == 0
    assert second["processing"]["reindex_result"] == "ok"
    assert len(curations) == 1
    assert len(index_calls) == 2
    assert load_checkpoint(settings, "fixture-session-001") == checkpoint
    assert note_path.read_bytes() == note_before
    assert load_event(settings.state_dir / "worker-status.json")["status"] == "ok"
    assert load_event(settings.state_dir / "health.json")["score"] >= 80
    assert alert_status(settings)["healthy"]


def test_full_index_retry_preserves_full_mode_and_private_failure_state(
    settings, monkeypatch
):
    from obsidian_sidecar import maintenance

    calls = []

    def fail(_settings, *, full=False):
        calls.append(full)
        raise RuntimeError("private index failure detail")

    monkeypatch.setattr(maintenance, "_reindex_basic_memory", fail)
    with pytest.raises(RuntimeError):
        maintenance.reindex_basic_memory(settings, full=True)
    state = settings.state_dir / "index-status.json"
    assert "private index failure detail" not in state.read_text()
    assert not alert_status(settings)["healthy"]

    def recover(_settings, *, full=False):
        calls.append(full)
        return "ok"

    monkeypatch.setattr(maintenance, "_reindex_basic_memory", recover)
    assert process_ready(settings).reindex_result == "ok"
    assert calls == [True, True]
    assert alert_status(settings)["healthy"]


def test_incomplete_tail_cannot_retire_capture(settings, transcript_path):
    from obsidian_sidecar import worker
    from obsidian_sidecar.checkpoints import checkpoint_path

    session = "fixture-session-001"
    content = transcript_path.read_bytes()
    tail = b'{"type":"event_msg","timestamp":"2026-07-14T08:02:00Z","payload":{}}\n'
    transcript_path.write_bytes(content + tail[:25])
    event = enqueue_event(
        settings,
        {
            "session_id": session,
            "transcript_path": str(transcript_path),
            "captured_at": "2026-07-14T08:03:00Z",
        },
    )
    checkpoint = {
        "version": 1,
        "session_id": session,
        "curation": {},
        "update_count": 1,
        "captured_at": "2026-07-14T08:03:00Z",
        "cursor": {
            "transcript_path": str(transcript_path),
            "byte_offset": len(content),
        },
    }
    save_event(checkpoint_path(settings, session), checkpoint)
    assert worker._retire_covered_events([event], settings, {}) == 0
    assert event.exists()
    transcript_path.write_bytes(content + tail)
    assert worker._retire_covered_events([event], settings, {}) == 0
    checkpoint["cursor"]["byte_offset"] = transcript_path.stat().st_size
    checkpoint["update_count"] = 2
    save_event(checkpoint_path(settings, session), checkpoint)
    assert worker._retire_covered_events([event], settings, {}) == 1
    assert not event.exists()


def test_transcript_only_capture_uses_canonical_checkpoint(
    settings, transcript_path, valid_curation, monkeypatch
):
    from obsidian_sidecar import worker
    from obsidian_sidecar.checkpoints import load_checkpoint
    from obsidian_sidecar.transcript import build_curation_packet

    monkeypatch.setattr(worker, "reindex_basic_memory", lambda _: "ok")
    event = {
        "transcript_path": str(transcript_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    enqueue_event(settings, event)
    first = process_ready(settings, force=True, curator=StaticCurator(valid_curation))
    assert first.processed_events == 1
    checkpoint = load_checkpoint(settings, "fixture-session-001")
    assert checkpoint is not None
    assert not list(settings.queue_dir.glob("*.json"))
    packet = build_curation_packet(event, checkpoint=checkpoint)
    assert packet["session_id"] == "fixture-session-001"
    assert packet["checkpoint"]["mode"] == "incremental"
    assert process_ready(settings).groups_seen == 0


def test_index_refresh_cannot_postpone_daily_maintenance(settings, monkeypatch):
    from obsidian_sidecar import worker

    calls = []
    stale = datetime.now(UTC) - timedelta(hours=25)
    save_event(
        settings.state_dir / "maintenance-success.json",
        {"completed_at": stale.isoformat()},
    )
    save_event(settings.state_dir / "health.json", {"score": 100})
    monkeypatch.setattr(
        worker, "process_ready", lambda _: worker.ProcessSummary(reindex_result="ok")
    )
    monkeypatch.setattr(
        worker,
        "inspect_vault",
        lambda *_a, **_k: VaultHealth(checked_at=datetime.now(UTC).isoformat()),
    )

    def maintain(*_a, **_k):
        calls.append(True)
        return {
            "critical_failures": 0,
            "reindex_result": "ok",
            "backup_result": "disabled",
        }

    monkeypatch.setattr(worker, "_run_maintenance", maintain)
    assert daemon_once(settings)["maintenance"] is not None
    # More successful capture/index observations refresh health, not the daily clock.
    clock = (settings.state_dir / "maintenance-success.json").read_bytes()
    assert daemon_once(settings)["maintenance"] is None
    assert len(calls) == 1
    assert (settings.state_dir / "maintenance-success.json").read_bytes() == clock


@pytest.mark.parametrize(
    "result",
    [
        {"deferred_reason": "local-writer-active", "backup_result": "deferred"},
        {
            "critical_failures": 0,
            "reindex_result": "ok",
            "backup_result": "unavailable",
        },
        {
            "critical_failures": 0,
            "reindex_result": "error",
            "backup_result": "disabled",
        },
    ],
)
def test_incomplete_maintenance_stays_due(settings, monkeypatch, result):
    from obsidian_sidecar import worker

    monkeypatch.setattr(worker, "_run_maintenance", lambda *_a, **_k: result)
    run_maintenance(settings)
    assert worker._maintenance_due(settings)
    assert not (settings.state_dir / "maintenance-success.json").exists()


def test_active_index_owner_does_not_raise_failure_alert(
    settings, transcript_path, monkeypatch
):
    from obsidian_sidecar import maintenance

    save_event(
        settings.state_dir / "maintenance-success.json",
        {"completed_at": datetime.now(UTC).isoformat()},
    )
    save_event(
        settings.state_dir / "index-status.json",
        {
            "status": "running",
            "checked_at": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
        },
    )
    enqueue_event(
        settings,
        {"session_id": "fixture-session-001", "transcript_path": str(transcript_path)},
    )
    with LocalWriterLease(settings.vault_path, ttl_seconds=600):
        assert maintenance.indexing_problem(settings) is None
        daemon_once(settings)
        assert (
            load_event(settings.state_dir / "worker-status.json")["status"]
            == "deferred"
        )
        assert alert_status(settings)["healthy"]

    def dead_owner(*_args):
        raise ProcessLookupError()

    monkeypatch.setattr(maintenance.os, "kill", dead_owner)
    assert maintenance.indexing_problem(settings) == "basic-memory-index-error"
    assert not alert_status(settings)["healthy"]


def test_abandoned_index_state_is_actionable(settings):
    from obsidian_sidecar.maintenance import indexing_problem

    save_event(
        settings.state_dir / "index-status.json",
        {
            "status": "running",
            "checked_at": (datetime.now(UTC) - timedelta(minutes=11)).isoformat(),
            "pid": os.getpid(),
        },
    )
    assert indexing_problem(settings) == "basic-memory-index-error"
