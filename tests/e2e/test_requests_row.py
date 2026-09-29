"""E2E: the "Your requests" row (issue #127), through the real SPA against fake Plex, Overseerr and the Arrs.

The row is made in the UI from its template tile, its sources are checked from the editor, a run
builds it, and the result is read back from three places: the fake PMS (what landed on Plex and for
whom), the run's trace ("How we picked"), and the Users page's Requests column.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from playwright.sync_api import Page, expect

from shortlist.engine.delivery import strip_marker
from tests.e2e.conftest import ShortlistApp, _free_port, _ThreadedServer
from tests.fakes.fake_arr import (
    ARR_API_KEY,
    REQUESTED_MOVIE_KEY,
    REQUESTED_SHOW_KEY,
    REQUESTER_PLEX_ID,
    SEERR_API_KEY,
    make_fake_arr,
    make_fake_seerr,
)
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

LOAD = 20_000
#: The setup check reads Overseerr and both Arrs in turn before it answers.
CHECK = 30_000


@dataclass(frozen=True)
class RequestSourceUrls:
    overseerr: str
    radarr: str
    sonarr: str


@pytest.fixture(scope="module")
def fake_request_sources(fake_plex) -> Iterator[RequestSourceUrls]:
    """Fake Overseerr, Radarr and Sonarr, booted once for the module beside the fake Plex."""
    _, _, state = fake_plex
    servers = {
        "overseerr": _ThreadedServer(make_fake_seerr(state), _free_port()),
        "radarr": _ThreadedServer(make_fake_arr("radarr", state), _free_port()),
        "sonarr": _ThreadedServer(make_fake_arr("sonarr", state), _free_port()),
    }
    for server in servers.values():
        server.start()
    servers["overseerr"].wait_until_up("/api/v1/status")
    servers["radarr"].wait_until_up("/api/v3/system/status")
    servers["sonarr"].wait_until_up("/api/v3/system/status")
    yield RequestSourceUrls(**{name: f"http://127.0.0.1:{server.port}" for name, server in servers.items()})
    for server in servers.values():
        server.stop()


@pytest.fixture
def sourced_app(app: ShortlistApp, fake_request_sources: RequestSourceUrls) -> ShortlistApp:
    """The app with every request source's URL and key saved — what makes the row buildable."""
    response = app.api(
        "PUT",
        "/api/settings",
        json={
            "values": {
                "requests.overseerr.url": fake_request_sources.overseerr,
                "requests.overseerr.apikey": SEERR_API_KEY,
                "requests.radarr.url": fake_request_sources.radarr,
                "requests.radarr.apikey": ARR_API_KEY,
                "requests.sonarr.url": fake_request_sources.sonarr,
                "requests.sonarr.apikey": ARR_API_KEY,
            }
        },
    )
    assert response.status_code == 200, response.text
    return app


def _open_add_a_row(page: Page) -> None:
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Add a row").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible()


def _users_by_name(app: ShortlistApp) -> dict[str, dict]:
    return {user["username"]: user for user in app.api("GET", "/api/users").json()}


def _rows_labelled(state: FakePlexState, label: str) -> list:
    return [c for c in state.collections.values() if label in [lbl.lower() for lbl in c.labels]]


