"""Capture the two docs images that are NOT screenshots of a Shortlist screen.

Same gate and the same technique as `test_screenshots.py` (skipped unless `SHOTS_DIR` is set, so
CI never runs it), but the subject is different: these compose an HTML page and photograph that,
because neither picture is a Shortlist screen.

    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_marketing_assets.py -m e2e --no-cov -n0

- `plex-picked-for-you.png` the hero: one delivered row as a Plex Home shelf. Replaces a real
                      screenshot of the maintainer's own server, on which four of the eight visible
                      titles carried Plex's watched tick — a "Picked for You" row advertising films
                      the viewer had already seen, which is the exact failure the product exists to
                      prevent. Built from the harness instead, so the shelf holds what the engine
                      actually delivered and the test can ASSERT nothing on it is watched.
- `two-account.png`   two accounts' Plex Home hubs, side by side, read through their OWN server
                      tokens. Every poster on it is the fake PMS's real answer to that account's
                      token, so the picture cannot claim a privacy result the harness doesn't have.
- `social-preview.png` the og:image card. No live data at all, so it needs no Plex fixture.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from playwright.sync_api import Browser

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

SHOTS_DIR = os.environ.get("SHOTS_DIR")
skip_unless_capturing = pytest.mark.skipif(not SHOTS_DIR, reason="set SHOTS_DIR to capture screenshots")

#: The docs site's own tokens (docs/assets/css/main.css). Duplicated rather than imported because
#: these images are rendered by a browser that never loads the site's stylesheet — keeping the hex
#: values here, named the same, is what makes a drift visible when someone re-reads both files.
PALETTE = """
  --bg: #08080a;
  --surface: #121216;
  --surface-2: #191920;
  --border: rgba(255, 255, 255, 0.09);
  --text: #f4f4f5;
  --muted: #94949f;
  --amber: #e5a00d;
  --amber-bright: #f5c04a;
"""

#: Tiles per shelf on the two-account image. A row holds far more titles than this; showing them
#: all at a readable size needs a canvas nobody will open, and the point of the picture is that the
#: two accounts hold DIFFERENT titles, not how many each holds.
TILES = 5


def _shot_path(name: str) -> Path:
    out = Path(SHOTS_DIR) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


#: Tiles across the hero shelf. Eight fit at a readable size in the 1600px the docs site reserves,
#: and a ninth is rendered deliberately half-clipped by the right edge — a Plex shelf always
#: continues past the fold, and one ending flush reads as a short row rather than a scrollable one.
HERO_TILES = 8


def _hero_shelf(app: ShortlistApp, account_id: int) -> tuple[str, list[dict[str, str]]]:
    """That account's first Shortlist row, as (row title, tiles)."""
    for _key, title, tiles in _rows_visible_to(app, account_id):
        if tiles:
            return title, tiles
    raise AssertionError(f"account {account_id} has no Shortlist row to photograph")


