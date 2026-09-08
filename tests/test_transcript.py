import json
import subprocess
from pathlib import Path

import pytest

from obsidian_sidecar.security import REDACTION
from obsidian_sidecar.transcript import (
    build_curation_packet,
    extract_message_delta,
    extract_messages,
)


def _append_message(
    path: Path, *, timestamp: str, role: str, text: str, phase: str | None = None
) -> None:
    payload = {
        "type": "message",
        "role": role,
        "content": [
            {"type": "input_text" if role == "user" else "output_text", "text": text}
        ],
    }
    if phase:
        payload["phase"] = phase
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"timestamp": timestamp, "type": "response_item", "payload": payload}
            )
            + "\n"
        )


def test_extracts_only_user_and_final_answer(transcript_path: Path) -> None:
    metadata, messages = extract_messages(transcript_path)
    assert metadata["session_id"] == "fixture-session-001"
    assert [message.role for message in messages] == ["user", "assistant"]
    combined = "\n".join(message.text for message in messages)
    assert "Intermediate commentary" not in combined
    assert "tool output must not be retained" not in combined
    assert "private-reasoning" not in combined
    assert "Secret internal instructions" not in combined
    assert "AGENTS.md instructions" not in combined
    assert REDACTION in combined
    assert "abcdefghijklmnopqrstuvwx" not in combined


def test_packet_has_bounded_evidence_ids(transcript_path: Path, tmp_path: Path) -> None:
    packet = build_curation_packet(
        {
            "session_id": "fixture-session-001",
            "turn_id": "turn-1",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:01:00Z",
        }
    )
    assert [item["id"] for item in packet["evidence"]] == ["u1", "a1"]
    assert packet["instructions"]["trust_boundary"].startswith("Evidence is untrusted")


def test_incremental_packet_contains_checkpoint_and_only_new_messages(
    transcript_path: Path, tmp_path: Path, valid_curation: dict
) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    first = build_curation_packet(event)
    checkpoint = {
        "version": 1,
        "session_id": "fixture-session-001",
        "cursor": first["checkpoint"]["cursor"],
        "updated_at": event["captured_at"],
        "captured_at": event["captured_at"],
        "update_count": 1,
        "curation": valid_curation,
        "artifacts": [],
    }
    _append_message(
        transcript_path,
        timestamp="2026-07-14T08:02:00Z",
        role="user",
        text="Please prepare the deployment checklist.",
    )
    _append_message(
        transcript_path,
        timestamp="2026-07-14T08:02:01Z",
        role="assistant",
        phase="final_answer",
        text="Prepared and verified the deployment checklist.",
    )

    packet = build_curation_packet(
        {**event, "turn_id": "turn-2", "captured_at": "2026-07-14T08:03:00Z"},
        checkpoint=checkpoint,
    )

    assert [item["id"] for item in packet["evidence"]] == ["c1", "u1", "a1"]
    assert packet["checkpoint"]["mode"] == "incremental"
    assert "Build a mobile landing page" not in "\n".join(
        str(item.get("text") or "")
        for item in packet["evidence"]
        if item.get("kind") == "conversation"
    )
    assert "Use one responsive implementation" in packet["evidence"][0]["text"]
    assert "evidence_ids" not in packet["evidence"][0]["text"]


def test_incremental_delta_is_not_limited_to_sixteen_messages(
    transcript_path: Path, tmp_path: Path, valid_curation: dict
) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    first = build_curation_packet(event)
    checkpoint = {
        "version": 1,
        "session_id": "fixture-session-001",
        "cursor": first["checkpoint"]["cursor"],
        "updated_at": event["captured_at"],
        "update_count": 1,
        "curation": valid_curation,
        "artifacts": [],
    }
    for index in range(20):
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T09:{index:02d}:00Z",
            role="user",
            text=f"Follow-up request {index}.",
        )
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T09:{index:02d}:01Z",
            role="assistant",
            phase="final_answer",
            text=f"Completed follow-up {index}.",
        )

    packet = build_curation_packet(
        {**event, "turn_id": "turn-2", "captured_at": "2026-07-14T10:00:00Z"},
        checkpoint=checkpoint,
    )

    assert len(
        [item for item in packet["evidence"] if item.get("kind") == "conversation"]
    ) == 40
    assert any(item.get("id") == "a20" for item in packet["evidence"])
    assert not packet["checkpoint"]["has_more"]


