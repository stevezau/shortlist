"""Every relative link in `docs/` must land on a file that exists, at a heading that exists.

Jekyll publishes a link to a page that is gone, and a link into a heading that was renamed, without
complaining — so the failure only shows up when a reader clicks it. That risk is not theoretical
here: the docs are cross-linked between seven Plex how-to pages, eight guides and a reference, and a
page split or a retitled heading breaks inbound links silently.

Deliberately relative links only, and no YAML: `_config.yml`'s nav and the front-matter permalinks
would need a parser this suite does not otherwise depend on, and near enough every internal link the
site actually carries is written as a relative path to another source file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"

#: `[text](target)`, with an optional `"title"` after the target.
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
#: kramdown's `{#explicit-id}` suffix, and any hand-written HTML anchor.
EXPLICIT_ID = re.compile(r"\{#([A-Za-z0-9_-]+)\}\s*$")
HTML_ID = re.compile(r"""\sid=["']([A-Za-z0-9_-]+)["']""")
#: Liquid (`{{ ... }}`, `{% ... %}`) resolves at build time, so it is not ours to check.
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "{{", "{%", "#{{")


def _slugify(text: str) -> str:
    """The heading id GitHub Pages actually produces, checked against the deployed HTML.

    Derived by fetching all 24 published pages and diffing their real `<h*, id=...>` values against
    this function, because the obvious sources are both wrong here. The stock `kramdown` gem DELETES
    underscores (`source_viewed_at` -> `sourceviewedat`), and trusting it cost a live regression:
    two correct anchors in the split reference were "fixed" into two broken ones. GitHub Pages runs
    kramdown with the GFM parser, which KEEPS them.

    The other detail worth keeping, and the one a tidier regex gets wrong: runs are not collapsed.
    Deleting a character that sat between two spaces leaves two spaces, and each becomes its own
    hyphen — "Requests (Radarr / Sonarr, or Overseerr)" is `requests-radarr--sonarr-or-overseerr`.

    Re-derive it the same way if it ever drifts: compare against the deployed site, never against a
    local markdown library.
    """
    # The slug comes from the RENDERED text, so inline code and link markup go first.
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[^a-zA-Z]+", "", text)
    text = re.sub(r"[^a-zA-Z0-9 _-]", "", text)
    return text.replace(" ", "-").lower()


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = set(HTML_ID.findall(text))
    for raw in HEADING.findall(text):
        if explicit := EXPLICIT_ID.search(raw):
            found.add(explicit.group(1))
            raw = EXPLICIT_ID.sub("", raw)
        found.add(_slugify(raw))
    return found


def _pages() -> list[Path]:
    """Published sources only — `_layouts`, `_includes` and `_data` are Jekyll's, not the reader's."""
    return sorted(
        path for path in DOCS.rglob("*.md") if not any(part.startswith("_") for part in path.relative_to(DOCS).parts)
    )


@pytest.mark.parametrize("page", _pages(), ids=lambda p: str(p.relative_to(DOCS)))
def test_every_relative_link_resolves(page: Path) -> None:
    broken: list[str] = []
    for target in LINK.findall(page.read_text(encoding="utf-8", errors="replace")):
        if target.startswith(SKIP_PREFIXES):
            continue
        url, _, fragment = target.partition("#")
        if not url:  # a bare "#anchor": same page
            if fragment and fragment not in _anchors(page):
                broken.append(f"#{fragment} — this page has no such heading")
            continue
        if url.startswith("/"):
            continue  # a site-absolute path; Jekyll's permalinks decide it, not the file tree
        resolved = (page.parent / url).resolve()
        if not resolved.exists():
            broken.append(f"{url} — no such file")
        elif fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
            broken.append(f"{url}#{fragment} — {resolved.name} has no such heading")
    assert not broken, f"{page.relative_to(DOCS)} links nowhere:\n  " + "\n  ".join(broken)
