from __future__ import annotations

from obsidian_sidecar.benchmark import FIXTURES, _fixture_json


def test_runtime_benchmark_fixtures_are_package_resources() -> None:
    assert "fixture-session-001" in FIXTURES.joinpath("transcript.jsonl").read_text(
        encoding="utf-8"
    )
    assert _fixture_json("valid-curation.json")["project_slug"] == "rainbow-joes"
