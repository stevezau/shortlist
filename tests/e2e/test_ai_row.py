"""E2E: an AI row, from the template gallery to a practice run for one person (#138).

Full stack: the built SPA and the real API, the fake PMS and TMDB. Only the AI provider is faked, at the
`make_curator` boundary, so the one call "Build the list" makes is seen and counted. The row is added, its list
previewed and saved, and "Try it" runs it as a dry run for one person: the picks and the reasons come back, and
nothing reaches Plex.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

LOAD = 20_000
RUN = 90_000
BRIEF = "Tense thrillers with a slow-burn standoff"
#: Films the demo library holds and sarah has not watched (she has seen rating keys 101-108).
TITLES = {
    "Heat": (1995, "A slow-burn standoff across a diner table"),
    "Prisoners": (2013, "A father's patience runs out"),
    "Nightcrawler": (2014, "Tension that never lets go"),
}


class _FakeCurator:
    """The AI provider: answers "Build the list" with canned JSON and counts every call."""

    name = "anthropic"
    can_complete = True
    last_tokens = 321

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return json.dumps(
            {
                "name": "Tense Thrillers",
                "emoji": "🎯",
                "rules": {"max_runtime": None},
                "tags": [],
                "genres": [],
                "titles": [
                    {"title": title, "year": year, "media": "movie", "reason": reason}
                    for title, (year, reason) in TITLES.items()
                ],
            }
        )


@pytest.fixture
def curator(app: ShortlistApp, monkeypatch) -> _FakeCurator:
    fake = _FakeCurator()
    monkeypatch.setattr("shortlist.server.api.themes.make_curator", lambda provider, **kwargs: fake)
    monkeypatch.setattr("shortlist.server.services.context_builder.make_curator", lambda provider, **kwargs: fake)
    # The settings the API reads to know an AI provider is set; the key is never used (the curator is faked).
    app.api("PUT", "/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-fake"}})
    return fake


def _plex_writes(state: FakePlexState) -> tuple:
    """Everything a run could change on the fake server: its collections and each account's share filters."""
    return (
        copy.deepcopy(state.collections),
        {account: dict(user.filters) for account, user in state.users.items()},
    )


def test_an_ai_row_is_added_from_the_gallery_and_tried_for_one_person_with_nothing_written_to_plex(
    page: Page, app: ShortlistApp, curator: _FakeCurator, reset_fake_plex: FakePlexState
):
    # 1. The AI template, from the gallery, opens an editor that wants a list before it can be added.
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Add a row").click()
    page.get_by_role("group", name="Templates", exact=True).get_by_role(
        "button", name=re.compile(r"^.*Describe a row")
    ).click()
    page.get_by_role("button", name="Use template").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible(timeout=LOAD)
    expect(page.get_by_role("button", name="Add row")).to_be_disabled()

    # 2. One AI call writes the list; every title is checked against TMDB and the library, and shown with its reason.
    page.get_by_label("Describe it").fill(BRIEF)
    page.get_by_role("button", name="Build the list").click()
    card = page.get_by_role("region", name="The list")
    expect(card).to_contain_text("Tense Thrillers", timeout=RUN)
    for title, (_year, reason) in TITLES.items():
        expect(card).to_contain_text(title)
        expect(card).to_contain_text(reason)
    expect(card.get_by_text("Not saved yet")).to_be_visible()
    assert len(curator.calls) == 1
    assert BRIEF in curator.calls[0][1]
    assert not any(c.get("theme_id") for c in app.api("GET", "/api/collections").json()), "a preview saves nothing"

    # 3. Adding the row saves the list and the row, switched off.
    page.get_by_role("button", name="Add row").click()
    expect(page).to_have_url(re.compile(r"/rows$"), timeout=LOAD)
    row = next(c for c in app.api("GET", "/api/collections").json() if c.get("theme_id"))
    assert row["enabled"] is False
    theme = app.api("GET", f"/api/themes/{row['theme_id']}").json()
    assert theme["brief"] == BRIEF and theme["origin"] == "ai"
    assert [pick["title"] for pick in theme["picks"]] == list(TITLES)
    assert len(curator.calls) == 1, "saving never asks the AI again"

    # 4. Try it for sarah: a dry run of this row alone.
    before = _plex_writes(reset_fake_plex)
    page.goto(f"/rows/{row['id']}")
    expect(page.get_by_label("Try it for")).to_be_visible(timeout=LOAD)
    page.get_by_label("Try it for").select_option(label="sarah")
    page.get_by_role("button", name=re.compile(r"^Try it$")).click()

    result = page.get_by_text("What sarah would get").locator("..")
    expect(result).to_contain_text("Heat", timeout=RUN)
    for title, (_year, reason) in TITLES.items():
        expect(result).to_contain_text(title)
        expect(result).to_contain_text(reason)
    expect(result).to_contain_text("Fits Tense Thrillers")

    # 5. It was a dry run: recorded as one, the picks live on the run, and neither Plex nor the AI was touched.
    runs = app.api("GET", "/api/runs").json()
    latest = runs[0] if isinstance(runs, list) else runs["runs"][0]
    assert latest["dry_run"] is True
    with_picks = app.api("GET", f"/api/runs/{latest['id']}").json()
    sarah = next(u for u in with_picks["users"] if u["slug"] == "sarah")
    assert {pick["title"] for pick in sarah["picks"]} == set(TITLES)
    assert _plex_writes(reset_fake_plex) == before
    assert len(curator.calls) == 1, "Try it never asks the AI"
