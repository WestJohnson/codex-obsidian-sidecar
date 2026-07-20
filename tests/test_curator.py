from __future__ import annotations

import json
from pathlib import Path

from obsidian_sidecar.curator import CodexLunaCurator, _safe_error_detail
from obsidian_sidecar.validation import response_schema_path


def test_curator_places_global_approval_flag_before_exec(settings) -> None:
    command = CodexLunaCurator(settings).command(Path("/tmp/curation.json"))

    assert command[1:4] == ["--ask-for-approval", "never", "exec"]
    assert "--skip-git-repo-check" in command
    assert command.index("--ask-for-approval") < command.index("exec")


def test_response_schema_uses_only_model_supported_constraints() -> None:
    schema = json.loads(Path(response_schema_path()).read_text(encoding="utf-8"))
    unsupported = {
        "format",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "uniqueItems",
    }

    def walk(value) -> None:
        if isinstance(value, dict):
            assert not (unsupported & value.keys())
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value.get("required", [])) == set(
                    value.get("properties", {})
                )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


def test_curator_failure_detail_does_not_echo_input_packet() -> None:
    stderr = """user\nprivate packet api_key=abcdefghijklmnopqrstuvwx\nERROR: {
      "type": "error",
      "error": {
        "type": "invalid_request_error",
        "code": "invalid_json_schema",
        "message": "Unsupported schema keyword"
      },
      "status": 400
    }"""

    detail = _safe_error_detail("", stderr)

    assert "private packet" not in detail
    assert "abcdefghijklmnopqrstuvwx" not in detail
    assert "invalid_json_schema" in detail
