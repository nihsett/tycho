"""Legacy deterministic normalization used only by tests and audit decoding.

Production startup rejects legacy/shadow acquisition modes, and this module
cannot write a canonical Delta@2 or act as an operational rollback path.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from html import unescape
import json
import re
from typing import Any

from schemas.delta import Change

LEGACY_DIFFER_VERSION = "legacy-python-differ@1"

_RELEASE_FIELDS = (
    "tag_name",
    "name",
    "body",
    "draft",
    "prerelease",
    "published_at",
    "target_commitish",
    "html_url",
)

MAX_TEXT_CHUNK_LINES = 100
MAX_TEXT_CHUNK_BYTES = 16_000
_HUNK_CONTEXT_LINES = 3
_UPDATE_BLOCK = re.compile(
    r"^[ \t]*<Update\b[^>]*>.*?</Update>[ \t]*",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_UPDATE_LABEL = re.compile(
    r"\blabel\s*=\s*([\"'])(.*?)\1",
    re.IGNORECASE | re.DOTALL,
)


def normalize_github_releases(payload: bytes) -> dict[str, dict[str, Any]]:
    releases = json.loads(payload)
    if not isinstance(releases, list):
        raise ValueError("GitHub release payload must be a list")
    normalized: dict[str, dict[str, Any]] = {}
    for release in releases:
        if not isinstance(release, dict) or not release.get("tag_name"):
            continue
        normalized[str(release["tag_name"])] = {
            field: release.get(field) for field in _RELEASE_FIELDS
        }
    return normalized


def structured_diff(before: Any, after: Any, path: str = "") -> list[Change]:
    """Normalize nested JSON changes to {path, before, after}."""
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[Change] = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append(Change(path=child_path, before=None, after=after[key]))
            elif key not in after:
                changes.append(Change(path=child_path, before=before[key], after=None))
            else:
                changes.extend(structured_diff(before[key], after[key], child_path))
        return changes
    return [Change(path=path or "$", before=before, after=after)]


def diff_github_releases(before_payload: bytes, after_payload: bytes) -> list[Change]:
    before = normalize_github_releases(before_payload)
    after = normalize_github_releases(after_payload)
    return structured_diff(before, after, "releases")


def normalize_webpage_sections(payload: bytes) -> dict[str, str]:
    document = json.loads(payload)
    if not isinstance(document, dict) or not isinstance(document.get("sections"), list):
        raise ValueError("webpage payload must contain sections")
    normalized: dict[str, str] = {}
    for section in document["sections"]:
        if not isinstance(section, dict) or not section.get("path"):
            raise ValueError("webpage section must contain a path")
        normalized[str(section["path"])] = str(section.get("content", ""))
    return normalized


def _text_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _split_long_line(line: str) -> list[str]:
    if _text_bytes(line) <= MAX_TEXT_CHUNK_BYTES:
        return [line]

    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in line:
        character_bytes = _text_bytes(character)
        if current and current_bytes + character_bytes > MAX_TEXT_CHUNK_BYTES:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        parts.append("".join(current))
    return parts


def _split_bounded_text(value: str) -> list[str]:
    if not value:
        return []

    parts: list[str] = []
    current: list[str] = []
    current_lines = 0
    current_bytes = 0

    def flush() -> None:
        nonlocal current, current_lines, current_bytes
        if current:
            parts.append("".join(current))
        current = []
        current_lines = 0
        current_bytes = 0

    for line in value.splitlines(keepends=True):
        for segment in _split_long_line(line):
            segment_bytes = _text_bytes(segment)
            if current and (
                current_lines >= MAX_TEXT_CHUNK_LINES
                or current_bytes + segment_bytes > MAX_TEXT_CHUNK_BYTES
            ):
                flush()
            current.append(segment)
            current_lines += 1
            current_bytes += segment_bytes
            if current_lines >= MAX_TEXT_CHUNK_LINES:
                flush()
    flush()
    return parts


def _fits_text_chunk(value: str) -> bool:
    return (
        len(value.splitlines(keepends=True)) <= MAX_TEXT_CHUNK_LINES
        and _text_bytes(value) <= MAX_TEXT_CHUNK_BYTES
    )


def _bounded_changes(
    path: str, before: str | None, after: str | None
) -> list[Change]:
    before_value = before or None
    after_value = after or None
    if before_value is None and after_value is None:
        return []
    if (before_value is None or _fits_text_chunk(before_value)) and (
        after_value is None or _fits_text_chunk(after_value)
    ):
        return [Change(path=path, before=before_value, after=after_value)]

    before_parts = _split_bounded_text(before_value or "")
    after_parts = _split_bounded_text(after_value or "")
    part_count = max(len(before_parts), len(after_parts))
    changes: list[Change] = []
    for index in range(part_count):
        changes.append(
            Change(
                path=f"{path} [part {index + 1}/{part_count}]",
                before=before_parts[index] if index < len(before_parts) else None,
                after=after_parts[index] if index < len(after_parts) else None,
            )
        )
    return changes


def _text_hunk_changes(path: str, before: str, after: str) -> list[Change]:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    changes: list[Change] = []
    for index, group in enumerate(
        matcher.get_grouped_opcodes(_HUNK_CONTEXT_LINES), start=1
    ):
        before_start, before_end = group[0][1], group[-1][2]
        after_start, after_end = group[0][3], group[-1][4]
        before_text = "".join(before_lines[before_start:before_end])
        after_text = "".join(after_lines[after_start:after_end])
        hunk_path = (
            f"{path} [hunk {index}; "
            f"lines {before_start + 1}-{before_end} -> {after_start + 1}-{after_end}]"
        )
        changes.extend(_bounded_changes(hunk_path, before_text, after_text))
    return changes


def _semantic_text_chunks(value: str) -> dict[str, str] | None:
    """Split common version-style update blocks without source-specific code."""
    matches = list(_UPDATE_BLOCK.finditer(value))
    if not matches:
        return None

    chunks: dict[str, str] = {}
    seen: dict[str, int] = {}
    cursor = 0
    context_count = 0
    for index, match in enumerate(matches, start=1):
        context = value[cursor:match.start()].strip()
        if context:
            context_count += 1
            key = "intro" if context_count == 1 and cursor == 0 else f"context {context_count}"
            chunks[key] = context

        block = match.group(0).strip()
        label_match = _UPDATE_LABEL.search(block)
        label = unescape(label_match.group(2)).strip() if label_match else str(index)
        base_key = f"Update {label}"
        seen[base_key] = seen.get(base_key, 0) + 1
        key = base_key if seen[base_key] == 1 else f"{base_key} #{seen[base_key]}"
        chunks[key] = block
        cursor = match.end()

    trailing = value[cursor:].strip()
    if trailing:
        chunks[f"context {context_count + 1}"] = trailing
    return chunks


def _semantic_chunk_changes(
    before: dict[str, str], after: dict[str, str], path: str
) -> list[Change]:
    changes: list[Change] = []
    for key in sorted(set(before) | set(after)):
        child_path = f"{path} > {key}"
        if key not in before:
            changes.extend(_bounded_changes(child_path, None, after[key]))
        elif key not in after:
            changes.extend(_bounded_changes(child_path, before[key], None))
        elif before[key] != after[key]:
            changes.extend(_text_hunk_changes(child_path, before[key], after[key]))
    return changes


def diff_webpage_sections(before_payload: bytes, after_payload: bytes) -> list[Change]:
    """Diff webpage text without copying unchanged historical page content."""
    before = normalize_webpage_sections(before_payload)
    after = normalize_webpage_sections(after_payload)
    changes: list[Change] = []

    for section in sorted(set(before) | set(after)):
        path = f"sections.{section}"
        if section not in before:
            chunks = _semantic_text_chunks(after[section])
            if chunks is None:
                changes.extend(_bounded_changes(path, None, after[section]))
            else:
                changes.extend(_semantic_chunk_changes({}, chunks, path))
            continue
        if section not in after:
            chunks = _semantic_text_chunks(before[section])
            if chunks is None:
                changes.extend(_bounded_changes(path, before[section], None))
            else:
                changes.extend(_semantic_chunk_changes(chunks, {}, path))
            continue
        if before[section] == after[section]:
            continue

        before_chunks = _semantic_text_chunks(before[section])
        after_chunks = _semantic_text_chunks(after[section])
        if before_chunks is not None and after_chunks is not None:
            changes.extend(_semantic_chunk_changes(before_chunks, after_chunks, path))
        else:
            changes.extend(_text_hunk_changes(path, before[section], after[section]))
    return changes
