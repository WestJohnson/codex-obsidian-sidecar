from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable


MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\((<[^>]+>|[^)]+)\)")
REMOTE_SCHEMES = {"http", "https", "mailto", "obsidian", "app", "data"}


def resolve_local_link(target: str, cwd: Path) -> Path | None:
    value = target.strip()
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.casefold() in REMOTE_SCHEMES:
        return None
    if parsed.scheme and parsed.scheme.casefold() != "file":
        return None
    raw_path = urllib.parse.unquote(parsed.path if parsed.scheme == "file" else value)
    raw_path = raw_path.split("#", 1)[0].strip()
    if not raw_path:
        return None
    line_suffix = re.match(r"^(.*):(\d+)(?::\d+)?$", raw_path)
    if line_suffix:
        raw_path = line_suffix.group(1)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def local_markdown_links(text: str, cwd: Path) -> Iterable[tuple[str, Path]]:
    for match in MARKDOWN_LINK.finditer(text):
        path = resolve_local_link(match.group(2), cwd)
        if path is not None:
            yield match.group(1).strip() or path.name, path


def extract_packet_artifacts(
    evidence: list[dict[str, Any]], cwd: Path
) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in evidence:
        if item.get("kind") != "conversation" or item.get("role") != "assistant":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        for label, path in local_markdown_links(text, cwd):
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            if not path.exists():
                continue
            artifacts.append(
                {
                    "label": label[:200],
                    "path": normalized,
                    "evidence_id": str(item.get("id") or ""),
                }
            )
    return artifacts


def render_artifact_bullets(packet: dict[str, Any]) -> str:
    artifacts = packet.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        return "- None recorded."
    lines: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "Artifact").strip()
        path = str(item.get("path") or "").strip()
        evidence_id = str(item.get("evidence_id") or "").strip()
        target = (
            f"<{path}>" if any(value in path for value in (" ", "(", ")")) else path
        )
        lines.append(f"- [{label}]({target}) _(evidence: `{evidence_id}`)_")
    return "\n".join(lines) or "- None recorded."
