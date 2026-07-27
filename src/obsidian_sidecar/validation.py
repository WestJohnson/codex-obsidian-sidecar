from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .artifacts import local_markdown_links
from .security import contains_secret


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...]
    review_required: bool


POSITIVE_CUES = frozenset(
    {
        "complete",
        "completed",
        "done",
        "fixed",
        "implemented",
        "passed",
        "ready",
        "resolved",
        "successful",
        "verified",
        "working",
    }
)
NEGATIVE_CUES = frozenset(
    {
        "blocked",
        "broken",
        "cannot",
        "failed",
        "incomplete",
        "missing",
        "pending",
        "remaining",
        "remains",
        "unexamined",
        "unresolved",
    }
)
STOPWORDS = (
    frozenset(
        {
            "about",
            "after",
            "also",
            "been",
            "being",
            "from",
            "have",
            "into",
            "only",
            "should",
            "that",
            "their",
            "there",
            "these",
            "this",
            "through",
            "until",
            "using",
            "was",
            "were",
            "will",
            "with",
            "work",
        }
    )
    | POSITIVE_CUES
    | NEGATIVE_CUES
)


def _tokens(text: str) -> set[str]:
    return {
        value
        for value in re.findall(r"[a-z0-9]+", text.casefold())
        if len(value) >= 4 and value not in STOPWORDS
    }


def _has_positive_resolution(text: str) -> bool:
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    negative_phrases = ("not fixed", "not complete", "not completed", "not working")
    return bool(words & POSITIVE_CUES) and not any(
        phrase in text.casefold() for phrase in negative_phrases
    )


def _related(left: str, right: str) -> bool:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    minimum = min(len(left_tokens), len(right_tokens))
    required = 1 if minimum <= 2 else 2
    return len(shared) >= required and len(shared) / minimum >= 0.35


def _latest_order(item: dict[str, Any], order: dict[str, int]) -> int:
    return max(
        (order.get(value, -1) for value in item.get("evidence_ids", [])), default=-1
    )


def _chronology_errors(curation: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    evidence = [item for item in packet.get("evidence", []) if isinstance(item, dict)]
    order = {
        str(item.get("id")): index
        for index, item in enumerate(evidence)
        if isinstance(item.get("id"), str)
    }
    positive_items: list[tuple[str, int]] = []
    for field in ("changes", "verification"):
        for item in curation.get(field, []):
            if isinstance(item, dict) and _has_positive_resolution(
                str(item.get("text", ""))
            ):
                positive_items.append(
                    (str(item.get("text", "")), _latest_order(item, order))
                )

    errors: list[str] = []
    for index, item in enumerate(curation.get("unresolved", [])):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", ""))
        unresolved_order = _latest_order(item, order)
        for resolved_text, resolved_order in positive_items:
            if resolved_order > unresolved_order and _related(text, resolved_text):
                errors.append(
                    f"unresolved[{index}] contradicts newer resolved evidence"
                )
                break
    return errors


def _link_errors(curation: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cwd = Path(str(packet.get("cwd") or Path.home())).expanduser()
    for artifact_index, item in enumerate(packet.get("artifacts", [])):
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("path") or "")).expanduser()
        if not path.exists():
            errors.append(f"artifacts[{artifact_index}] references missing path {path}")
    strings: list[tuple[str, str]] = []
    for field in ("summary", "objective", "outcome"):
        strings.append((field, str(curation.get(field, ""))))
    for field in ("decisions", "changes", "verification", "unresolved", "next_actions"):
        for index, item in enumerate(curation.get(field, [])):
            if not isinstance(item, dict):
                continue
            strings.append((f"{field}[{index}]", str(item.get("text", ""))))
            if "rationale" in item:
                strings.append(
                    (f"{field}[{index}].rationale", str(item.get("rationale", "")))
                )
    for location, value in strings:
        for _, path in local_markdown_links(value, cwd):
            if not path.exists():
                errors.append(f"{location} references missing path {path}")
    return errors


