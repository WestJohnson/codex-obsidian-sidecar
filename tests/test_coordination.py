from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from obsidian_sidecar.coordination import (
    CloudLease,
    LocalWriterLease,
    MachineProcessLock,
    cloud_lease_status,
    local_writer_status,
)


NOW = datetime(2026, 7, 14, 20, 0, tzinfo=UTC)


def test_cloud_lease_is_active_then_removed(tmp_path) -> None:
    with CloudLease(tmp_path, ttl_seconds=600, now=NOW):
        active, reason, value = cloud_lease_status(tmp_path, now=NOW)
        assert active is True
        assert reason == "active"
        assert value and value["owner"] == "obsidian-cloud-maintenance"
    assert cloud_lease_status(tmp_path, now=NOW)[0] is False


def test_active_cloud_lease_rejects_second_owner(tmp_path) -> None:
    with CloudLease(tmp_path, ttl_seconds=600, now=NOW):
        with pytest.raises(RuntimeError, match="lease is active"):
            with CloudLease(tmp_path, ttl_seconds=600, now=NOW):
                pass


def test_expired_lease_can_be_replaced(tmp_path) -> None:
    path = tmp_path / "_System/Coordination/cloud-maintenance.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}),
        encoding="utf-8",
    )
    assert cloud_lease_status(tmp_path, now=NOW)[:2] == (False, "expired")
    with CloudLease(tmp_path, ttl_seconds=60, now=NOW):
        assert cloud_lease_status(tmp_path, now=NOW)[0] is True


def test_malformed_lease_fails_closed(tmp_path) -> None:
    path = tmp_path / "_System/Coordination/cloud-maintenance.json"
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert cloud_lease_status(tmp_path, now=NOW)[:2] == (
        True,
        "malformed-fail-closed",
    )


def test_local_writer_uses_distinct_lease(tmp_path) -> None:
    with LocalWriterLease(tmp_path, ttl_seconds=600, now=NOW):
        assert local_writer_status(tmp_path, now=NOW)[0] is True
        assert cloud_lease_status(tmp_path, now=NOW)[0] is False
    assert local_writer_status(tmp_path, now=NOW)[0] is False


def test_simultaneous_same_host_lease_acquisition_has_one_winner(tmp_path) -> None:
    start = threading.Barrier(3)
    outcomes: list[str] = []

    def contend() -> None:
        start.wait()
        try:
            with CloudLease(tmp_path, ttl_seconds=600):
                outcomes.append("acquired")
                time.sleep(0.1)
        except RuntimeError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert outcomes.count("acquired") == 1
    assert outcomes.count("rejected") == 1


def test_machine_process_lock_rejects_overlapping_run(tmp_path) -> None:
    path = tmp_path / "cloud-maintenance.lock"
    with MachineProcessLock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with MachineProcessLock(path):
                pass
