"""Legacy deterministic triage used only by tests and audit compatibility.

Canonical semantic generation performs extraction, materiality classification,
routing, and summary in one validated Gemini call; production never imports
this path for Delta creation.
"""

from __future__ import annotations

from dataclasses import dataclass

from schemas.delta import Change, Triage

_NOISY_SUFFIXES = (".body", ".html_url", ".target_commitish")


@dataclass(frozen=True)
class TriageResult:
    triage: Triage
    triage_by: str
    routed_to: list[str]
    summary: str


def triage_github_release_changes(entity_name: str, changes: list[Change]) -> TriageResult:
    added_tags = sorted(
        str(change.after["tag_name"])
        for change in changes
        if change.before is None
        and isinstance(change.after, dict)
        and change.after.get("tag_name")
    )
    removed_tags = sorted(
        str(change.before["tag_name"])
        for change in changes
        if change.after is None
        and isinstance(change.before, dict)
        and change.before.get("tag_name")
    )
    meaningful = [
        change for change in changes if not change.path.endswith(_NOISY_SUFFIXES)
    ]
    if not meaningful:
        return TriageResult(
            triage=Triage.NOISE,
            triage_by="rules@1",
            routed_to=["product/capabilities"],
            summary=f"{entity_name} release metadata changed without a product signal.",
        )

    parts: list[str] = []
    if added_tags:
        parts.append(f"published {', '.join(added_tags)}")
    if removed_tags:
        parts.append(f"removed {', '.join(removed_tags)} from the feed")
    if not parts:
        parts.append(f"changed {len(meaningful)} release field(s)")
    return TriageResult(
        triage=Triage.MEANINGFUL,
        triage_by="rules@1",
        routed_to=["product/capabilities", "product/roadmap"],
        summary=f"{entity_name} {' and '.join(parts)}.",
    )


def _display_webpage_change_path(path: str) -> str:
    value = path.removeprefix("sections.")
    return value.split(" [", 1)[0]


def triage_webpage_changes(entity_name: str, changes: list[Change]) -> TriageResult:
    meaningful = [change for change in changes if change.path != "$content_hash"]
    if not meaningful:
        return TriageResult(
            triage=Triage.NOISE,
            triage_by="rules@1",
            routed_to=["product/capabilities"],
            summary=f"{entity_name} webpage transport changed without a content signal.",
        )
    change_names = list(
        dict.fromkeys(_display_webpage_change_path(change.path) for change in meaningful)
    )
    suffix = "" if len(change_names) <= 3 else f" and {len(change_names) - 3} more"
    item_word = "item" if len(change_names) == 1 else "items"
    return TriageResult(
        triage=Triage.MEANINGFUL,
        triage_by="rules@1",
        routed_to=["product/capabilities", "product/roadmap"],
        summary=(
            f"{entity_name} changed {len(change_names)} official changelog "
            f"{item_word}: {', '.join(change_names[:3])}{suffix}."
        ),
    )
