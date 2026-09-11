"""Build `docs/llms-full.txt` — every docs page's text in one file, for AI agents.

`llms.txt` is the index: what Shortlist is, how it differs from the tools people are usually
pointed at first, and a list of links. This is the companion an agent fetches when it wants the
whole corpus without crawling 25 pages, which is the convention the two filenames come from.

Generated rather than hand-written, and committed rather than built by Jekyll, for three reasons:

- GitHub Pages only runs its allow-listed plugins, so there is nowhere to hook a build step.
- Doing it in Liquid means `page.content` inside a loop over `site.pages`, whose render order
  Jekyll does not promise — the output would depend on which pages happened to be converted first.
- A committed artifact can be diffed in review, and `tests/unit/test_llms_full.py` fails the build
  if a docs edit lands without regenerating it, so it cannot rot silently.

Run after any docs change:

    python scripts/build_llms_full.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parent.parent / "docs"
OUT = DOCS / "llms-full.txt"

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
LIQUID_COMMENT = re.compile(r"\{%-?\s*comment\s*-?%\}.*?\{%-?\s*endcomment\s*-?%\}", re.DOTALL)
JSON_LD = re.compile(r'<script type="application/ld\+json">.*?</script>', re.DOTALL)
INCLUDE = re.compile(r"\{%-?\s*include\s+([\w.-]+)\s*-?%\}")
RELATIVE_URL = re.compile(r"""\{\{\s*['"]([^'"]+)['"]\s*\|\s*relative_url\s*\}\}""")
PAGE_VAR = re.compile(r"\{\{\s*page\.(\w+)(?:\s*\|[^}]*)?\}\}")
SITE_VAR = re.compile(r"\{\{\s*site\.(\w+)(?:\s*\|[^}]*)?\}\}")
HTML_TAG = re.compile(r"<[^>]+>")
SVG = re.compile(r"<svg\b.*?</svg>", re.DOTALL)
BLANK_RUN = re.compile(r"\n{3,}")
# Heading anchors, the one piece of real markup inside the prose. Deliberately NOT the blanket
# HTML_TAG above: these pages are full of angle-bracket placeholders (`label!=shortlist_<userslug>`,
# `http://<host>:5959`) that a general tag strip would silently eat.
ANCHOR_SPAN = re.compile(r'<span id="[^"]*"></span>\n?')
# Notes to whoever edits the page next. Invisible to a reader on the site, so they have no place
# in a file whose whole purpose is to be read as text.
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# Inline links between pages, written as repo-relative .md paths so they also work when someone
# reads these files on github.com. `jekyll-relative-links` rewrites them for the site; this file
# gets no such plugin, so a raw `guides/ai.md` here would be a dead end for an agent citing it.
MD_LINK = re.compile(r"\]\((?!https?:|#|mailto:)([^)#]+?)\.md(#[^)]*)?\)")


def _load_config() -> dict:
    """Jekyll's config, which carries the site URL, the nav order and the image namespaces."""
    text = (DOCS / "_config.yml").read_text()
    return yaml.safe_load(text)


def _nav_urls(config: dict) -> list[str]:
    """Every page URL in `_config.yml`'s nav, depth-first — the site's own reading order.

    `_config.yml` calls nav "the single place page order is defined", so this file inherits that
    rather than inventing a second ordering that could disagree with the sidebar.
    """
    urls: list[str] = []
    for entry in config.get("nav", []):
        urls.append(entry["url"])
        for child in entry.get("children", []):
            urls.append(child["url"])
    return urls


def _url_to_source(url: str) -> Path:
    """`/guides/rows/` -> `docs/guides/rows.md`. The site is `permalink: pretty`."""
    return DOCS / f"{url.strip('/')}.md"


def _include_as_text(name: str) -> str:
    """Flatten an HTML include to prose.

    Done generically rather than per-include: the one include that reaches a page today
    (`privacy-order.html`, the four-step write order on the FAQ) is a drawn figure whose every word
    is real text, so stripping the markup leaves exactly the sentences a reader sees. Hand-writing
    a plain-text copy here would be a second source of truth for the privacy ordering — the one
    claim in these docs that must never drift.
    """
    raw = (DOCS / "_includes" / name).read_text()
    raw = LIQUID_COMMENT.sub("", raw)
    raw = SVG.sub("", raw)
    # Close each list item and caption onto its own line before the tags go, or the steps run
    # together into one paragraph.
    raw = re.sub(r"</(li|figcaption|p|div)>", "\n", raw)
    raw = re.sub(r"</(strong|span)>", ": ", raw)
    text = HTML_TAG.sub("", raw)
    text = text.replace("&rsquo;", "'").replace("&amp;", "&").replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", line).strip(" :") for line in text.splitlines()]
    return "\n".join(f"  {line}" for line in lines if line)


