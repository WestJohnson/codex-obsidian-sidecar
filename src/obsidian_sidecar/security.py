from __future__ import annotations

import re
from dataclasses import dataclass


REDACTION = "[REDACTED_SECRET]"


@dataclass(frozen=True)
class SecretMatch:
    kind: str
    start: int
    end: int


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("openai-key", re.compile(r"\bsk-(?:api-|or-v1-)?[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\s*[:=]\s*[\"']?([^\s\"']{12,})"
        ),
    ),
)


def find_secrets(text: str) -> list[SecretMatch]:
    matches: list[SecretMatch] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            matches.append(SecretMatch(kind=kind, start=match.start(), end=match.end()))
    matches.sort(key=lambda item: (item.start, -(item.end - item.start)))
    collapsed: list[SecretMatch] = []
    for match in matches:
        if collapsed and match.start < collapsed[-1].end:
            if match.end > collapsed[-1].end:
                previous = collapsed[-1]
                collapsed[-1] = SecretMatch(previous.kind, previous.start, match.end)
            continue
        collapsed.append(match)
    return collapsed


def redact_text(text: str) -> tuple[str, list[str]]:
    matches = find_secrets(text)
    if not matches:
        return text, []
    output: list[str] = []
    cursor = 0
    kinds: list[str] = []
    for match in matches:
        output.append(text[cursor : match.start])
        output.append(REDACTION)
        cursor = match.end
        kinds.append(match.kind)
    output.append(text[cursor:])
    return "".join(output), sorted(set(kinds))


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))
