"""One install command on the website, produced by one include.

The site used to print the install command eight times: a Docker Hub `docker run` on six Plex how-to
pages and the getting-started guide, a curl-the-example-compose recipe on the landing page, and a GHCR
`docker run` beside it, each followed by its own paragraph explaining the doubled z in `stevezzau`. Eight
copies drift. `docs/_includes/install.html` is now the only place a reader is shown how to run Shortlist,
and getting-started is the only page that names the Docker Hub account, in its "Other ways to install".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
INCLUDE = DOCS / "_includes" / "install.html"
GETTING_STARTED = DOCS / "getting-started.md"

#: Files allowed to spell an install command out by hand: the include itself, and getting-started's
#: "Other ways to install", which is where the alternatives to the one command live.
ALLOWED = {INCLUDE, GETTING_STARTED}

FENCE = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1", re.M | re.S)
PRE = re.compile(r"<pre\b.*?</pre>", re.S | re.I)
INSTALL_TEXT = ("docker run", "image:")


def _sources() -> list[Path]:
    """Every published page and template."""
    pages = list(DOCS.rglob("*.md"))
    templates = [*DOCS.glob("_layouts/*.html"), *DOCS.glob("_includes/*.html")]
    return sorted(pages + templates)


def _code_blocks(text: str) -> list[str]:
    return [m.group(0) for m in FENCE.finditer(text)] + PRE.findall(text)


@pytest.mark.parametrize("source", _sources(), ids=lambda p: str(p.relative_to(DOCS)))
def test_only_getting_started_names_the_docker_hub_account(source: Path) -> None:
    if source == GETTING_STARTED:
        return
    assert "stevezzau" not in source.read_text(encoding="utf-8"), (
        f"{source.relative_to(DOCS)} names the Docker Hub account; use {{% include install.html %}} "
        "(GHCR) and leave the doubled-z explanation to getting-started"
    )


@pytest.mark.parametrize("source", _sources(), ids=lambda p: str(p.relative_to(DOCS)))
def test_install_commands_come_from_the_include(source: Path) -> None:
    if source in ALLOWED:
        return
    hand_written = [
        block.strip().splitlines()[0]
        for block in _code_blocks(source.read_text(encoding="utf-8"))
        if any(marker in block for marker in INSTALL_TEXT)
    ]
    assert not hand_written, (
        f"{source.relative_to(DOCS)} spells out an install command by hand: {hand_written}. "
        'Use {% include install.html %} (or variant="run")'
    )


def test_the_include_installs_from_ghcr() -> None:
    text = INCLUDE.read_text(encoding="utf-8")
    assert "{{ site.ghcr }}" in text
    assert "stevezzau" not in text


def test_getting_started_explains_the_docker_hub_name_once() -> None:
    text = GETTING_STARTED.read_text(encoding="utf-8")
    other_ways = text.split("## Other ways to install", 1)
    assert len(other_ways) == 2, "getting-started needs an 'Other ways to install' section"
    before, after = other_ways
    assert "stevezzau" not in before, "the Docker Hub name belongs under 'Other ways to install' only"
    assert "stevezzau" in after
    assert "{% include install.html" in before, "getting-started's main install must be the include"
