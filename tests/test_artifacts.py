from pathlib import Path

from obsidian_sidecar.artifacts import extract_packet_artifacts, resolve_local_link


def test_extracts_existing_local_markdown_artifact_from_final_answer(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "reports" / "launch report.md"
    artifact.parent.mkdir()
    artifact.write_text("# Launch report\n", encoding="utf-8")
    evidence = [
        {
            "id": "a1",
            "kind": "conversation",
            "role": "assistant",
            "text": f"Reviewed [Launch report](<{artifact}>).",
        }
    ]

    result = extract_packet_artifacts(evidence, tmp_path)

    assert result == [
        {"label": "Launch report", "path": str(artifact), "evidence_id": "a1"}
    ]


def test_resolves_line_suffix_and_ignores_remote_links(tmp_path: Path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('ok')\n", encoding="utf-8")

    assert resolve_local_link(f"{source}:12", tmp_path) == source
    assert resolve_local_link("https://example.com/report", tmp_path) is None


def test_ignores_local_artifacts_that_no_longer_exist(tmp_path: Path) -> None:
    missing = tmp_path / "archived-report.md"
    evidence = [
        {
            "id": "a1",
            "kind": "conversation",
            "role": "assistant",
            "text": f"Reviewed [Archived report]({missing}).",
        }
    ]

    assert extract_packet_artifacts(evidence, tmp_path) == []
