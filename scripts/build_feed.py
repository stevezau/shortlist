"""Build `docs/feed.xml` — an Atom feed of Shortlist releases, from CHANGELOG.md.

One entry per released version heading (`## [x.y.z] - YYYY-MM-DD`), newest first, at most 20. Each
links to that version's GitHub release and carries its CHANGELOG section as plain text. The
`[Unreleased]` section never appears.

Generated and committed for the same reason as `docs/llms-full.txt`: GitHub Pages runs only its
allow-listed plugins, so nothing can build this at deploy time. `tests/unit/test_feed.py` fails if the
committed file drifts from CHANGELOG.md. Run after every release:

    python scripts/build_feed.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"
OUT = REPO / "docs" / "feed.xml"

SITE = "https://shortlistapp.dev"
RELEASES = "https://github.com/stevezau/shortlist/releases/tag/v"
MAX_ENTRIES = 20

#: `## [1.9.3] - 2026-09-27`, and the one withdrawn release's `## [1.5.0] - 2026-08-13 — withdrawn`.
RELEASE = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})(?P<note>[^\n]*)$", re.M)


def releases(text: str) -> list[dict[str, str]]:
    """Every dated release in CHANGELOG order (newest first), with its section body."""
    headings = list(RELEASE.finditer(text))
    found = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        body = text[heading.end() : end]
        # The link-reference block at the very end of a changelog is not part of the last release.
        body = re.split(r"^\[[^\]]+\]:\s", body, maxsplit=1, flags=re.M)[0]
        note = heading.group("note").strip(" —-")
        found.append(
            {
                "version": heading.group("version"),
                "date": heading.group("date"),
                "note": note,
                "body": body.strip(),
            }
        )
    return found


def _entry(release: dict[str, str]) -> str:
    title = f"Shortlist {release['version']}" + (f" ({release['note']})" if release["note"] else "")
    link = RELEASES + release["version"]
    stamp = f"{release['date']}T00:00:00Z"
    return (
        "  <entry>\n"
        f"    <title>{escape(title)}</title>\n"
        f'    <link href={quoteattr(link)} rel="alternate" type="text/html"/>\n'
        f"    <id>{escape(link)}</id>\n"
        f"    <updated>{stamp}</updated>\n"
        f'    <content type="text">{escape(release["body"])}</content>\n'
        "  </entry>\n"
    )


def build() -> str:
    newest = releases(CHANGELOG.read_text(encoding="utf-8"))[:MAX_ENTRIES]
    if not newest:
        raise SystemExit("CHANGELOG.md has no released version headings")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <title>Shortlist releases</title>\n"
        "  <subtitle>New releases of Shortlist, private per-user recommendation rows for Plex.</subtitle>\n"
        f'  <link href="{SITE}/feed.xml" rel="self" type="application/atom+xml"/>\n'
        f'  <link href="{SITE}/" rel="alternate" type="text/html"/>\n'
        f"  <id>{SITE}/feed.xml</id>\n"
        f"  <updated>{newest[0]['date']}T00:00:00Z</updated>\n"
        "  <author><name>Steven Adams</name></author>\n" + "".join(_entry(r) for r in newest) + "</feed>\n"
    )


if __name__ == "__main__":
    feed = build()
    OUT.write_text(feed, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)}: {feed.count('<entry>')} releases", file=sys.stderr)