def _absolute_links(body: str, source: Path, config: dict) -> str:
    """Turn page-relative `.md` links into absolute site URLs.

    Resolved against the LINKING page's directory, not the docs root: `reference/settings.md`
    links to its sibling as `concepts.md`, which from the root would resolve to a page that does
    not exist.
    """

    def _one(match: re.Match[str]) -> str:
        target = (source.parent / f"{match.group(1)}.md").resolve()
        try:
            slug = str(target.relative_to(DOCS).with_suffix(""))
        except ValueError:  # points outside docs/ — leave it exactly as the author wrote it
            return match.group(0)
        slug = "" if slug == "index" else f"{slug}/"
        return f"]({config['url']}/{slug}{match.group(2) or ''})"

    return MD_LINK.sub(_one, body)


def _render(body: str, front: dict, config: dict, source: Path) -> str:
    """Resolve the handful of Liquid constructs these pages use, and drop what is markup-only."""
    body = LIQUID_COMMENT.sub("", body)
    body = JSON_LD.sub("", body)
    body = ANCHOR_SPAN.sub("", body)
    body = HTML_COMMENT.sub("", body)
    body = INCLUDE.sub(lambda m: _include_as_text(m.group(1)), body)
    body = RELATIVE_URL.sub(lambda m: f"{config['url']}{m.group(1)}", body)
    body = SITE_VAR.sub(lambda m: str(config.get(m.group(1), m.group(0))), body)

    def _page(match: re.Match[str]) -> str:
        value = front.get(match.group(1))
        # `facts_checked` is a date and reaches here as a `datetime.date`; every other page var in
        # these docs is already a string.
        if value is None:
            return ""
        return value.strftime("%-d %B %Y") if hasattr(value, "strftime") else str(value)

    body = PAGE_VAR.sub(_page, body)
    body = _absolute_links(body, source, config)
    return BLANK_RUN.sub("\n\n", body).strip()


def _page_section(source: Path, config: dict) -> str:
    raw = source.read_text()
    match = FRONT_MATTER.match(raw)
    if not match:
        raise SystemExit(f"{source.relative_to(DOCS)} has no front matter")
    front = yaml.safe_load(match.group(1)) or {}
    body = _render(raw[match.end() :], front, config, source)

    slug = str(source.relative_to(DOCS).with_suffix(""))
    url = config["url"] + "/" + ("" if slug == "index" else f"{slug}/")

    parts = [f"# {front['title']}", f"Source: {url}", "", front["description"].strip()]
    if body:
        parts += ["", body]
    return "\n".join(parts)


def build() -> str:
    config = _load_config()
    ordered = [DOCS / "index.md"] + [_url_to_source(u) for u in _nav_urls(config)]

    every = sorted(
        p for p in DOCS.rglob("*.md") if p.name != "README.md" and "superpowers" not in p.relative_to(DOCS).parts
    )
    missing = [p for p in every if p not in ordered]
    if missing:
        names = ", ".join(str(p.relative_to(DOCS)) for p in missing)
        raise SystemExit(f"page(s) not in _config.yml nav, so they would be dropped: {names}")

    # llms.txt is a Jekyll page, so strip its front matter before reusing the prose: left in,
    # its `permalink: /llms.txt` would publish THIS file on top of the real llms.txt.
    index_page = FRONT_MATTER.sub("", (DOCS / "llms.txt").read_text())
    header = index_page.split("\n## Docs")[0].rstrip()
    sections = [_page_section(p, config) for p in ordered]

    return (
        "---\nlayout: null\nsitemap: false\npermalink: /llms-full.txt\n---\n"
        f"{header}\n\n"
        "# Full documentation\n\n"
        "Every page of the Shortlist documentation, in the site's own reading order. The index "
        f"version of this file is at {config['url']}/llms.txt.\n\n" + "\n\n---\n\n".join(sections) + "\n"
    )


if __name__ == "__main__":
    text = build()
    OUT.write_text(text)
    pages = text.count("\nSource: ")
    print(f"wrote {OUT.relative_to(Path.cwd())}: {len(text):,} bytes, {pages} pages", file=sys.stderr)