def test_a_requests_row_reaches_only_the_person_who_asked_when_built_from_the_template(
    page: Page, sourced_app: ShortlistApp, reset_fake_plex: FakePlexState, fake_request_sources: RequestSourceUrls
):
    app, state = sourced_app, reset_fake_plex
    movie = state.movies[REQUESTED_MOVIE_KEY]
    show = state.shows[REQUESTED_SHOW_KEY]

    # (a) The owner adds the row from its template tile and checks its sources from the editor.
    _open_add_a_row(page)
    page.get_by_role("button", name="Your requests").click()
    page.get_by_role("button", name="Use template").click()
    page.locator('details[data-settings-group="Row settings"] > summary').click()
    expect(page.get_by_text("Which requests show up")).to_be_visible(timeout=LOAD)
    # The Check button lives under the collapsed "Use my own tags" disclosure.
    page.get_by_text("Use my own tags").click()
    page.get_by_role("button", name="Check", exact=True).click()
    expect(page.get_by_text("Checking…")).to_have_count(0, timeout=CHECK)
    sources_panel = page.locator("[data-setting='requests_sources']")
    expect(sources_panel).to_contain_text("Overseerr", timeout=CHECK)
    expect(sources_panel).to_contain_text("Connected")
    expect(sources_panel).to_contain_text("1 request from 1 person, 1 linked to someone here")
    expect(sources_panel).to_contain_text("Radarr: tagging requests, 1 tagged movie")
    expect(sources_panel).to_contain_text("Sonarr: tagging requests, 1 tagged show")
    expect(sources_panel).to_contain_text(re.compile(r"People\s*1 of \d+ linked"))
    expect(sources_panel).to_contain_text("1 person with something ready on Plex")
    page.get_by_role("button", name="Add row").click()
    expect(page).to_have_url(re.compile(r"/rows$"), timeout=LOAD)
    row = next(c for c in app.api("GET", "/api/collections").json() if c["requests_row"])
    assert row["name"] == "📬 {library_name} you asked for"
    assert row["build"] == "per_person"

    # (b) A run for the requester and a bystander, scoped to this row.
    users = _users_by_name(app)
    sarah, mike = users["sarah"], users["mike"]
    assert sarah["plex_account_id"] == REQUESTER_PLEX_ID
    created = app.api(
        "POST",
        "/api/runs",
        json={"dry_run": False, "user_ids": [sarah["id"], mike["id"]], "collection_ids": [row["id"]]},
    ).json()
    run = app.wait_for_run(created["run_id"])
    assert run["status"] == "ok", run

    # (c) On Plex: sarah's rows hold exactly what she asked for; mike, who asked for nothing, has none.
    # Titles carry the invisible ownership marker on Plex; the words are what the person sees.
    sarah_rows = {strip_marker(c.title): c for c in _rows_labelled(state, "shortlist_sarah")}
    assert set(sarah_rows) == {"📬 Movies you asked for", "📬 TV Shows you asked for"}, sorted(sarah_rows)
    assert sarah_rows["📬 Movies you asked for"].item_keys == [movie.rating_key]
    assert sarah_rows["📬 TV Shows you asked for"].item_keys == [show.rating_key]
    assert _rows_labelled(state, "shortlist_mike") == []

    # (d) The trace says what was asked for and that it went in the row.
    page.goto(f"/runs/{run['id']}/trace/{sarah['id']}")
    expect(page.get_by_text("What they asked for").first).to_be_visible(timeout=LOAD)
    movie_line = page.get_by_role("row").filter(has_text=movie.title)
    expect(movie_line).to_contain_text("In the row")
    expect(movie_line).to_contain_text("Overseerr, tag")
    # The trace shows one library at a time; the show's request is under the TV tab.
    page.get_by_role("tab", name=re.compile(r"^TV Shows")).click()
    show_line = page.get_by_role("row").filter(has_text=show.title)
    expect(show_line).to_contain_text("In the row")
    expect(show_line).to_contain_text("tag")

    # (e) The Users page reads the same sources: sarah is linked, mike has no Overseerr account.
    page.goto("/users")
    for username, status in (("sarah", "Linked"), ("mike", "No account")):
        person = page.get_by_role("row").filter(has=page.get_by_role("link", name=username, exact=True))
        expect(person.get_by_text(status, exact=True)).to_be_visible(timeout=CHECK)


def test_the_your_requests_template_cannot_be_confirmed_when_no_source_is_connected(page: Page, app: ShortlistApp):
    _open_add_a_row(page)
    tile = page.get_by_role("button", name="Your requests")
    tile.click()
    expect(tile).to_have_attribute("aria-pressed", "true")

    expect(
        page.get_by_text("Needs a way to know who asked for what: an Overseerr or Jellyseerr connection")
    ).to_be_visible(timeout=CHECK)
    expect(page.get_by_role("button", name="Use template")).to_be_disabled()
    expect(page.get_by_role("link", name="Settings")).to_have_attribute("href", "/settings#connections")
