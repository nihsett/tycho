"""Generic deterministic webpage fetch and semantic section extraction."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup


class WebpageFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebpageFetch:
    url: str
    payload: bytes
    title: str
    section_count: int


class WebpageFetcher(Protocol):
    def fetch(self, url: str, extract_hint: str) -> WebpageFetch: ...


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _sectionize(lines: list[str]) -> list[dict[str, str]]:
    stack: list[str] = []
    content: list[str] = []
    sections: list[dict[str, str]] = []
    duplicate_count: dict[str, int] = {}

    def flush() -> None:
        nonlocal content
        body = "\n".join(line for line in content if line).strip()
        if not body:
            content = []
            return
        base_path = " > ".join(stack) if stack else "$document"
        duplicate_count[base_path] = duplicate_count.get(base_path, 0) + 1
        occurrence = duplicate_count[base_path]
        path = base_path if occurrence == 1 else f"{base_path} #{occurrence}"
        sections.append({"path": path, "content": body})
        content = []

    for raw_line in lines:
        line = _clean_line(raw_line)
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            heading = _clean_line(match.group(2))
            stack[level - 1 :] = [heading]
        elif line:
            content.append(line)
    flush()
    return sections or [{"path": "$document", "content": ""}]


def extract_markdown(text: str) -> tuple[str, list[dict[str, str]]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    title = next(
        (
            _clean_line(match.group(2))
            for line in lines
            if (match := _HEADING.match(_clean_line(line)))
        ),
        "Untitled webpage",
    )
    return title, _sectionize(lines)


def extract_html(text: str) -> tuple[str, list[dict[str, str]]]:
    soup = BeautifulSoup(text, "html.parser")
    for unwanted in soup.select("script, style, noscript, svg, nav, footer, header"):
        unwanted.decompose()
    root = soup.select_one("main") or soup.select_one("article") or soup.body or soup
    title_node = root.find("h1") or soup.title
    title = _clean_line(title_node.get_text(" ", strip=True)) if title_node else "Untitled webpage"

    markdown_lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre"]):
        if node.name in {"p", "li"} and node.find_parent(["li", "pre"]):
            continue
        value = _clean_line(node.get_text(" ", strip=True))
        if not value:
            continue
        if node.name.startswith("h"):
            markdown_lines.append(f"{'#' * int(node.name[1])} {value}")
        elif node.name == "li":
            markdown_lines.append(f"- {value}")
        else:
            markdown_lines.append(value)
    return title, _sectionize(markdown_lines)


class WebpageAdapter:
    adapter_version = "webpage@1"

    def __init__(self, max_bytes: int = 5_000_000) -> None:
        self.max_bytes = max_bytes

    def fetch(self, url: str, extract_hint: str) -> WebpageFetch:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/markdown,text/html;q=0.9,*/*;q=0.1",
                "User-Agent": "tycho-intel/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(self.max_bytes + 1)
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                final_url = response.url
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise WebpageFetchError(f"webpage fetch failed for {url}: {exc}") from exc
        if len(raw) > self.max_bytes:
            raise WebpageFetchError(f"webpage exceeds {self.max_bytes} bytes: {url}")

        text = raw.decode(charset, errors="replace")
        markdown = content_type in {"text/markdown", "text/plain"} or url.endswith(".md")
        title, sections = extract_markdown(text) if markdown else extract_html(text)
        normalized = {
            "schema": "webpage-sections@1",
            "url": final_url,
            "title": title,
            "extract_hint": extract_hint,
            "sections": sections,
        }
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        return WebpageFetch(
            url=final_url,
            payload=payload,
            title=title,
            section_count=len(sections),
        )
