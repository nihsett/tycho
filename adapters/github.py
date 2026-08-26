"""Deterministic GitHub Releases adapter using the official REST API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class GithubFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class GithubFetch:
    repository: str
    payload: bytes
    releases: list[dict[str, Any]]


class GithubAdapter(Protocol):
    def fetch_releases(self, repository: str) -> GithubFetch: ...


class GithubReleasesAdapter:
    adapter_version = "github@1"

    def __init__(self, token: str | None = None, per_page: int = 20) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.per_page = per_page

    def fetch_releases(self, repository: str) -> GithubFetch:
        quoted_repo = urllib.parse.quote(repository, safe="/")
        url = f"https://api.github.com/repos/{quoted_repo}/releases?per_page={self.per_page}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "tycho-intel/0.1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise GithubFetchError(f"GitHub fetch failed for {repository}: {exc}") from exc

        try:
            releases = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GithubFetchError(f"GitHub returned invalid JSON for {repository}") from exc
        if not isinstance(releases, list) or not all(isinstance(item, dict) for item in releases):
            raise GithubFetchError(f"GitHub returned an unexpected release payload for {repository}")

        # Canonical JSON makes hashing independent of transport whitespace while
        # preserving every field returned by the source.
        canonical = json.dumps(releases, sort_keys=True, separators=(",", ":")).encode()
        return GithubFetch(repository=repository, payload=canonical, releases=releases)
