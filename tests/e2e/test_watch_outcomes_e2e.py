"""E2E: watched vs finished, in a real browser against the real API.

The unit suites can prove the numbers. Only this layer can prove the PAGE: that the pair renders,
that the split bar never draws a segment wider than the bar containing it, and that adding a second
number to a line already tight on a phone did not push the dashboard sideways.

    .venv/bin/python -m pytest tests/e2e/test_watch_outcomes_e2e.py -m e2e --no-cov -n0
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp
from tests.fakes.fake_plex import FakeHistoryEntry

pytestmark = pytest.mark.e2e

PHONE = {"width": 390, "height": 844}


def seed_outcomes(app: ShortlistApp) -> None:
    """Picks covering every cell of the matrix, across two rows and two libraries.

    Written straight to SQLite because there is no API that sets `finished_at` — it is stamped by the
    nightly reconcile from Plex's own counts, and the point here is the RENDERING of a state the
    server can legitimately be in, not the route that produces it.
    """
    now = datetime.now(UTC)
    db = app.config_dir / "shortlist.db"
    # Create the second row through the REAL API, not a raw INSERT. `collections` has 27 NOT NULL
    # columns whose defaults live in Python, so a hand-written INSERT fails the constraint — and an
    # `INSERT OR IGNORE` swallows that failure, leaving the row absent and the test asserting
    # against the deleted-row branch instead. (Cost me a red run; leaving the reason here.)
    created = app.api("POST", "/api/collections", json={"name": "My Faves"})
    assert created.status_code == 201, created.text
    faves_slug = created.json()["slug"]

    rows = [
        # (tmdb, media_type, slug, library, delivered_ago, watched_ago, finished_ago)
        (901, "movie", "picked", "Movies", 20, 18, 18),  # film: watched == finished
        (902, "movie", "picked", "Movies", 20, 17, 17),
        (903, "movie", "picked", "Movies", 20, None, None),  # never opened
        (904, "show", faves_slug, "TV Shows", 20, 16, None),  # credited on one episode
        (905, "show", faves_slug, "TV Shows", 20, 15, None),
        (906, "show", faves_slug, "TV Shows", 20, 14, 3),  # series seen out
    ]
    with sqlite3.connect(db) as con:
        uid = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
        for tmdb, media, slug, library, d_ago, w_ago, f_ago in rows:
            con.execute(
                "INSERT INTO picks (user_id, tmdb_id, media_type, rating_key, rank, collection_slug, "
                "section_key, library, title, reason, sources, affinity, created_at, watched_at, finished_at) "
                "VALUES (?,?,?,?,1,?,?,?,?,'','',1.0,?,?,?)",
                (
                    uid,
                    tmdb,
                    media,
                    tmdb,
                    slug,
                    "1" if media == "movie" else "2",
                    library,
                    f"Title {tmdb}",
                    (now - timedelta(days=d_ago)).strftime("%Y-%m-%d %H:%M:%S"),
                    None if w_ago is None else (now - timedelta(days=w_ago)).strftime("%Y-%m-%d %H:%M:%S"),
                    None if f_ago is None else (now - timedelta(days=f_ago)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        con.commit()


class TestTheDashboardShowsBothNumbers:
    def test_the_finished_tile_renders_beside_watched(self, page: Page, app: ShortlistApp):
        seed_outcomes(app)
        page.goto("/")

        expect(page.get_by_text("Watched", exact=True)).to_be_visible(timeout=20_000)
        expect(page.get_by_text("Finished", exact=True)).to_be_visible()

    def test_the_api_and_the_page_agree_on_the_split(self, page: Page, app: ShortlistApp):
        """5 watched (2 films + 3 series), 3 finished (2 films + 1 series seen out)."""
        seed_outcomes(app)

        overall = app.api("GET", "/api/report?window=30").json()["overall"]

        assert overall["watched"] == 5, overall
        assert overall["finished"] == 3, overall

        page.goto("/")
        expect(page.get_by_text("Finished", exact=True)).to_be_visible(timeout=20_000)
        tile = page.get_by_text("Finished", exact=True).locator("xpath=..")
        expect(tile).to_contain_text("3")

    def test_each_row_line_carries_its_own_finished_count(self, page: Page, app: ShortlistApp):
        """The whole point, on one screen: the movie row finished everything it landed, the TV row
        finished one of three. Before the split these two lines were indistinguishable."""
        seed_outcomes(app)
        page.goto("/")
        expect(page.get_by_text("Watched", exact=True)).to_be_visible(timeout=20_000)

        body = page.locator("body")
        expect(body).to_contain_text(re.compile(r"2 watched · 2 finished"))
        expect(body).to_contain_text(re.compile(r"3 watched · 1 finished"))

    def test_no_console_errors_with_the_split_rendered(self, page: Page, app: ShortlistApp):
        seed_outcomes(app)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto("/")
        page.get_by_text("Finished", exact=True).wait_for(timeout=20_000)
        page.wait_for_timeout(1500)

        assert not errors, errors


class TestTheRealSyncStampsTheRightColumn:
    """The full path, not a stub: a real watch-history sync reads the PMS as each person, caches
    their episode counts, and `reconcile_watched` decides which column each pick earns.

    Everything above this class seeds `finished_at` directly, which proves the REPORTING. Only this
    proves the thing that writes it — and it is the half that talks to Plex.
    """

    def _sync(self, app: ShortlistApp) -> None:
        started = app.api("POST", "/api/report/sync")
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        # Polled from the jobs table rather than an endpoint: there is no `/api/jobs/{id}` route
        # (only the `/api/system/jobs` list), and the status column is what the worker writes anyway.
        for _ in range(300):
            with sqlite3.connect(app.config_dir / "shortlist.db") as con:
                row = con.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row and row[0] in ("done", "error"):
                assert row[0] == "done", row
                return
            time.sleep(0.2)
        raise AssertionError("the sync job never finished")

    def _pick(self, app: ShortlistApp, uid: int, tmdb_id: int, media_type: str, title: str) -> None:
        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            con.execute(
                "INSERT INTO picks (user_id, tmdb_id, media_type, rating_key, rank, collection_slug, "
                "section_key, library, title, reason, sources, affinity, created_at, watched_at, finished_at) "
                "VALUES (?,?,?,?,1,'picked',?,?,?,'','tmdb',1.0,?,NULL,NULL)",
                (
                    uid,
                    tmdb_id,
                    media_type,
                    tmdb_id,
                    "2" if media_type == "show" else "1",
                    "TV Shows" if media_type == "show" else "Movies",
                    title,
                    (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            con.commit()

    def _stamps(self, app: ShortlistApp, title: str) -> tuple:
        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            return con.execute("SELECT watched_at, finished_at FROM picks WHERE title = ?", (title,)).fetchone()

    def test_a_finished_series_and_a_part_watched_one_land_in_different_columns(
        self, page: Page, app: ShortlistApp, reset_fake_plex
    ):
        """The distinction, end to end. Both shows are 'watched' as far as Plex's own flag goes —
        `unwatched=0` returns a series from its first episode — so only the episode counts separate
        them, and only this test drives the code that reads them."""
        state = reset_fake_plex
        sarah = state.users[201]
        # Show 301 = tmdb 7001, show 302 = tmdb 7002, both 10 episodes.
        state.history.append(FakeHistoryEntry(account_id=sarah.id, rating_key=301, viewed_at=int(time.time())))
        state.history.append(FakeHistoryEntry(account_id=sarah.id, rating_key=302, viewed_at=int(time.time())))
        state.watch_episodes(sarah.id, 302, 2)  # two episodes in, 8 to go

        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            uid = con.execute("SELECT id FROM users WHERE username = 'sarah'").fetchone()[0]
        self._pick(app, uid, 7001, "show", "Seen out")
        self._pick(app, uid, 7002, "show", "Two episodes in")

        self._sync(app)

        seen_out = self._stamps(app, "Seen out")
        partial = self._stamps(app, "Two episodes in")

        assert seen_out[0] is not None, "a finished series must be credited as watched"
        assert seen_out[1] is not None, "a finished series must be stamped finished"
        assert partial[0] is not None, "2 of 10 episodes IS a watch — Plex says so and so do we"
        assert partial[1] is None, "2 of 10 episodes is NOT finished — this is the whole feature"

    def test_a_watched_film_gets_both_columns_from_the_same_event(self, page: Page, app: ShortlistApp, reset_fake_plex):
        state = reset_fake_plex
        sarah = state.users[201]
        state.history.append(FakeHistoryEntry(account_id=sarah.id, rating_key=101, viewed_at=int(time.time())))

        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            uid = con.execute("SELECT id FROM users WHERE username = 'sarah'").fetchone()[0]
        self._pick(app, uid, 9001, "movie", "A film")

        self._sync(app)

        watched, finished = self._stamps(app, "A film")
        assert watched is not None and finished is not None
        assert watched == finished, "a film has no middle state — one event, both columns"


class TestTheSplitBarsAreHonest:
    def test_no_finished_segment_is_wider_than_the_bar_it_sits_in(self, page: Page, app: ShortlistApp):
        """A segment overflowing its track is what a windowing bug looks like on screen: the numbers
        read as nonsense ("0 watched · 1 finished") and the bar silently clips instead of erroring."""
        seed_outcomes(app)
        page.goto("/")
        expect(page.get_by_text("Watched", exact=True)).to_be_visible(timeout=20_000)

        # Every split track on the page: children must fit inside their parent. The count is
        # asserted first because these tracks are `hidden xl:flex` — at any viewport below 1280 they
        # have zero width, get filtered out below, and the overflow check passes measuring nothing.
        assert page.evaluate("() => document.querySelectorAll('div.rounded-full.bg-muted').length") > 0, (
            "no split track rendered at this viewport — the assertion below would be vacuous"
        )
        overflows = page.evaluate(
            """() => {
                const bad = [];
                for (const track of document.querySelectorAll('div.rounded-full.bg-muted')) {
                    const inner = [...track.children].reduce((s, c) => s + c.getBoundingClientRect().width, 0);
                    const outer = track.getBoundingClientRect().width;
                    if (outer > 0 && inner > outer + 1) bad.push({inner, outer, html: track.outerHTML.slice(0, 120)});
                }
                return bad;
            }"""
        )
        assert not overflows, overflows

    def test_the_row_panels_library_bars_never_overflow_their_track(self, page: Page, app: ShortlistApp):
        """The dashboard assertions only ever visit `/`, so the row editor's per-library bars — built
        by different code with their own rounding — were covered by nothing in a browser.

        Two independently-rounded widths can sum to 101% when the split lands on .5, which overflows
        the track they are drawn inside; the second is derived from the first to prevent it.

        Seeded on purpose rather than reusing `seed_outcomes`: those bars only render for a row with
        MORE THAN ONE library whose picks are old enough to have matured (>30 days), and the first
        version of this test navigated to a page where neither was true and skipped itself.
        """
        now = datetime.now(UTC)
        rows = [
            # Split lands on .5 of the track — the exact input that rounds to 101%.
            ("Movies", "movie", "1", 8, 4, 4),
            ("TV Shows", "show", "2", 8, 4, 1),
        ]
        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            uid = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
            tmdb = 4000
            for library, media, section, delivered, watched, finished in rows:
                for i in range(delivered):
                    tmdb += 1
                    con.execute(
                        "INSERT INTO picks (user_id, tmdb_id, media_type, rating_key, rank, collection_slug, "
                        "section_key, library, title, reason, sources, affinity, created_at, watched_at, finished_at) "
                        "VALUES (?,?,?,?,1,'picked',?,?,?,'','tmdb',1.0,?,?,?)",
                        (
                            uid,
                            tmdb,
                            media,
                            tmdb,
                            section,
                            library,
                            f"{library} {tmdb}",
                            (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),
                            (now - timedelta(days=55)).strftime("%Y-%m-%d %H:%M:%S") if i < watched else None,
                            (now - timedelta(days=50)).strftime("%Y-%m-%d %H:%M:%S") if i < finished else None,
                        ),
                    )
            con.commit()

        default_row = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")
        panel = app.api("GET", f"/api/collections/{default_row['id']}/effectiveness").json()
        assert len(panel["per_library"]) > 1, f"the bars only render for >1 library: {panel['per_library']}"

        page.goto(f"/rows/{default_row['id']}")
        expect(page.get_by_text("Movies", exact=True).first).to_be_visible(timeout=20_000)

        measured = page.evaluate(
            """() => {
                const bars = [];
                for (const track of document.querySelectorAll('div.rounded-full.bg-muted')) {
                    const inner = [...track.children].reduce((s, c) => s + c.getBoundingClientRect().width, 0);
                    const outer = track.getBoundingClientRect().width;
                    if (outer > 0) bars.push({inner: Math.round(inner), outer: Math.round(outer)});
                }
                return bars;
            }"""
        )
        assert measured, "no per-library bar rendered — the assertion below would be vacuous"
        assert [b for b in measured if b["inner"] > b["outer"] + 1] == [], measured

    def test_no_trend_column_has_a_finished_segment_taller_than_itself(self, page: Page, app: ShortlistApp):
        """`seed_outcomes` alone puts every watch in ONE week, and the chart collapses to a single
        number below three weeks — so this assertion used to run against zero columns and could not
        fail. Spread the watches across four weeks, then refuse to pass if nothing rendered."""
        seed_outcomes(app)
        now = datetime.now(UTC)
        with sqlite3.connect(app.config_dir / "shortlist.db") as con:
            uid = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()[0]
            for n, weeks_back in enumerate((2, 3, 4, 5)):
                when = now - timedelta(weeks=weeks_back)
                con.execute(
                    "INSERT INTO picks (user_id, tmdb_id, media_type, rating_key, rank, collection_slug, "
                    "section_key, library, title, reason, sources, affinity, created_at, watched_at, finished_at) "
                    "VALUES (?,?, 'movie', ?, 1, 'picked', '1', 'Movies', ?, '', 'tmdb', 1.0, ?, ?, ?)",
                    (
                        uid,
                        7100 + n,
                        7100 + n,
                        f"Week {n}",
                        (when - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                        when.strftime("%Y-%m-%d %H:%M:%S"),
                        when.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            con.commit()

        page.goto("/")
        expect(page.get_by_text("Watched", exact=True)).to_be_visible(timeout=20_000)
        page.wait_for_timeout(400)
        assert page.locator('[data-testid="trend-week"]').count() >= 3, (
            "the chart collapsed to its under-three-weeks form, so the check below measures nothing"
        )

        overflows = page.evaluate(
            """() => {
                const bad = [];
                for (const col of document.querySelectorAll('[data-testid="trend-week"]')) {
                    const inner = [...col.children].reduce((s, c) => s + c.getBoundingClientRect().height, 0);
                    const outer = col.getBoundingClientRect().height;
                    if (outer > 0 && inner > outer + 1) bad.push({inner, outer});
                }
                return bad;
            }"""
        )
        assert not overflows, overflows


class TestItStillFitsAPhone:
    def test_the_dashboard_does_not_scroll_sideways_with_both_counts(self, page: Page, app: ShortlistApp):
        """The per-row line was already the tightest thing on this page (the mobile audit measures it
        overflowing at 320px). Adding "· N finished" to it is exactly the change that could push the
        390px floor over, and that is invisible on a laptop."""
        seed_outcomes(app)
        page.set_viewport_size(PHONE)
        page.goto("/")
        expect(page.get_by_text("Watched", exact=True)).to_be_visible(timeout=20_000)
        page.wait_for_timeout(500)

        widths = page.evaluate(
            """() => {
                const doc = document.documentElement;
                const offenders = [];
                for (const el of document.querySelectorAll('body *')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.right > doc.clientWidth + 1) {
                        offenders.push({
                            tag: el.tagName,
                            cls: (el.className || '').toString().slice(0, 70),
                            text: el.textContent.trim().slice(0, 45),
                            right: Math.round(r.right),
                        });
                    }
                }
                return {scroll: doc.scrollWidth, client: doc.clientWidth, offenders: offenders.slice(0, 5)};
            }"""
        )
        assert widths["scroll"] <= widths["client"] + 1, (
            f"the dashboard scrolls sideways at {PHONE['width']}px: "
            f"content is {widths['scroll']}px in a {widths['client']}px viewport. "
            f"Offenders: {widths['offenders']}"
        )

    def test_the_by_row_line_fits_its_card_on_a_narrow_laptop(self, page: Page, app: ShortlistApp):
        """1024px is where this broke, and neither 390 nor 1440 could see it.

        The two breakdown cards go side by side from `lg` (1024), so each is ~400px — and the By-row
        line is the widest thing on the page (name + library badge + two counts + bar). With the bar
        shown from `sm` it overflowed its own card and clipped off-screen. Found by screenshot, not
        by assertion, which is why this test now exists at this exact width.
        """
        seed_outcomes(app)
        page.set_viewport_size({"width": 1024, "height": 1000})
        page.goto("/")
        expect(page.get_by_text("By row", exact=True)).to_be_visible(timeout=20_000)
        page.wait_for_timeout(400)

        overflow = page.evaluate(
            """() => {
                const heading = [...document.querySelectorAll('*')]
                    .find(e => e.textContent.trim() === 'By row' && e.children.length === 0);
                const card = heading.closest('div.rounded-xl, div.rounded-lg, section') || heading.parentElement;
                const box = card.getBoundingClientRect();
                const bad = [];
                for (const el of card.querySelectorAll('*')) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.right > box.right + 1) {
                        bad.push({text: el.textContent.slice(0, 40), right: r.right, cardRight: box.right});
                    }
                }
                return bad;
            }"""
        )
        assert not overflow, f"By row content spills out of its card at 1024px: {overflow[:3]}"

        widths = page.evaluate(
            "() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth})"
        )
        assert widths["scroll"] <= widths["client"] + 1, widths

    def test_the_headline_tiles_do_not_overflow_on_a_phone(self, page: Page, app: ShortlistApp):
        """Five tiles where there were four. On a phone they wrap to a 2-column grid — this proves
        they wrap rather than squeezing into an unreadable row."""
        seed_outcomes(app)
        page.set_viewport_size(PHONE)
        page.goto("/")
        expect(page.get_by_text("Finished", exact=True)).to_be_visible(timeout=20_000)

        overflow = page.evaluate(
            """() => {
                const el = document.evaluate("//*[text()='Finished']", document, null, 9, null).singleNodeValue;
                const tile = el.closest('div.rounded-lg');
                const r = tile.getBoundingClientRect();
                return {right: r.right, width: r.width, viewport: document.documentElement.clientWidth};
            }"""
        )
        assert overflow["right"] <= overflow["viewport"] + 1, overflow
        assert overflow["width"] > 80, f"the Finished tile squeezed to {overflow['width']}px"
