from __future__ import annotations

import json
import select
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import UTC, datetime
from threading import Event

import pytest

from obsidian_sidecar import maintenance, worker
from obsidian_sidecar.checkpoints import load_checkpoint
from obsidian_sidecar.queueing import (
    enqueue_event,
    load_event,
    processing_status,
    save_event,
)


@pytest.mark.parametrize("previous_result", ["ok", "error"])
def test_index_completion_cannot_clear_later_capture_after_worker_crash(
    settings, transcript_path, valid_curation, monkeypatch, previous_result
):
    state_path = settings.state_dir / "index-status.json"
    save_event(state_path, {"status": "error", "full": True})
    save_event(
        settings.state_dir / "maintenance-success.json",
        {"completed_at": datetime.now(UTC).isoformat()},
    )
    queued = enqueue_event(
        settings,
        {
            "session_id": "fixture-session-001",
            "cwd": str(transcript_path.parent),
            "transcript_path": str(transcript_path),
            "captured_at": "2026-07-14T08:01:00Z",
        },
    )

    class Failure:
        def curate(self, _packet):
            raise RuntimeError("fixture processing failure before recovery")

    assert worker.process_ready(settings, force=True, curator=Failure()).failed == 1
    assert processing_status(settings) == "error"
    save_event(state_path, {"status": "error", "full": True})
    completing = Event()
    release_completion = Event()
    curated = Event()
    packets = []
    index_calls = []
    persist = maintenance.save_event
    retire = worker._retire_covered_events

    def pause_old_completion(path, value):
        if (
            path == state_path
            and value["status"] == previous_result
            and not completing.is_set()
        ):
            completing.set()
            assert release_completion.wait(5)
        persist(path, value)

    def index(_settings, *, full=False):
        index_calls.append(full)
        return previous_result if len(index_calls) == 1 else "ok"

    class RecordingCurator:
        def curate(self, packet):
            packets.append(packet)
            curated.set()
            return valid_curation

    def crash_after_retirement(*args, **kwargs):
        assert retire(*args, **kwargs) == 1
        raise SystemExit("simulated capture process exit")

    monkeypatch.setattr(maintenance, "save_event", pause_old_completion)
    monkeypatch.setattr(maintenance, "_reindex_basic_memory", index)
    monkeypatch.setattr(maintenance, "basic_memory_status", lambda _: "ok")
    monkeypatch.setattr(maintenance, "_command_status", lambda *_: "ok")
    monkeypatch.setattr(worker, "CodexLunaCurator", lambda _: RecordingCurator())
    monkeypatch.setattr(worker, "_retire_covered_events", crash_after_retirement)

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_index = pool.submit(maintenance.reindex_basic_memory, settings)
        try:
            assert completing.wait(5)
            capture = pool.submit(worker.daemon_once, settings)
            assert curated.wait(5)
            with pytest.raises(FutureTimeout):
                capture.result(timeout=0.1)
        finally:
            release_completion.set()
        assert old_index.result(timeout=5) == previous_result
        with pytest.raises(SystemExit, match="simulated capture process exit"):
            capture.result(timeout=5)

    assert not queued.exists()
    assert len(list(settings.processed_dir.glob("*.json"))) == 1
    checkpoint = load_checkpoint(settings, "fixture-session-001")
    assert checkpoint["update_count"] == 1
    notes = {path: path.read_bytes() for path in settings.vault_path.rglob("*.md")}
    assert any(
        path.is_relative_to(settings.vault_path / "60 Sessions") for path in notes
    )
    pending = load_event(state_path)
    assert pending["status"] == "pending"
    assert pending["full"] is (previous_result == "error")

    recovered = worker.daemon_once(settings)
    assert recovered["processing"]["groups_seen"] == 0
    assert recovered["processing"]["reindex_result"] == "ok"
    assert recovered["maintenance"] is None
    assert len(packets) == 1
    assert index_calls == [True, previous_result == "error"]
    assert load_checkpoint(settings, "fixture-session-001") == checkpoint
    assert {path: path.read_bytes() for path in notes} == notes
    assert load_event(state_path)["status"] == "ok"
    assert processing_status(settings) == "ok"


def test_indexing_from_another_process_waits_for_pending_vault_write(settings):
    note = settings.vault_path / "record.md"
    note.write_text("before", encoding="utf-8")
    save_event(
        settings.state_dir / "index-status.json", {"status": "error", "full": True}
    )
    child = """
import json
import sys
from pathlib import Path
from obsidian_sidecar import maintenance
from obsidian_sidecar.config import Settings

settings = Settings(
    vault_path=Path(sys.argv[1]), state_dir=Path(sys.argv[2]), codex_bin=Path("/bin/false")
)
def index(settings, *, full=False):
    print(json.dumps({"content": (settings.vault_path / "record.md").read_text(), "full": full}), flush=True)
    return "ok"
maintenance._reindex_basic_memory = index
print("ready", flush=True)
print(maintenance.reindex_basic_memory(settings), flush=True)
"""
    process = None
    try:
        with maintenance.pending_index_write(settings):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    child,
                    str(settings.vault_path),
                    str(settings.state_dir),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert select.select([process.stdout], [], [], 5)[0]
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.1)
            assert (
                load_event(settings.state_dir / "index-status.json")["status"]
                == "pending"
            )
            note.write_text("after", encoding="utf-8")
        output, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
        lines = output.splitlines()
        assert json.loads(lines[0]) == {"content": "after", "full": True}
        assert lines[1] == "ok"
        assert load_event(settings.state_dir / "index-status.json")["status"] == "ok"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_pending_write_failure_releases_index_lock_and_keeps_full_intent(
    settings, monkeypatch
):
    state_path = settings.state_dir / "index-status.json"
    save_event(state_path, {"status": "error", "full": True})
    with pytest.raises(SystemExit):
        with maintenance.pending_index_write(settings):
            raise SystemExit("simulated interrupted vault write")
    assert load_event(state_path)["status"] == "pending"
    calls = []

    def index(_settings, *, full=False):
        calls.append(full)
        return "ok"

    monkeypatch.setattr(maintenance, "_reindex_basic_memory", index)
    assert maintenance.reindex_basic_memory(settings) == "ok"
    assert calls == [True]
    assert load_event(state_path)["status"] == "ok"