def _hero_html(pms_url: str, row_title: str, tiles: list[dict[str, str]]) -> str:
    cards = "".join(
        f"""
      <li class="tile">
        <img src="{html.escape(pms_url + tile["thumb"])}" alt="">
        <p class="tile__title">{html.escape(tile["title"])}</p>
        <p class="tile__year">{html.escape(tile["year"])}</p>
      </li>"""
        for tile in tiles
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{{PALETTE}
    --font: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  /* Plex's own shelf background, not the docs site's: the picture is meant to read as Plex. */
  body {{
    margin: 0; padding: 26px 0 22px 28px; width: 1600px; overflow: hidden;
    background: #101013; color: #f4f4f5; font-family: var(--font); -webkit-font-smoothing: antialiased;
  }}
  .shelf {{ display: flex; align-items: center; gap: 12px; margin: 0 28px 18px 0; }}
  .shelf h2 {{ margin: 0; font-size: 30px; font-weight: 700; letter-spacing: -0.01em; }}
  .shelf .chevrons {{ margin-left: auto; display: flex; gap: 18px; color: #6f6f7a; }}
  .shelf svg {{ width: 22px; height: 22px; }}
  /* One row that overflows on purpose — see HERO_TILES. */
  .tiles {{ display: flex; gap: 16px; margin: 0; padding: 0; list-style: none; }}
  .tile {{ flex: 0 0 168px; }}
  .tile img {{
    display: block; width: 168px; height: 252px; object-fit: cover;
    border-radius: 6px; background: var(--surface-2);
  }}
  .tile__title {{
    margin: 10px 0 2px; font-size: 15px; font-weight: 500; color: #e8e8ec;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .tile__year {{ margin: 0; font-size: 14px; color: #86868f; }}
</style></head>
<body>
  <div class="shelf">
    <h2>{html.escape(row_title)}</h2>
    <span class="chevrons">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 6l-6 6 6 6"/></svg>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg>
    </span>
  </div>
  <ul class="tiles">{cards}</ul>
</body></html>"""


@skip_unless_capturing
def test_capture_hero_shelf(browser: Browser, app: ShortlistApp, reset_fake_plex) -> None:
    """Sarah's delivered movie row, drawn as the Plex shelf it becomes.

    The assertion is the point of doing this from the harness rather than by hand: every title on
    the hero must be one Sarah has NOT watched. The image it replaces failed that, and nothing could
    have told anyone, because it was a photograph.
    """
    state = reset_fake_plex
    build_real_rows(app)

    row_title, tiles = _hero_shelf(app, 201)
    assert len(tiles) >= HERO_TILES, f"the row holds {len(tiles)} titles — too few to fill the hero"
    tiles = tiles[: HERO_TILES + 1]

    watched = state.watched_now(201)
    already_seen = [tile["title"] for tile in tiles if int(tile["key"]) in watched]
    assert not already_seen, f"the hero would advertise titles Sarah has already watched: {already_seen}"
    assert all(tile["thumb"] for tile in tiles), "a tile has no artwork path — the hero would show a gap"

    context = browser.new_context(viewport={"width": 1600, "height": 440}, device_scale_factor=2)
    page = context.new_page()
    page.set_content(_hero_html(app.pms_url, row_title, tiles))
    # Posters come off the fake PMS over HTTP, so the shelf is empty until they land.
    page.wait_for_load_state("networkidle")
    page.locator("body").screenshot(path=str(_shot_path("plex-picked-for-you.png")))
    context.close()


def _rows_visible_to(app: ShortlistApp, account_id: int) -> list[tuple[int, str, list[dict[str, str]]]]:
    """Every Shortlist row on that account's own Plex Home, with the items inside it.

    Asked of the fake PMS with `X-Plex-Token: server-<account_id>`, which is the same call
    `test_privacy_uninstall_e2e.py` uses to prove nobody sees anyone else's row. Reading it any
    other way (Shortlist's own API, the owner's token) would produce a picture of what Shortlist
    believes rather than of what Plex answers.

    Returns:
        One `(rating_key, row title, tiles)` per visible row, in hub order. A tile carries the
        item's key, title, year and artwork path: both pictures draw posters, and the key is what
        lets the hero assert it is not advertising something the viewer already watched.
    """
    rows: list[tuple[int, str, list[dict[str, str]]]] = []
    for hub in app.plex_hubs_as(account_id):
        key = str(hub.get("key") or "")
        match = re.search(r"/library/collections/(\d+)", key)
        if match is None:
            continue  # Continue Watching and friends: not ours, and not what this picture is about
        response = httpx.get(
            f"{app.pms_url}{key}",
            headers={"X-Plex-Token": f"server-{account_id}"},
            timeout=30,
        )
        response.raise_for_status()
        container = ElementTree.fromstring(response.text)
        # Plex serves movies as <Video> and shows as <Directory>; a row can hold either.
        tiles = [
            {
                "key": element.get("ratingKey") or "",
                "title": title,
                "year": element.get("year") or "",
                "thumb": element.get("thumb") or "",
            }
            for element in container
            if element.tag in ("Video", "Directory") and (title := element.get("title"))
        ]
        rows.append((int(match.group(1)), str(hub.get("title") or ""), tiles))
    return rows


def _poster_tile(pms_url: str, tile: dict[str, str]) -> str:
    """One poster with its title under it.

    The caption is not redundant with the artwork's own title block: at five tiles across half of a
    1440px canvas a poster is ~130px wide, and the point of this picture is *which titles* each
    account holds — a title only legible at full size would not make it.
    """
    return (
        f'<figure class="tile"><img src="{html.escape(pms_url + tile["thumb"])}" alt="">'
        f"<figcaption>{html.escape(tile['title'])}</figcaption></figure>"
    )


def _column_html(
    pms_url: str, display_name: str, rows: list[tuple[int, str, list[dict[str, str]]]], hidden: list[str]
) -> str:
    """One account's Home column."""
    shelves = "".join(
        f"""
        <div class="shelf">
          <h3>{html.escape(title)}</h3>
          <div class="tiles">
            {"".join(_poster_tile(pms_url, tile) for tile in tiles[:TILES])}
          </div>
        </div>"""
        for _key, title, tiles in rows
    )
    hidden_html = "".join(f"<span>{html.escape(name)}</span>" for name in hidden)
    return f"""
    <section class="home">
      <header>
        <span class="avatar">{html.escape(display_name[0].upper())}</span>
        <div>
          <strong>{html.escape(display_name)}</strong>
          <span>Plex Home</span>
        </div>
      </header>
      {shelves}
      <footer>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M3 3l18 18"/>
          <path d="M10.6 5.1A9.9 9.9 0 0 1 12 5c5 0 9 4.5 9 7a12 12 0 0 1-2.2 3.2"/>
          <path d="M6.6 6.7C4.3 8.1 3 10.3 3 12c0 2.5 4 7 9 7a9.6 9.6 0 0 0 4.4-1.1"/>
          <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>
        </svg>
        <p>Not on this Home: {hidden_html}</p>
      </footer>
    </section>"""


def _two_account_html(columns: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  :root {{{PALETTE}
    --font: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 44px 48px 40px; width: 1440px;
    background: var(--bg); color: var(--text); font-family: var(--font);
    -webkit-font-smoothing: antialiased;
  }}
  .head {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 30px; }}
  .head h1 {{ font-size: 30px; letter-spacing: -0.02em; margin: 0; }}
  .head p {{ margin: 0; color: var(--muted); font-size: 17px; }}
  /* Equal-height columns with the footer pinned down: someone who watches both films and TV has
     a row in each library and someone who watches one has one, so the two cards hold different
     numbers of shelves. Left to size themselves the shorter card ends halfway up the image and
     reads as a broken render rather than as the point being made. */
  .cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 28px; align-items: stretch; }}
  .home {{
    display: flex; flex-direction: column;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 18px; padding: 22px 24px 20px;
  }}
  .home > footer {{ margin-top: auto; }}
  .home > header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }}
  .avatar {{
    display: grid; place-items: center; width: 40px; height: 40px; border-radius: 50%;
    background: var(--amber); color: #1a1200; font-weight: 800; font-size: 18px;
  }}
  .home > header strong {{ display: block; font-size: 18px; }}
  .home > header span:last-child {{ color: var(--muted); font-size: 13px; }}
  .shelf {{ margin-bottom: 22px; }}
  .shelf h3 {{
    margin: 0 0 10px; font-size: 15px; font-weight: 700; color: var(--amber-bright);
  }}
  .tiles {{ display: grid; grid-template-columns: repeat({TILES}, 1fr); gap: 10px; }}
  .tile {{ margin: 0; }}
  .tile img {{
    display: block; width: 100%; aspect-ratio: 2 / 3; object-fit: cover;
    border-radius: 10px; border: 1px solid var(--border); background: var(--surface-2);
  }}
  .tile figcaption {{
    margin-top: 7px; text-align: center; color: var(--muted); font-size: 12px; font-weight: 600;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .home > footer {{
    display: flex; align-items: center; gap: 9px;
    border-top: 1px solid var(--border); padding-top: 14px; color: var(--muted); font-size: 13px;
  }}
  .home > footer svg {{ flex: none; width: 16px; height: 16px; }}
  .home > footer p {{ margin: 0; }}
  .home > footer span {{ color: var(--text); }}
  .home > footer span + span::before {{ content: ", "; color: var(--muted); }}
  .foot {{ margin: 26px 0 0; color: var(--muted); font-size: 15px; text-align: center; }}
  .foot b {{ color: var(--text); font-weight: 600; }}
</style></head>
<body>
  <div class="head">
    <h1>Same server, same night</h1>
    <p>Each Home read with that account's own Plex token</p>
  </div>
  <div class="cols">{columns}</div>
  <p class="foot">
    <b>Neither can see the other's row.</b>
    Plex is told to hide it from them, so it never reaches their Home in the first place.
  </p>
</body></html>"""


@skip_unless_capturing
def test_capture_two_account_image(browser: Browser, app: ShortlistApp, reset_fake_plex) -> None:
    """Two accounts' real Home hubs, side by side.

    Sarah and mike, not the canary: the harness gives them deliberately disjoint watch sets (sarah
    movies plus some TV, mike a different set of shows), so their rows always hold different
    titles, while the canary has no history and falls back to the cold-start row.
    """
    state = reset_fake_plex
    build_real_rows(app)

    owner_of = {}  # rating key -> slug, from the labels the fake PMS actually stored
    for collection in state.collections.values():
        for label in collection.labels:
            if label.lower().startswith("shortlist_"):
                owner_of[collection.rating_key] = label.lower().removeprefix("shortlist_")

    seen = {slug: _rows_visible_to(app, account_id) for account_id, slug in ((201, "Sarah"), (202, "Mike"))}
    for slug, rows in seen.items():
        assert rows, f"{slug} has no row to photograph — the run did not deliver one"
        assert all(tiles for _key, _title, tiles in rows), f"{slug} has an empty row"
        # A tile with no artwork path photographs as a blank box, and the picture would still be
        # committed. The image is the only place that failure would ever show.
        assert all(tile["thumb"] for _key, _title, tiles in rows for tile in tiles[:TILES]), (
            f"{slug} has a tile with no artwork"
        )

    columns = ""
    for slug, rows in seen.items():
        visible = {key for key, _title, _items in rows}
        # Whose rows exist on this server and are NOT on this Home. Counted per owner rather than
        # collected as a set of names, because someone who watches both films and TV has a row in
        # each library — calling that "Sarah's row" would undercount what is being hidden.
        withheld: dict[str, int] = {}
        for key, owner in owner_of.items():
            if key not in visible and owner != slug.lower():
                withheld[owner.title()] = withheld.get(owner.title(), 0) + 1
        hidden = [
            f"{name}'s row" if count == 1 else f"{name}'s {count} rows" for name, count in sorted(withheld.items())
        ]
        columns += _column_html(app.pms_url, slug, rows, hidden)

    context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    page = context.new_page()
    page.set_content(_two_account_html(columns))
    # Posters come off the fake PMS over HTTP, so the columns are empty until they land.
    page.wait_for_load_state("networkidle")
    page.locator("body").screenshot(path=str(_shot_path("two-account.png")))
    context.close()


@skip_unless_capturing
def test_capture_social_preview(browser: Browser) -> None:
    """The og:image card, from a static HTML source anyone can edit without touching Python.

    Captured at device_scale_factor=1: `docs/_config.yml` wants exactly 1280x640 pixels, because
    Reddit, Discord, Slack and X crop anything taller than 2:1. That is a pixel size, not a CSS
    size, so the 2x used for in-app screenshots would produce a 2560x1280 file.
    """
    source = Path(__file__).parent / "assets" / "social_preview.html"
    context = browser.new_context(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
    page = context.new_page()
    page.goto(source.as_uri())
    page.wait_for_timeout(300)
    page.screenshot(path=str(_shot_path("social-preview.png")))
    context.close()
