from copy import deepcopy
from pathlib import Path

from obsidian_sidecar.validation import normalize_curation_metadata, validate_curation


def packet() -> dict:
    return {"evidence": [{"id": "u1"}, {"id": "a1"}]}


def test_valid_grounded_curation_passes(valid_curation: dict) -> None:
    result = validate_curation(valid_curation, packet(), minimum_confidence=0.65)
    assert result.valid
    assert not result.review_required


def test_operator_decision_requires_user_or_checkpoint_evidence(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["decisions"][0]["decision_type"] = "operator-decision"

    result = validate_curation(value, packet(), minimum_confidence=0.65)

    assert not result.valid
    assert any("operator-decision lacks" in error for error in result.errors)


def test_implemented_choice_requires_change_or_verification_evidence(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["changes"] = []
    value["verification"] = []

    result = validate_curation(value, packet(), minimum_confidence=0.65)

    assert not result.valid
    assert any("implemented-choice lacks" in error for error in result.errors)


def test_legacy_decision_requires_checkpoint_evidence(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["decisions"][0]["decision_type"] = "legacy-unclassified"

    result = validate_curation(value, packet(), minimum_confidence=0.65)

    assert not result.valid
    assert any("legacy-unclassified" in error for error in result.errors)


def test_topic_metadata_is_stably_deduplicated_and_capped(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["topics"] = [f"topic-{index}" for index in range(14)] + ["topic-3"]

    normalized = normalize_curation_metadata(value)

    assert normalized["topics"] == [f"topic-{index}" for index in range(12)]
    assert len(value["topics"]) == 15
    assert validate_curation(normalized, packet(), minimum_confidence=0.65).valid


def test_invalid_topic_metadata_is_not_hidden_by_normalization(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["topics"] = [f"topic-{index}" for index in range(12)] + ["Bad Topic"]

    normalized = normalize_curation_metadata(value)
    result = validate_curation(normalized, packet(), minimum_confidence=0.65)

    assert normalized["topics"] == value["topics"]
    assert not result.valid


def test_metadata_normalization_never_truncates_semantic_lists(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["changes"] = [
        {"text": f"Verified change {index}", "evidence_ids": ["a1"]}
        for index in range(31)
    ]

    normalized = normalize_curation_metadata(value)
    result = validate_curation(normalized, packet(), minimum_confidence=0.65)

    assert len(normalized["changes"]) == 31
    assert not result.valid


def test_unknown_evidence_fails(valid_curation: dict) -> None:
    value = deepcopy(valid_curation)
    value["changes"][0]["evidence_ids"] = ["a99"]
    result = validate_curation(value, packet(), minimum_confidence=0.65)
    assert not result.valid
    assert any("unknown evidence" in error for error in result.errors)


def test_low_confidence_requires_review(valid_curation: dict) -> None:
    value = deepcopy(valid_curation)
    value["confidence"] = 0.5
    result = validate_curation(value, packet(), minimum_confidence=0.65)
    assert result.valid
    assert result.review_required


def test_duplicate_claims_fail(valid_curation: dict) -> None:
    value = deepcopy(valid_curation)
    value["changes"].append(deepcopy(value["changes"][0]))
    result = validate_curation(value, packet(), minimum_confidence=0.65)
    assert not result.valid
    assert any("duplicates" in error for error in result.errors)


def test_newer_resolution_removes_stale_unresolved(valid_curation: dict) -> None:
    value = deepcopy(valid_curation)
    value["changes"] = [
        {
            "text": "Completed and verified the post-paywall application review.",
            "evidence_ids": ["a2"],
        }
    ]
    value["verification"] = []
    value["unresolved"] = [
        {
            "text": "The post-paywall application remains unexamined.",
            "disposition": "blocker",
            "evidence_ids": ["a1"],
        }
    ]
    evidence = {
        "cwd": "/tmp",
        "evidence": [
            {
                "id": "a1",
                "kind": "conversation",
                "role": "assistant",
                "text": "The post-paywall application has not been reviewed.",
            },
            {
                "id": "a2",
                "kind": "conversation",
                "role": "assistant",
                "text": "Completed and verified the post-paywall application review.",
            },
        ],
    }

    result = validate_curation(value, evidence, minimum_confidence=0.65)

    assert not result.valid
    assert any("unresolved[0]" in error for error in result.errors)


def test_same_evidence_can_support_success_and_a_scoped_caveat(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["changes"] = []
    value["verification"] = [
        {
            "text": "Verified interactive responses through Kimi K3.",
            "evidence_ids": ["a1"],
        }
    ]
    value["unresolved"] = [
        {
            "text": "The separate non-interactive print mode may return an empty final JSON result.",
            "disposition": "monitor",
            "evidence_ids": ["a1"],
        }
    ]
    evidence = {
        "cwd": "/tmp",
        "evidence": [
            {
                "id": "a1",
                "kind": "conversation",
                "role": "assistant",
                "text": (
                    "Interactive Kimi responses are verified. The non-interactive "
                    "print mode may still return an empty final JSON result."
                ),
            }
        ],
    }

    result = validate_curation(value, evidence, minimum_confidence=0.65)

    assert result.valid


def test_newer_success_sentence_does_not_erase_explicit_remaining_caveats(
    valid_curation: dict,
) -> None:
    value = deepcopy(valid_curation)
    value["unresolved"].extend(
        [
            {
                "text": "The revised waiver still needs legal review.",
                "disposition": "blocker",
                "evidence_ids": ["c1"],
            },
            {
                "text": "The exact overnight amount remains undecided.",
                "disposition": "blocker",
                "evidence_ids": ["c1"],
            },
        ]
    )
    evidence = {
        "cwd": "/tmp",
        "evidence": [
            {"id": "c1", "kind": "checkpoint", "text": "Prior durable state."},
            {
                "id": "a1",
                "kind": "conversation",
                "role": "assistant",
                "text": (
                    "The revised waiver is complete. Legal review remains required "
                    "and the exact overnight amount is still undecided."
                ),
            },
        ],
    }

    result = validate_curation(value, evidence, minimum_confidence=0.65)

    assert result.valid


def test_missing_packet_artifact_fails_validation(
    valid_curation: dict, tmp_path: Path
) -> None:
    value = deepcopy(valid_curation)
    evidence = packet()
    evidence["cwd"] = str(tmp_path)
    evidence["artifacts"] = [
        {
            "label": "Build report",
            "path": str(tmp_path / "missing-report.md"),
            "evidence_id": "a1",
        }
    ]

    result = validate_curation(value, evidence, minimum_confidence=0.65)

    assert not result.valid
    assert any("references missing path" in error for error in result.errors)


def test_existing_packet_artifact_passes_validation(
    valid_curation: dict, tmp_path: Path
) -> None:
    artifact = tmp_path / "build-report.md"
    artifact.write_text("# Build report\n", encoding="utf-8")
    evidence = packet()
    evidence["cwd"] = str(tmp_path)
    evidence["artifacts"] = [
        {"label": "Build report", "path": str(artifact), "evidence_id": "a1"}
    ]

    result = validate_curation(valid_curation, evidence, minimum_confidence=0.65)

    assert result.valid
