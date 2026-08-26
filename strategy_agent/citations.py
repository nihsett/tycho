"""Deterministic claim-version citation markers for strategy briefs.

Tycho never cites a URL.  A brief cites ``(claim_id, version)``, which is the
only provenance that stays reproducible after the claim is later superseded.
An unknown or unpinned citation is removed and fails the run rather than
silently shipping a link to nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from schemas.strategy import CitationMarker

DEFAULT_DASHBOARD_BASE = "/claims"

_MARKER = re.compile(
    r"""<claim\s+id\s*=\s*"(?P<claim_id>clm_[0-7][0-9A-HJKMNP-TV-Z]{25})"\s+"""
    r"""version\s*=\s*"(?P<version>\d{1,6})"\s*/>""",
)
# Anything shaped like a citation but not exactly right, so a malformed or
# invented marker is caught instead of being left as literal text in the brief.
_LOOSE_MARKER = re.compile(r"<\s*claim\b[^>]*/?>", re.IGNORECASE)
_URL = re.compile(r"https?://", re.IGNORECASE)


class CitationError(ValueError):
    """A brief cited something that is not a pinned claim version."""


@dataclass(frozen=True)
class CitationResult:
    rendered: str
    citations: list[CitationMarker] = field(default_factory=list)


def find_citations(text: str) -> list[CitationMarker]:
    """Parse every well-formed citation marker, in order of appearance."""
    return [
        CitationMarker(claim_id=match["claim_id"], version=int(match["version"]))
        for match in _MARKER.finditer(text)
    ]


def dashboard_link(marker: CitationMarker, base: str = DEFAULT_DASHBOARD_BASE) -> str:
    return f"[{marker.claim_id}@v{marker.version}]({base}/{marker.claim_id}?version={marker.version})"


def replace_citations(
    text: str,
    pinned: set[tuple[str, int]],
    *,
    base: str = DEFAULT_DASHBOARD_BASE,
) -> CitationResult:
    """Validate every marker against the pinned versions, then link them.

    Raises when the writer cited a URL, emitted a malformed marker, or cited a
    claim version this brief did not pin.  There is no partial success: an
    unusable citation fails the run.
    """
    if _URL.search(text):
        raise CitationError("a Tycho brief cites claim versions, never URLs")

    well_formed = {match.group(0) for match in _MARKER.finditer(text)}
    for loose in _LOOSE_MARKER.finditer(text):
        if loose.group(0) not in well_formed:
            raise CitationError("malformed citation marker in brief text")

    citations = find_citations(text)
    unpinned = sorted(
        f"{marker.claim_id}@v{marker.version}"
        for marker in citations
        if (marker.claim_id, marker.version) not in pinned
    )
    if unpinned:
        raise CitationError(f"brief cites unpinned claim versions: {unpinned}")

    def _link(match: re.Match[str]) -> str:
        marker = CitationMarker(claim_id=match["claim_id"], version=int(match["version"]))
        return dashboard_link(marker, base)

    return CitationResult(rendered=_MARKER.sub(_link, text), citations=citations)
