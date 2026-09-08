from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from obsidian_sidecar import cli, maintenance, queueing
from obsidian_sidecar.coordination import LocalWriterLease


def test_cli_capture_recovery_retry_and_policy(
    settings, transcript_path, valid_curation, tmp_path, monkeypatch, capsys
):
    """Exercise CLI output and persisted contracts with synthetic curation/indexing."""
    session = "11111111-2222-4333-8444-555555555555"
    root = tmp_path / "codex"
    config = tmp_path / "config.json"
    fixture = tmp_path / "curation.json"
    config.write_text(json.dumps(settings.public_dict()))
    fixture.write_text(json.dumps(valid_curation))
    monkeypatch.setattr(queueing, "_codex_home", lambda: root)
    index_results = iter(["error", "ok"])
    index_calls = []

    def index(_settings, *, full=False):
        index_calls.append(full)
        return next(index_results)

    monkeypatch.setattr(maintenance, "_reindex_basic_memory", index)
    transcript = []

    def command(*args, stdin=None, expected=0):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = cli.main(["--config", str(config), *args])
        captured = capsys.readouterr()
        transcript.append(
            {
                "command": ["obsidian-sidecar", "--config", str(config), *args],
                "exit_code": code,
                "stdout": captured.out,
                "stderr": captured.err,
            }
        )
        assert code == expected, captured
        return json.loads(captured.out) if captured.out else None

    private_hook = "synthetic-private-hook-body"
    command(
        "capture-hook",
        stdin=json.dumps(
            {"session_id": session, "cwd": "/tmp/project", "content": private_hook}
        ),
    )
    pending = next(settings.capture_pending_dir.glob("*.json"))
    pending_record = json.loads(pending.read_text())
    assert private_hook not in pending.read_text()
    assert pending.stat().st_mode & 0o777 == 0o600
    assert pending_record["session_id"] == session
    assert pending_record["codex_home"] == str(root)
    assert command("process", "--force", "--curation-json", str(fixture))[
        "processed_events"
    ] == 0
    assert pending.exists()

    exact = root / "sessions" / f"rollout-test-{session}.jsonl"
    exact.parent.mkdir(parents=True)
    exact.write_text(
        transcript_path.read_text()
        .replace("fixture-session-001", session)
        .replace("/tmp/rainbow-joes", "/tmp/project")
    )
    with LocalWriterLease(settings.vault_path, ttl_seconds=600):
        deferred = command("process", "--force", "--curation-json", str(fixture))
        assert deferred["deferred_reason"] == "local-writer-active"
        assert deferred["failed"] == 0
        event = next(settings.queue_dir.glob("*.json"))
        deferred_record = json.loads(event.read_text())
        assert deferred_record["attempts"] == 0
        assert deferred_record["transcript_path"] == str(exact)
        assert not pending.exists()

    result = command("process", "--force", "--curation-json", str(fixture), expected=1)
    assert result["notes_written"] == result["processed_events"] == 1
    assert result["checkpoint_updates"] == 1
    assert result["reindex_result"] == "error"
    assert not list(settings.queue_dir.glob("*.json"))
    failed_index = command("alert-status")
    assert not failed_index["healthy"]
    assert "basic-memory-index-error" in {a["code"] for a in failed_index["alerts"]}
    note = Path(result["note_paths"][0])
    note_before = note.read_bytes()
    checkpoint = next(settings.checkpoint_dir.glob("*.json"))
    checkpoint_before = checkpoint.read_bytes()
    queueing.save_event(settings.state_dir / "health.json", {"score": 100})
    retried = command("daemon-once")
    assert retried["processing"]["groups_seen"] == 0
    assert retried["processing"]["reindex_result"] == "ok"
    assert index_calls == [False, False]
    assert note.read_bytes() == note_before
    assert checkpoint.read_bytes() == checkpoint_before
    assert command("alert-status")["healthy"]

    command("capture-hook", stdin="invalid synthetic-private-hook-body")
    failed_capture = next(settings.capture_failed_dir.glob("*.json"))
    assert private_hook not in failed_capture.read_text()
    assert "capture-incomplete" in {
        a["code"] for a in command("alert-status")["alerts"]
    }

    # Package-owned setup must leave captures, queues, and note freshness intact.
    retained = queueing.enqueue_event(
        settings,
        {
            "session_id": "retained-session",
            "transcript_path": str(exact),
            "turn_id": "retained-turn",
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )
    retained_before = retained.read_bytes()
    existing = json.loads(config.read_text())
    existing.update(
        freshness_project_days=60,
        freshness_decision_days=120,
        freshness_runbook_days=21,
    )
    config.write_text(json.dumps(existing))
    setup_args = (
        "setup",
        "--vault", str(settings.vault_path),
        "--state-dir", str(settings.state_dir),
        "--codex-bin", sys.executable,
        "--executable", sys.executable,
        "--no-service", "--no-codex-hook", "--no-basic-memory",
        "--disable-update-checks", "--apply",
    )
    command(*setup_args)
    preserved = json.loads(config.read_text())
    policy_keys = (
        "freshness_project_days", "freshness_decision_days", "freshness_runbook_days"
    )
    assert [preserved[k] for k in policy_keys] == [60, 120, 21]
    command(*setup_args, "--freshness-project-days", "90")
    overridden = json.loads(config.read_text())
    assert [overridden[k] for k in policy_keys] == [90, 120, 21]
    assert retained.read_bytes() == retained_before
    assert note.read_bytes() == note_before
    assert checkpoint.read_bytes() == checkpoint_before
    assert failed_capture.exists()
    assert config.stat().st_mode & 0o777 == 0o600

    evidence_dir = os.environ.get("SIDECAR_TEST_EVIDENCE_DIR")
    if evidence_dir:
        output = Path(evidence_dir)
        output.mkdir(parents=True, exist_ok=True)
        evidence = {
            "scope": (
                "Synthetic transcript and deterministic curation; "
                "indexing failure/retry simulated."
            ),
            "cli": transcript,
            "pending_capture": pending_record,
            "deferred_queue_event": deferred_record,
            "recovered_audit": queueing.load_event(
                next((settings.state_dir / "capture-recovered").glob("*.json"))
            ),
            "index_status": queueing.load_event(settings.state_dir / "index-status.json"),
            "worker_status": queueing.load_event(settings.state_dir / "worker-status.json"),
            "invalid_capture": json.loads(failed_capture.read_text()),
            "preserved_policy_days": [60, 120, 21],
            "explicit_policy_days": [90, 120, 21],
            "note_checkpoint_and_queue_preserved": True,
        }
        (output / "runtime-cli-evidence.json").write_text(
            json.dumps(evidence, indent=2).replace(str(tmp_path), "<TEST_ROOT>") + "\n"
        )
        (output / "recovered-session-note.md").write_text(note_before.decode())


def test_daemon_backup_retry_is_immediate_and_restorable(
    settings, tmp_path, monkeypatch
):
    from obsidian_sidecar import worker
    from obsidian_sidecar.alerts import alert_status

    configured = replace(settings, auto_git_backup=True)
    note = configured.vault_path / "manual-note.md"
    note.write_text("# Backup recovery probe\n\nPreserve this synthetic operator note.\n")
    queueing.save_event(configured.state_dir / "health.json", {"score": 100})
    real_backup = worker.commit_git_backup
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(True)
        return "unavailable" if len(calls) == 1 else real_backup(*args, **kwargs)

    monkeypatch.setattr(worker, "commit_git_backup", fail_once)
    first = worker.daemon_once(configured)
    assert first["checkpoint"]["status"] == "unavailable"
    assert not (configured.state_dir / "git-checkpoint.json").exists()
    failed_status = queueing.load_event(configured.state_dir / "worker-status.json")
    assert failed_status["status"] == "error"
    assert not alert_status(configured)["healthy"]

    second = worker.daemon_once(configured)
    assert second["checkpoint"]["status"] == "ok"
    assert len(calls) == 2
    assert alert_status(configured)["healthy"]
    restored = subprocess.run(
        ["git", "-C", str(configured.vault_path), "show", "HEAD:manual-note.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert restored == note.read_text()
    checkpoint = queueing.load_event(configured.state_dir / "git-checkpoint.json")
    assert checkpoint["result"] == "ok"
    if evidence_dir := os.environ.get("SIDECAR_TEST_EVIDENCE_DIR"):
        output = Path(evidence_dir)
        output.mkdir(parents=True, exist_ok=True)
        evidence = {
            "scope": (
                "First backup failure injected; immediate next tick "
                "writes a real Git commit."
            ),
            "first_tick": first,
            "failed_worker_status": failed_status,
            "next_tick": second,
            "successful_backup_checkpoint": checkpoint,
            "git_show_HEAD_manual_note": restored,
        }
        (output / "backup-retry-evidence.json").write_text(
            json.dumps(evidence, indent=2).replace(str(tmp_path), "<TEST_ROOT>") + "\n"
        )