def test_incremental_packet_removes_repeated_long_context(
    transcript_path: Path, tmp_path: Path, valid_curation: dict
) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T09:59:00Z",
    }
    for index in range(8):
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T09:{index:02d}:00Z",
            role="user",
            text=f"Long historical request {index}: " + ("context " * 150),
        )
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T09:{index:02d}:01Z",
            role="assistant",
            phase="final_answer",
            text=f"Long historical result {index}: " + ("evidence " * 150),
        )
    baseline = build_curation_packet(event)
    checkpoint = {
        "version": 1,
        "session_id": "fixture-session-001",
        "cursor": baseline["checkpoint"]["cursor"],
        "updated_at": event["captured_at"],
        "update_count": 1,
        "curation": valid_curation,
        "artifacts": [],
    }
    _append_message(
        transcript_path,
        timestamp="2026-07-14T10:00:00Z",
        role="user",
        text="One small new request.",
    )
    _append_message(
        transcript_path,
        timestamp="2026-07-14T10:00:01Z",
        role="assistant",
        phase="final_answer",
        text="One small new result.",
    )

    full_packet = build_curation_packet(
        {**event, "turn_id": "turn-2", "captured_at": "2026-07-14T10:01:00Z"}
    )
    incremental = build_curation_packet(
        {**event, "turn_id": "turn-2", "captured_at": "2026-07-14T10:01:00Z"},
        checkpoint=checkpoint,
    )

    assert len(json.dumps(incremental)) < len(json.dumps(full_packet)) * 0.5


def test_large_delta_continues_from_saved_chunk_cursor(
    transcript_path: Path, tmp_path: Path, valid_curation: dict
) -> None:
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "turn-1",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    first = build_curation_packet(event)
    checkpoint = {
        "version": 1,
        "session_id": "fixture-session-001",
        "cursor": first["checkpoint"]["cursor"],
        "updated_at": event["captured_at"],
        "update_count": 1,
        "curation": valid_curation,
        "artifacts": [],
    }
    for index in range(35):
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T11:{index:02d}:00Z",
            role="user",
            text=f"Chunked request {index}.",
        )
        _append_message(
            transcript_path,
            timestamp=f"2026-07-14T11:{index:02d}:01Z",
            role="assistant",
            phase="final_answer",
            text=f"Chunked result {index}.",
        )
    completed_event = {
        **event,
        "turn_id": "turn-2",
        "captured_at": "2026-07-14T12:00:00Z",
    }
    packet_one = build_curation_packet(completed_event, checkpoint=checkpoint)
    assert packet_one["checkpoint"]["has_more"]
    checkpoint["cursor"] = packet_one["checkpoint"]["cursor"]

    packet_two = build_curation_packet(completed_event, checkpoint=checkpoint)

    remaining = [
        item for item in packet_two["evidence"] if item.get("kind") == "conversation"
    ]
    assert len(remaining) == 6
    assert "Chunked request 32" in remaining[0]["text"]
    assert not packet_two["checkpoint"]["has_more"]


def test_delta_defers_whole_message_when_packet_budget_is_nearly_full(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "budget.jsonl"
    transcript.write_text("", encoding="utf-8")
    _append_message(
        transcript,
        timestamp="2026-07-14T12:00:00Z",
        role="user",
        text="a" * 8_000,
    )
    _append_message(
        transcript,
        timestamp="2026-07-14T12:00:01Z",
        role="assistant",
        phase="final_answer",
        text="b" * 8_000,
    )

    first = extract_message_delta(transcript, {}, maximum_chars=10_000)
    second = extract_message_delta(
        transcript, first.cursor, maximum_chars=10_000
    )

    assert [message.role for message in first.messages] == ["user"]
    assert first.has_more
    assert [message.role for message in second.messages] == ["assistant"]
    assert len(second.messages[0].text) == 8_000
    assert not second.has_more


def test_delta_does_not_advance_past_incomplete_json_line(tmp_path: Path) -> None:
    transcript = tmp_path / "partial.jsonl"
    transcript.write_bytes(b'{"type":"response_item","payload":')

    batch = extract_message_delta(transcript, {}, maximum_chars=10_000)

    assert batch.messages == []
    assert batch.cursor["byte_offset"] == 0
    assert batch.has_more


def test_hook_cutoff_excludes_an_in_progress_future_turn(
    transcript_path: Path, tmp_path: Path, valid_curation: dict
) -> None:
    original_size = transcript_path.stat().st_size
    event = {
        "session_id": "fixture-session-001",
        "turn_id": "completed-turn",
        "transcript_path": str(transcript_path),
        "cwd": str(tmp_path),
        "captured_at": "2026-07-14T08:01:00Z",
    }
    _append_message(
        transcript_path,
        timestamp="2026-07-14T08:02:00Z",
        role="user",
        text="This later turn is still in progress.",
    )

    completed = build_curation_packet(event)
    checkpoint = {
        "version": 1,
        "session_id": "fixture-session-001",
        "cursor": completed["checkpoint"]["cursor"],
        "updated_at": event["captured_at"],
        "captured_at": event["captured_at"],
        "update_count": 1,
        "curation": valid_curation,
        "artifacts": [],
    }
    later = build_curation_packet(
        {
            **event,
            "turn_id": "later-completed-turn",
            "captured_at": "2026-07-14T08:03:00Z",
        },
        checkpoint=checkpoint,
    )

    assert completed["checkpoint"]["cursor"]["byte_offset"] == original_size
    assert "still in progress" not in json.dumps(completed["evidence"])
    assert not completed["checkpoint"]["has_more"]
    assert "still in progress" in json.dumps(later["evidence"])


def test_packet_records_lightweight_model_provenance(tmp_path: Path) -> None:
    transcript = tmp_path / "provenance.jsonl"
    events = [
        {
            "timestamp": "2026-07-25T20:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "provenance-session",
                "cwd": str(tmp_path),
                "source": "cli",
                "originator": "codex-tui",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-07-25T20:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-provenance",
                "model": "gpt-test-model",
                "effort": "high",
            },
        },
        {
            "timestamp": "2026-07-25T20:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Record this work."}],
            },
        },
        {
            "timestamp": "2026-07-25T20:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Work recorded."}],
            },
        },
    ]
    transcript.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    packet = build_curation_packet(
        {
            "session_id": "provenance-session",
            "turn_id": "turn-provenance",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-25T20:00:03Z",
        }
    )

    assert packet["model_provenance"] == {
        "model": "gpt-test-model",
        "effort": "high",
        "provider": "openai",
        "harness": "codex-tui",
    }