def schema_path() -> str:
    return str(files("obsidian_sidecar.schemas").joinpath("curation.schema.json"))


def response_schema_path() -> str:
    return str(
        files("obsidian_sidecar.schemas").joinpath("curation.response.schema.json")
    )


def load_schema() -> dict[str, Any]:
    return json.loads(
        files("obsidian_sidecar.schemas").joinpath("curation.schema.json").read_text()
    )


def normalize_curation_metadata(curation: dict[str, Any]) -> dict[str, Any]:
    """Normalize bounded, non-semantic metadata without masking invalid output."""

    normalized = dict(curation)
    topics = curation.get("topics")
    if not isinstance(topics, list):
        return normalized

    topic_schema = load_schema()["properties"]["topics"]
    item_schema = topic_schema["items"]
    if not all(Draft202012Validator(item_schema).is_valid(topic) for topic in topics):
        return normalized

    maximum = int(topic_schema["maxItems"])
    seen: set[str] = set()
    deduplicated: list[str] = []
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        deduplicated.append(topic)
    normalized["topics"] = deduplicated[:maximum]
    return normalized


def validate_curation(
    curation: dict[str, Any],
    packet: dict[str, Any],
    *,
    minimum_confidence: float,
) -> ValidationResult:
    errors = sorted(
        error.message
        for error in Draft202012Validator(load_schema()).iter_errors(curation)
    )
    allowed_evidence = {
        item.get("id")
        for item in packet.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    implemented_evidence = {
        str(value)
        for source_field in ("changes", "verification")
        for source_item in curation.get(source_field, [])
        if isinstance(source_item, dict)
        for value in source_item.get("evidence_ids", [])
    }
    for field in ("decisions", "changes", "verification", "unresolved", "next_actions"):
        values = curation.get(field, [])
        if not isinstance(values, list):
            continue
        seen_text: set[str] = set()
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            normalized = re.sub(r"\s+", " ", text).casefold()
            if normalized in seen_text:
                errors.append(f"{field}[{index}] duplicates another item")
            seen_text.add(normalized)
            for evidence_id in item.get("evidence_ids", []):
                if evidence_id not in allowed_evidence:
                    errors.append(
                        f"{field}[{index}] cites unknown evidence id {evidence_id}"
                    )
            if field == "decisions":
                decision_type = str(item.get("decision_type") or "")
                evidence_ids = {
                    str(value) for value in item.get("evidence_ids", [])
                }
                if (
                    decision_type == "operator-decision"
                    and not any(value.startswith("u") for value in evidence_ids)
                    and "c1" not in evidence_ids
                ):
                    errors.append(
                        f"decisions[{index}] operator-decision lacks user or checkpoint evidence"
                    )
                if (
                    decision_type == "implemented-choice"
                    and not (evidence_ids & implemented_evidence)
                    and "c1" not in evidence_ids
                ):
                    errors.append(
                        f"decisions[{index}] implemented-choice lacks change, verification, or checkpoint evidence"
                    )
                if decision_type == "legacy-unclassified" and "c1" not in evidence_ids:
                    errors.append(
                        f"decisions[{index}] legacy-unclassified is only valid for checkpoint evidence"
                    )

    errors.extend(_chronology_errors(curation, packet))
    errors.extend(_link_errors(curation, packet))

    serialized = json.dumps(curation, ensure_ascii=False)
    if contains_secret(serialized):
        errors.append("curation output contains an apparent secret")

    if not curation.get("skip"):
        durable_fields = (
            str(curation.get("objective", "")).strip(),
            str(curation.get("outcome", "")).strip(),
            curation.get("decisions", []),
            curation.get("changes", []),
            curation.get("unresolved", []),
        )
        if not any(durable_fields):
            errors.append("non-skipped curation contains no durable information")

    confidence = curation.get("confidence", 0)
    review_required = (
        not isinstance(confidence, (int, float)) or confidence < minimum_confidence
    )
    return ValidationResult(not errors, tuple(sorted(set(errors))), review_required)
