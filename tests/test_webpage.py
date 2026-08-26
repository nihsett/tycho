import json
from datetime import UTC, datetime, timedelta

from adapters.webpage import WebpageFetch, extract_html, extract_markdown
from pipeline.acquire_webpage import acquire_website_changelog
from pipeline.differ import (
    MAX_TEXT_CHUNK_BYTES,
    MAX_TEXT_CHUNK_LINES,
    diff_webpage_sections,
)
from pipeline.triage import triage_webpage_changes
from pipeline.local_backend import LocalBackend, LocalSettings
from schemas.config import load_config
from tests.semantic_test_helpers import FakeSemanticDiffer


class StaticWebpageAdapter:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(self, url: str, extract_hint: str) -> WebpageFetch:
        document = json.loads(self.payload)
        return WebpageFetch(
            url=url,
            payload=self.payload,
            title=document["title"],
            section_count=len(document["sections"]),
        )


def markdown_payload(markdown: str) -> bytes:
    title, sections = extract_markdown(markdown)
    return json.dumps(
        {
            "schema": "webpage-sections@1",
            "url": "https://pi.dev/news/releases?page=1",
            "title": title,
            "extract_hint": "release entries",
            "sections": sections,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def test_markdown_extracts_stable_hierarchical_sections():
    title, sections = extract_markdown(
        """# Changelog
Intro text.
## v0.84.2
### Added
New provider support.
### Fixed
Terminal rendering.
"""
    )
    assert title == "Changelog"
    assert sections == [
        {"path": "Changelog", "content": "Intro text."},
        {"path": "Changelog > v0.84.2 > Added", "content": "New provider support."},
        {"path": "Changelog > v0.84.2 > Fixed", "content": "Terminal rendering."},
    ]


def test_html_uses_main_content_and_ignores_page_chrome():
    title, sections = extract_html(
        """
        <html><head><title>Fallback</title></head><body>
          <nav>Ignore navigation</nav>
          <main><h1>Release notes</h1><p>Current versions.</p>
            <h2>v2</h2><p>Added agent hooks.</p></main>
          <footer>Ignore copyright</footer>
        </body></html>
        """
    )
    assert title == "Release notes"
    assert sections == [
        {"path": "Release notes", "content": "Current versions."},
        {"path": "Release notes > v2", "content": "Added agent hooks."},
    ]


def test_webpage_diff_uses_stable_update_blocks():
    before = markdown_payload(
        """# Changelog
Intro.
<Update label="1.0" description="yesterday">
* Old feature
</Update>
"""
    )
    after = markdown_payload(
        """# Changelog
Intro.
<Update label="1.1" description="today">
* New feature
</Update>
<Update label="1.0" description="yesterday">
* Old feature
</Update>
"""
    )

    changes = diff_webpage_sections(before, after)

    assert len(changes) == 1
    assert changes[0].path == "sections.Changelog > Update 1.1"
    assert changes[0].before is None
    assert "New feature" in changes[0].after
    assert "Update 1.1" in triage_webpage_changes("Test", changes).summary


def test_webpage_diff_bounds_large_update_blocks_without_truncating():
    items = "\n".join(f"* item {index}" for index in range(250))
    before = markdown_payload("# Changelog\n")
    after = markdown_payload(
        f'# Changelog\n<Update label="1.0" description="today">\n{items}\n</Update>\n'
    )

    changes = diff_webpage_sections(before, after)

    assert len(changes) == 3
    assert all(
        len((change.after or "").splitlines(keepends=True)) <= MAX_TEXT_CHUNK_LINES
        for change in changes
    )
    assert all(
        len((change.after or "").encode("utf-8")) <= MAX_TEXT_CHUNK_BYTES
        for change in changes
    )
    assert "".join(change.after or "" for change in changes).count("item ") == 250


def test_webpage_change_flows_through_sqlite_to_claim(tmp_path):
    config = load_config("tycho.yaml")
    entity = config.entities["pi"]
    settings = LocalSettings(tmp_path / "data")
    first_at = datetime(2026, 8, 20, 4, tzinfo=UTC)
    differ = FakeSemanticDiffer()
    before = markdown_payload("# Pi releases\n## v0.84.1\nInitial release.\n")
    after = markdown_payload(
        "# Pi releases\n## v0.84.2\nAdded model support.\n## v0.84.1\nInitial release.\n"
    )

    with LocalBackend(config, settings) as backend:
        first = acquire_website_changelog(
            "pi", entity, backend, StaticWebpageAdapter(before), now=first_at, differ=differ
        )
        assert first.outcome == "bootstrapped"

    with LocalBackend(config, settings) as backend:
        unchanged = acquire_website_changelog(
            "pi",
            entity,
            backend,
            StaticWebpageAdapter(before),
            now=first_at + timedelta(hours=1),
            differ=differ,
        )
        assert unchanged.outcome == "unchanged"

        changed = acquire_website_changelog(
            "pi",
            entity,
            backend,
            StaticWebpageAdapter(after),
            now=first_at + timedelta(days=1),
            differ=differ,
        )
        assert changed.outcome == "meaningful"
        assert backend.deltas()[0].diff_kind.value == "semantic"
        assert backend.deltas()[0].source == "website_changelog"
        assert backend.claims()[0].evidence[0].source == "website_changelog"
        assert backend.claims()[0].evidence[0].note == (
            "Grounded semantic change in the supplied observation."
        )
        assert backend.pending_count() == 0