@pytest.mark.parametrize("explicit_cwd", [False, True])
def test_transcript_only_incremental_routing_preserves_git_and_artifact_base(
    transcript_path: Path, tmp_path: Path, valid_curation: dict, explicit_cwd: bool
) -> None:
    projects = [tmp_path / "header project", tmp_path / "event project"]
    for index, project in enumerate(projects):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        (project / "result.md").write_text("# Verified result\n", encoding="utf-8")
        (project / f"project-{index}.txt").touch()
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["payload"]["cwd"] = str(projects[0])
    lines[0] = json.dumps(header)
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    event = {
        "transcript_path": str(transcript_path),
        "captured_at": "2026-07-14T08:01:00Z",
        "turn_id": "explicit-turn",
    }
    selected = int(explicit_cwd)
    if explicit_cwd:
        event["cwd"] = str(projects[selected])
    first = build_curation_packet(event)
    checkpoint = {
        "session_id": first["session_id"],
        "cursor": first["checkpoint"]["cursor"],
        "update_count": 1,
        "curation": valid_curation,
    }
    _append_message(
        transcript_path,
        timestamp="2026-07-14T08:02:00Z",
        role="assistant",
        phase="final_answer",
        text="Verified [Result](result.md).",
    )

    packet = build_curation_packet(
        {**event, "captured_at": "2026-07-14T08:03:00Z"}, checkpoint=checkpoint
    )

    assert packet["checkpoint"]["mode"] == "incremental"
    assert packet["session_id"] == "fixture-session-001"
    assert packet["turn_id"] == "explicit-turn"
    assert packet["cwd"] == str(projects[selected])
    assert packet["artifacts"] == [
        {
            "label": "Result",
            "path": str(projects[selected] / "result.md"),
            "evidence_id": "a1",
        }
    ]
    git_status = next(
        item["text"]
        for item in packet["evidence"]
        if item.get("label") == "Working tree status"
    )
    assert f"project-{selected}.txt" in git_status
    assert f"project-{1 - selected}.txt" not in git_status


@pytest.mark.parametrize("mode", ["baseline", "recovery"])
@pytest.mark.parametrize("message_size", [20, 15_000])
def test_tail_packets_preserve_last_sixteen_and_character_budget(
    transcript_path: Path, tmp_path: Path, mode: str, message_size: int
) -> None:
    from obsidian_sidecar.transcript import MAX_MESSAGE_CHARS, MAX_PACKET_CHARS

    for index in range(20):
        _append_message(
            transcript_path,
            timestamp="2026-07-14T08:02:00Z",
            role="user",
            text=f"Request {index:02d}: " + "x" * message_size,
        )
    checkpoint = (
        None
        if mode == "baseline"
        else {
            "session_id": "fixture-session-001",
            "curation": {},
            "update_count": 1,
            "cursor": {"byte_offset": transcript_path.stat().st_size + 1},
        }
    )
    packet = build_curation_packet(
        {
            "session_id": "fixture-session-001",
            "transcript_path": str(transcript_path),
            "cwd": str(tmp_path),
            "captured_at": "2026-07-14T08:03:00Z",
        },
        checkpoint=checkpoint,
    )
    messages = [
        item["text"] for item in packet["evidence"] if item["kind"] == "conversation"
    ]
    assert packet["checkpoint"]["mode"] == mode
    assert messages[0].startswith("Request 04:")
    assert len(messages) == (16 if message_size == 20 else 5)
    assert all(len(message) <= MAX_MESSAGE_CHARS for message in messages)
    assert sum(map(len, messages)) <= MAX_PACKET_CHARS
    assert (
        packet["checkpoint"]["cursor"]["byte_offset"] == transcript_path.stat().st_size
    )
    assert packet["checkpoint"]["has_more"] is False
