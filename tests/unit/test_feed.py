"""`docs/feed.xml` is the website's release feed, generated from CHANGELOG.md and committed.

GitHub Pages runs only allow-listed plugins, so nothing builds the feed at deploy time; it is written
by `scripts/build_feed.py`, the same arrangement as `docs/llms-full.txt`. A committed derivative with no
drift check goes stale the first release after someone forgets the script, and it goes stale silently:
feed readers keep showing the old newest release as if nothing had shipped since.
"""

from __future__ import annotations

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FEED = REPO / "docs" / "feed.xml"
CHANGELOG = REPO / "CHANGELOG.md"
ATOM = "{http://www.w3.org/2005/Atom}"


def _load_builder():
    """Import the build script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("build_feed", REPO / "scripts" / "build_feed.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def entries() -> list[ET.Element]:
    return ET.fromstring(FEED.read_text(encoding="utf-8")).findall(f"{ATOM}entry")


def _top_released_version() -> str:
    """Read independently of the builder: the first dated `## [x.y.z] - YYYY-MM-DD` heading."""
    match = re.search(r"^## \[([^\]]+)\] - \d{4}-\d{2}-\d{2}", CHANGELOG.read_text(encoding="utf-8"), re.M)
    assert match, "CHANGELOG.md has no released version heading"
    return match.group(1)


def test_committed_feed_matches_a_fresh_build() -> None:
    built = _load_builder().build()
    assert FEED.read_text(encoding="utf-8") == built, "docs/feed.xml is stale. Run: python scripts/build_feed.py"


def test_feed_is_atom_with_at_most_twenty_entries(entries: list[ET.Element]) -> None:
    root = ET.fromstring(FEED.read_text(encoding="utf-8"))
    assert root.tag == f"{ATOM}feed"
    assert root.find(f"{ATOM}updated") is not None
    assert 1 <= len(entries) <= 20


def test_newest_entry_is_the_top_released_changelog_version(entries: list[ET.Element]) -> None:
    version = _top_released_version()
    newest = entries[0]
    assert newest.findtext(f"{ATOM}title") == f"Shortlist {version}"
    link = newest.find(f"{ATOM}link").get("href")
    assert link == f"https://github.com/stevezau/shortlist/releases/tag/v{version}"


def test_unreleased_changes_never_reach_the_feed(entries: list[ET.Element]) -> None:
    titles = [entry.findtext(f"{ATOM}title") for entry in entries]
    assert not any("Unreleased" in title for title in titles)


def test_entry_content_is_that_release_s_changelog_section(entries: list[ET.Element]) -> None:
    """The newest entry carries its own section's text and none of the next release's."""
    text = CHANGELOG.read_text(encoding="utf-8")
    version = _top_released_version()
    section = text.split(f"## [{version}]", 1)[1].split("\n## [", 1)[0]
    first_bullet = next(line for line in section.splitlines() if line.startswith("- "))
    content = entries[0].findtext(f"{ATOM}content")
    assert first_bullet.strip() in content
    assert "\n## [" not in content
