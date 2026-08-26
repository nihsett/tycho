"""Citation markers: parsing, pinning, link replacement, and failure modes."""

import pytest

from strategy_agent.citations import (
    CitationError,
    dashboard_link,
    find_citations,
    replace_citations,
)
from schemas.strategy import CitationMarker

CLAIM_A = "clm_01ARZ3NDEKTSV4RRFFQ69G5FAY"
CLAIM_B = "clm_01ARZ3NDEKTSV4RRFFQ69G5FB1"
PINNED = {(CLAIM_A, 1), (CLAIM_B, 3)}


def marker(claim_id: str, version: int) -> str:
    return f'<claim id="{claim_id}" version="{version}"/>'


def test_well_formed_markers_are_parsed_in_order():
    text = f"Alpha {marker(CLAIM_A, 1)} then beta {marker(CLAIM_B, 3)}."
    assert find_citations(text) == [
        CitationMarker(claim_id=CLAIM_A, version=1),
        CitationMarker(claim_id=CLAIM_B, version=3),
    ]


def test_pinned_markers_become_dashboard_links():
    text = f"Alpha {marker(CLAIM_A, 1)}."
    result = replace_citations(text, PINNED)
    assert "<claim id=" not in result.rendered
    assert f"/claims/{CLAIM_A}?version=1" in result.rendered
    assert result.citations == [CitationMarker(claim_id=CLAIM_A, version=1)]
    assert dashboard_link(CitationMarker(claim_id=CLAIM_A, version=1), "/x") == (
        f"[{CLAIM_A}@v1](/x/{CLAIM_A}?version=1)"
    )


def test_an_unpinned_claim_version_fails_the_run():
    with pytest.raises(CitationError, match="unpinned"):
        replace_citations(f"Alpha {marker(CLAIM_A, 2)}.", PINNED)
    with pytest.raises(CitationError, match="unpinned"):
        replace_citations(
            f"Alpha {marker('clm_01ARZ3NDEKTSV4RRFFQ69G5H99', 1)}.", PINNED
        )


@pytest.mark.parametrize(
    "text",
    [
        '<claim id="clm_bogus" version="1"/>',
        '<claim id="clm_01ARZ3NDEKTSV4RRFFQ69G5FAY">',
        '<claim version="1"/>',
        '<claim id="clm_01ARZ3NDEKTSV4RRFFQ69G5FAY" version="one"/>',
    ],
)
def test_a_malformed_marker_fails_the_run(text):
    with pytest.raises(CitationError, match="malformed"):
        replace_citations(text, PINNED)


def test_a_brief_may_not_cite_a_url():
    with pytest.raises(CitationError, match="never URLs"):
        replace_citations("See https://github.com/anthropics/claude-code.", PINNED)


def test_text_without_citations_passes_through_unchanged():
    text = "No defensible cross-entity pattern this period."
    result = replace_citations(text, set())
    assert result.rendered == text
    assert result.citations == []
