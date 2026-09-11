"""`docs/llms-full.txt` is generated and committed, so something has to fail when it goes stale.

The file is the whole docs corpus in one fetch, for AI agents that would otherwise crawl 25 pages.
Nothing in the Jekyll build produces it — GitHub Pages runs only its allow-listed plugins — so it is
built by `scripts/build_llms_full.py` and committed. A committed derivative with no check rots the
first time someone edits a page and doesn't know the script exists, and it rots INVISIBLY: the site
keeps serving a file that confidently describes the docs as they were months ago.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs"
GENERATED = DOCS / "llms-full.txt"


def _load_builder():
    """Import the build script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("build_llms_full", REPO / "scripts" / "build_llms_full.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built() -> str:
    return _load_builder().build()


def test_committed_file_matches_a_fresh_build(built: str) -> None:
    """The actual drift guard: edit a docs page without rerunning the script and this fails."""
    committed = GENERATED.read_text()
    assert committed == built, "docs/llms-full.txt is out of date with docs/. Run: python scripts/build_llms_full.py"


def test_it_publishes_at_its_own_url_not_over_llms_txt() -> None:
    """Pinning a bug this file actually had.

    The header is lifted from `llms.txt`, which is itself a Jekyll page carrying
    `permalink: /llms.txt`. Copied verbatim, that front matter came along, and Jekyll would have
    published llms-full.txt ON TOP of the real llms.txt — losing the index file and serving 450KB
    to every agent that asked for the short one. Nothing about the output looked wrong.
    """
    head = GENERATED.read_text()[:200]
    assert "permalink: /llms-full.txt" in head
    assert "permalink: /llms.txt" not in head


def test_no_unrendered_liquid_reaches_the_reader(built: str) -> None:
    """An agent reading `{{ site.docker_image }}` gets a worse answer than one reading nothing."""
    leaked = re.findall(r"\{\{.*?\}\}|\{%.*?%\}", built, re.DOTALL)
    assert not leaked, f"unrendered Liquid in llms-full.txt: {leaked[:3]}"


def test_every_published_page_is_included(built: str) -> None:
    """A page missing from `_config.yml` nav would be silently dropped from the corpus.

    `build()` raises on that, so this asserts the positive: every page that ships has a section.
    """
    pages = {p for p in DOCS.rglob("*.md") if p.name != "README.md" and "superpowers" not in p.relative_to(DOCS).parts}
    assert len(re.findall(r"^Source: ", built, re.MULTILINE)) == len(pages)


def test_the_faq_answers_survive_the_strip(built: str) -> None:
    """The FAQ page ends in a Liquid loop building JSON-LD, and the stripper removes it.

    Worth a test because the failure is one-sided: strip slightly too much and the FAQ prose — the
    single most useful page for an agent answering a question about Shortlist — vanishes while the
    file still looks complete and still has its 25 sections.
    """
    assert "How is this private? Plex doesn't have per-user collections." in built
    assert "Will it fight with Kometa?" in built
    assert '"@type": "FAQPage"' not in built


def test_the_privacy_write_order_is_carried_as_prose(built: str) -> None:
    """The four-step ordering reaches the FAQ through an HTML include, not as markdown.

    It is the load-bearing claim in these docs — get the order wrong and rows leak — so an agent
    summarising Shortlist's privacy model must be able to read it here, not just see a dropped tag.
    """
    for step in ("Send the row in hidden", "Hide it from everyone else", "Now show it"):
        assert step in built
