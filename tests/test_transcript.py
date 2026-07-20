from pathlib import Path

from obsidian_sidecar.security import REDACTION
from obsidian_sidecar.transcript import build_curation_packet, extract_messages


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
