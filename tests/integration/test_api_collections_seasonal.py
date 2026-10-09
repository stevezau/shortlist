"""Seasonal rows and the server's own clock for 'shown today'."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.settings_store import SettingsStore
from tests.integration.api_collections_support import _plex_jobs

pytestmark = pytest.mark.integration


def stored_setting(client: TestClient, key: str):
    with client.app.state.sessions() as session:
        return SettingsStore(session, client.app.state.secrets).get(key)


class TestSeasonalRowsApi:
    """A row's seasons (discussion #124): stored, validated, and resolved on the SERVER's clock."""

    @staticmethod
    def _at(day):
        """Freeze the server's clock for the duration of a `with` block."""
        import contextlib
        from datetime import datetime

        import shortlist.server.services.context_builder as cb

        @contextlib.contextmanager
        def frozen():
            original = cb.local_now
            cb.local_now = lambda: datetime(day.year, day.month, day.day, 12, 0)
            try:
                yield
            finally:
                cb.local_now = original

        return frozen()

    def _spec(self, client: TestClient, slug: str):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        return next(s for s in specs if s.slug == slug)

    def test_seasons_round_trip_in_calendar_order(self, client: TestClient):
        created = client.post(
            "/api/collections",
            json={"name": "Seasonal", "seasons": ["christmas", "valentines", "christmas"], "season_lead_days": 14},
        )

        assert created.status_code == 201
        body = created.json()
        assert body["seasons"] == ["valentines", "christmas"]
        assert (body["season_lead_days"], body["season_after_days"]) == (14, 0)

    def test_a_row_is_not_seasonal_by_default(self, client: TestClient):
        body = client.post("/api/collections", json={"name": "Plain"}).json()
        assert body["seasons"] == []
        assert body["season_status"] is None

    def test_a_patch_refuses_an_unknown_season_and_stores_its_seasons_in_calendar_order(self, client: TestClient):
        """The catalogue lives partly in the database (#137), so the check runs in the handlers rather than a
        field validator — and a PATCH has to get it as surely as a POST."""
        cid = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["halloween"]}).json()["id"]

        refused = client.patch(f"/api/collections/{cid}", json={"name": "Seasonal", "seasons": ["easter"]})
        assert refused.status_code == 422
        assert "easter" in refused.text
        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Seasonal", "seasons": ["christmas", "valentines", "christmas"]}
        )
        assert r.status_code == 200, r.text
        assert r.json()["seasons"] == ["valentines", "christmas"]

    def test_an_unknown_season_is_refused(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["easter"]})
        assert r.status_code == 422
        assert "easter" in r.text

    @pytest.mark.parametrize(
        ("field", "value"), [("season_lead_days", 91), ("season_lead_days", -1), ("season_after_days", 31)]
    )
    def test_day_counts_outside_their_range_are_refused(self, client: TestClient, field, value):
        r = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["halloween"], field: value})
        assert r.status_code == 422

    def test_a_fallback_name_cannot_use_the_season(self, client: TestClient):
        """The fallback stands in when a name cannot be filled; one that needs a season is no fallback."""
        r = client.post(
            "/api/collections",
            json={"name": "{season} for {top_seed}", "seasons": ["halloween"], "fallback_name": "{season} picks"},
        )
        assert r.status_code == 422

    def test_in_season_the_row_is_shown_and_says_until_when(self, client: TestClient):
        from datetime import date

        with self._at(date(2026, 10, 15)):
            body = client.post(
                "/api/collections", json={"name": "Seasonal", "seasons": ["halloween", "christmas"]}
            ).json()

        assert body["shown_today"] is True
        assert body["season_status"]["showing"] == {
            "slug": "halloween",
            "name": "Halloween",
            "emoji": "🎃",
            "starts": "2026-10-01",
            "ends": "2026-10-31",
        }

    def test_until_is_the_day_the_next_season_takes_over_when_windows_overlap(self, client: TestClient):
        """With a long "keep it up", Halloween's window runs into November, but Christmas takes the row on
        1 Nov — "showing until 30 Nov" would be a date the row never reaches."""
        from datetime import date

        with self._at(date(2026, 10, 15)):
            body = client.post(
                "/api/collections",
                json={
                    "name": "Seasonal",
                    "seasons": ["halloween", "christmas"],
                    "season_lead_days": 60,
                    "season_after_days": 30,
                },
            ).json()

        assert body["season_status"]["showing"]["ends"] == "2026-10-31"
        assert body["season_status"]["next"]["slug"] == "christmas"

    def test_out_of_season_the_row_is_hidden_and_says_what_it_waits_for(self, client: TestClient):
        from datetime import date

        with self._at(date(2026, 11, 10)):
            body = client.post(
                "/api/collections", json={"name": "Seasonal", "seasons": ["halloween", "christmas"]}
            ).json()

        assert body["shown_today"] is False
        assert body["season_status"]["showing"] is None
        assert body["season_status"]["next"]["starts"] == "2026-11-25"

    def test_changing_the_seasons_is_applied_now(self, client: TestClient, monkeypatch):
        """Make a row seasonal in September and it has to come off people's Home now, not at midnight."""
        cid = client.post("/api/collections", json={"name": "Seasonal"}).json()["id"]

        client.patch(f"/api/collections/{cid}", json={"name": "Seasonal", "seasons": ["christmas"]})

        assert [(j["kind"], j["payload"]) for j in _plex_jobs(client)] == [
            ("rows.visibility", {"row": "seasonal", "dry_run": False})
        ]

    def _default_row(self, client: TestClient) -> dict:
        return next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")

    def test_the_default_row_cannot_be_renamed_after_a_season(self, client: TestClient):
        """Its title is the global template, which names a row that follows no season — the placeholder would
        never fill and the default row would stop being built for everyone."""
        default = self._default_row(client)
        r = client.patch(f"/api/collections/{default['id']}", json={"name": "{season} picks"})
        assert r.status_code == 422, r.text
        assert stored_setting(client, "row.name_template") != "{season} picks"

    def test_the_default_row_cannot_follow_seasons(self, client: TestClient):
        """A seasonal row is its own row: the default is everyone's everyday row and has no season name to wear."""
        default = self._default_row(client)
        r = client.patch(f"/api/collections/{default['id']}", json={"name": default["name"], "seasons": ["christmas"]})
        assert r.status_code == 422, r.text

    def test_the_rename_endpoint_refuses_a_season_name_on_a_row_with_no_seasons(self, client: TestClient):
        cid = client.post("/api/collections", json={"name": "Plain"}).json()["id"]
        r = client.post(f"/api/collections/{cid}/rename", json={"name_template": "{season} picks"})
        assert r.status_code == 422, r.text

    def test_the_rename_endpoint_refuses_a_season_name_for_the_default_row(self, client: TestClient):
        default = self._default_row(client)
        r = client.post(f"/api/collections/{default['id']}/rename", json={"name_template": "{season} picks"})
        assert r.status_code == 422, r.text
        assert stored_setting(client, "row.name_template") != "{season} picks"

    def test_the_rename_endpoint_accepts_a_season_name_on_a_seasonal_row(self, client: TestClient, monkeypatch):
        from shortlist.server.services import collection_reconcile

        monkeypatch.setattr(collection_reconcile, "reconcile_row_rename_iter", lambda state, **kw: iter(()))
        cid = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"]}).json()["id"]
        r = client.post(f"/api/collections/{cid}/rename", json={"name_template": "{season_emoji} {season} picks"})
        assert r.status_code == 200, r.text
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.get(Collection, cid).name_template == "{season_emoji} {season} picks"

    def test_the_badge_and_the_status_describe_the_same_day_across_midnight(self, client: TestClient, monkeypatch):
        """Both were read off the clock separately, so a response built across midnight on the last day of a
        season could say "showing today" beside a status with nothing showing."""
        from datetime import datetime

        from shortlist.server.services import context_builder as cb

        ticks = iter([datetime(2026, 12, 25, 23, 59, 59), datetime(2026, 12, 26, 0, 0, 0)])
        monkeypatch.setattr(cb, "local_now", lambda: next(ticks, datetime(2026, 12, 26, 0, 0, 1)))

        body = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"]}).json()

        assert body["shown_today"] is (body["season_status"]["showing"] is not None)

    def test_changing_how_early_it_shows_is_applied_now(self, client: TestClient, monkeypatch):
        cid = client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"]}).json()["id"]

        client.patch(f"/api/collections/{cid}", json={"name": "Seasonal", "season_lead_days": 60})

        assert [(j["kind"], j["payload"]) for j in _plex_jobs(client)] == [
            ("rows.visibility", {"row": "seasonal", "dry_run": False})
        ]

    def test_a_row_that_follows_no_season_cannot_be_named_after_one(self, client: TestClient):
        """Such a name can never be filled in, so the row would never be built for anyone."""
        r = client.post("/api/collections", json={"name": "{season} picks"})
        assert r.status_code == 422
        assert "{season}" in r.text

    def test_switching_the_seasons_off_under_a_seasonal_name_is_refused(self, client: TestClient):
        cid = client.post("/api/collections", json={"name": "{season} picks", "seasons": ["christmas"]}).json()["id"]
        r = client.patch(f"/api/collections/{cid}", json={"name": "{season} picks", "seasons": []})
        assert r.status_code == 422

    def test_renaming_a_seasonal_row_without_resending_its_seasons_is_fine(self, client: TestClient):
        """A PATCH moves only what it sends — the row still follows its seasons."""
        cid = client.post("/api/collections", json={"name": "{season} picks", "seasons": ["christmas"]}).json()["id"]
        r = client.patch(f"/api/collections/{cid}", json={"name": "{season} picks", "description": "For the season"})
        assert r.status_code == 200, r.text

    def test_two_seasonal_rows_with_different_names_do_not_clash(self, client: TestClient):
        """Both names render to nothing without a season; compared that way every seasonal row would clash."""
        first = client.post("/api/collections", json={"name": "{season} picks", "seasons": ["christmas"]})
        second = client.post("/api/collections", json={"name": "{season} for you", "seasons": ["halloween"]})
        assert (first.status_code, second.status_code) == (201, 201)

    def test_two_seasonal_rows_with_the_same_name_do_clash(self, client: TestClient):
        client.post("/api/collections", json={"name": "{season} picks", "seasons": ["christmas"], "media": "movie"})
        again = client.post(
            "/api/collections", json={"name": "{season} picks", "seasons": ["christmas"], "media": "movie"}
        )
        assert again.status_code == 409 or again.status_code == 422

    def test_a_seasonal_name_clashes_with_a_plain_row_titled_like_one_of_its_seasons(self, client: TestClient):
        """In December `{season} picks` IS "Christmas picks", so the two would share one collection per person
        per library — the #121 trap, reached through the calendar."""
        plain = client.post("/api/collections", json={"name": "Christmas picks", "media": "movie"})
        seasonal = client.post(
            "/api/collections", json={"name": "{season} picks", "seasons": ["halloween"], "media": "movie"}
        )
        assert plain.status_code == 201
        assert seasonal.status_code == 422, seasonal.text

    def test_a_plain_row_titled_like_a_season_clashes_with_an_existing_seasonal_row(self, client: TestClient):
        client.post("/api/collections", json={"name": "{season} picks", "seasons": ["christmas"], "media": "movie"})
        plain = client.post("/api/collections", json={"name": "Christmas picks", "media": "movie"})
        assert plain.status_code == 422, plain.text

    def test_the_spec_builds_tonights_season_and_shows_it(self, client: TestClient):
        from datetime import date

        with self._at(date(2026, 12, 1)):
            client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"], "placement": "home"})
            spec = self._spec(client, "seasonal")

        assert spec.seasons == ["christmas"]
        assert (spec.season.slug, spec.season.name, spec.season.emoji) == ("christmas", "Christmas", "🎄")
        assert spec.season.anchor == date(2026, 12, 25)
        assert spec.placement == "home"

    def test_the_night_before_its_season_the_spec_builds_it_hidden(self, client: TestClient):
        from datetime import date

        with self._at(date(2026, 11, 24)):
            client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"]})
            spec = self._spec(client, "seasonal")

        assert spec.season is not None and spec.season.slug == "christmas"
        assert (spec.placement, spec.placement_friends) == ("off", "off")

    def test_between_seasons_the_spec_is_dormant_and_hidden(self, client: TestClient):
        from datetime import date

        with self._at(date(2026, 11, 10)):
            client.post("/api/collections", json={"name": "Seasonal", "seasons": ["christmas"]})
            spec = self._spec(client, "seasonal")

        assert spec.dormant is True
        assert (spec.placement, spec.placement_friends) == ("off", "off")


class TestShownTodayComesFromTheServer:
    """The Rows page badge must not be computed in the browser (issue #102).

    Days turn over on the SERVER's clock — that is the clock the midnight job and Plex follow. A badge
    derived from `new Date()` in the admin's browser can read "Hidden today" for a row Plex is
    showing, for as long as the two timezones differ.
    """

    def test_a_row_carries_whether_it_is_shown_today(self, client: TestClient):
        from datetime import datetime

        import shortlist.server.services.context_builder as cb

        monday = datetime(2026, 8, 31, 12, 0)
        original = cb.local_now
        cb.local_now = lambda: monday
        try:
            on = client.post("/api/collections", json={"name": "Mondays", "show_days": [1]}).json()
            off = client.post("/api/collections", json={"name": "Tuesdays", "show_days": [2]}).json()
            always = client.post("/api/collections", json={"name": "Whenever"}).json()
        finally:
            cb.local_now = original

        assert on["shown_today"] is True
        assert off["shown_today"] is False
        assert always["shown_today"] is True, "a row with no schedule is shown every day"
