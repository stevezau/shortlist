"""Per-row request settings, show days and the requests row fields."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.db.models import User
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.settings_store import SettingsStore
from tests.integration.api_collections_support import _plex_jobs

pytestmark = pytest.mark.integration


class TestRowEffectiveness:
    """`GET /api/collections/{id}/effectiveness` — is one row working?

    The value under test is the MATURITY rule. A pick only counts as a hit if it is watched within
    `HIT_WINDOW_DAYS`, so a rate computed over picks younger than that reads as failure for no
    reason but time — and this panel sits beside the settings someone would then go and change.
    """

    def _picks(self, client: TestClient, slug: str, specs: list[tuple[int, int, bool, str]]) -> None:
        """specs = (tmdb_id, days_ago, watched, library)."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import PickRow, Run

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            for tmdb_id, days_ago, watched, library in specs:
                created = now - timedelta(days=days_ago)
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=tmdb_id,
                        media_type="movie",
                        rating_key=tmdb_id,
                        rank=1,
                        collection_slug=slug,
                        library=library,
                        title=f"T{tmdb_id}",
                        created_at=created,
                        watched_at=created if watched else None,
                    )
                )
            session.commit()

    def _row_id(self, client: TestClient, slug: str = "picked") -> int:
        return next(c["id"] for c in client.get("/api/collections").json() if c["slug"] == slug)

    def test_a_row_that_never_delivered_says_so_rather_than_scoring_zero(self, client: TestClient):
        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["first_delivered_at"] is None
        assert body["matured"] is None
        assert body["delivered"] == 0

    def test_picks_too_young_to_judge_are_counted_but_not_scored(self, client: TestClient):
        """The whole point. Five picks delivered yesterday, none watched — that is 0%, and reporting
        it would send someone to change settings that were never the problem."""
        self._picks(client, "picked", [(i, 1, False, "Movies") for i in range(1, 6)])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 5, "young picks still count towards the size"
        assert body["matured"] is None, "but they must not produce a rate"
        assert body["first_delivered_at"] is not None

    def test_a_matured_cohort_is_scored_and_excludes_the_young(self, client: TestClient):
        # Four matured (2 watched), plus two delivered yesterday that must not dilute the rate.
        self._picks(
            client,
            "picked",
            [
                (1, 60, True, "Movies"),
                (2, 60, True, "Movies"),
                (3, 60, False, "Movies"),
                (4, 60, False, "Movies"),
                (5, 1, False, "Movies"),
                (6, 1, False, "Movies"),
            ],
        )

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 6, "all time counts everything"
        assert body["matured"]["delivered"] == 4, "the score ignores picks that have not had their window"
        assert body["matured"]["watched"] == 2
        assert body["matured"]["rate"] == 0.5

    def test_each_library_is_scored_separately(self, client: TestClient):
        """A row across two libraries is two Plex collections, and the Movies half landing while the
        TV half does not is the most actionable thing this panel can say."""
        self._picks(
            client,
            "picked",
            [(1, 60, True, "Movies"), (2, 60, True, "Movies"), (3, 60, False, "TV Shows"), (4, 60, False, "TV Shows")],
        )

        by_library = {
            lib["library"]: lib
            for lib in client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()["per_library"]
        }

        assert by_library["Movies"]["rate"] == 1.0
        assert by_library["TV Shows"]["rate"] == 0.0

    def test_another_row_s_history_is_not_counted(self, client: TestClient):
        self._picks(client, "someone_else", [(1, 60, True, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["delivered"] == 0

    def test_an_unknown_row_is_a_404(self, client: TestClient):
        assert client.get("/api/collections/9999/effectiveness").status_code == 404

    def test_the_runs_count_matches_the_run_list_the_tile_links_to(self, client: TestClient):
        """The Runs tile is a LINK to `/api/runs?collection=<slug>`, so the number on it has to be
        the length of that list. A tile reading 3 above a list of 1 is worse than no tile."""
        self._picks(client, "picked", [(1, 40, True, "Movies")])
        self._picks(client, "picked", [(2, 20, False, "Movies")])
        self._picks(client, "someone_else", [(3, 10, False, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()
        listed = client.get("/api/runs", params={"collection": "picked"}).json()

        assert body["runs"] == 2, "one run per _picks call, and the other row's run is not ours"
        assert body["runs"] == len(listed), "the tile and the list it opens must agree"

    def test_the_runs_count_drops_a_run_that_has_been_pruned(self, client: TestClient):
        """`runs.retention` deletes old runs and leaves the picks behind with a null `run_id`
        (migration 0040). Both the count and the list it links to then stop claiming that run —
        the alternative is a tile that counts history nobody can open."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import Run
        from shortlist.server.services.run_persistence import prune_runs

        self._picks(client, "picked", [(1, 40, True, "Movies")])
        self._picks(client, "picked", [(2, 20, False, "Movies")])
        # Age the first run past retention and prune it the way the real job does, rather than
        # deleting the row by hand — the behaviour under test is `prune_runs` nulling `run_id`.
        with client.app.state.sessions() as session:
            oldest = session.query(Run).order_by(Run.id).first()
            oldest.started_at = datetime.now(UTC) - timedelta(days=400)
            session.commit()
            assert prune_runs(session, retention_months=1) == 1
            session.commit()

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()
        listed = client.get("/api/runs", params={"collection": "picked"}).json()

        assert body["runs"] == 1 == len(listed)
        assert body["delivered"] == 2, "the picks themselves outlive their run"

    def test_last_delivered_at_is_the_most_recent_delivery(self, client: TestClient):
        """`first_delivered_at` tells "never run" from "ran once"; this tells "ran last night" from
        "ran in March and has been idle since", which is the one a stalled row shows up in."""
        from datetime import UTC, datetime, timedelta

        self._picks(client, "picked", [(1, 60, False, "Movies"), (2, 3, False, "Movies")])

        body = client.get(f"/api/collections/{self._row_id(client)}/effectiveness").json()

        assert body["first_delivered_at"] < body["last_delivered_at"]
        assert body["last_delivered_at"].startswith((datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d"))


class TestPerRowRequestSettingsApi:
    """A row's own Sonarr/Radarr floors and target, over the API.

    NULL is the "inherit the global" signal throughout, so the round-trip that matters is that a
    value the caller never sent stays null rather than being defaulted to something concrete.
    """

    def test_a_new_row_inherits_everything_by_default(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Plain", "build": "per_person"}).json()
        assert created["req_min_rating"] is None
        assert created["req_radarr_root_folder"] is None
        assert created["req_max_per_row"] is None

    def test_overrides_round_trip(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}",
            json={
                "name": "Kids",
                "req_min_rating": 6.0,
                "req_max_per_row": 2,
                "req_radarr_root_folder": "/data/Kids",
                "req_radarr_quality_profile_id": 9,
                "req_auto_send": False,
            },
        )
        assert resp.status_code == 200, resp.text
        patched = resp.json()
        assert patched["req_min_rating"] == 6.0
        assert patched["req_max_per_row"] == 2
        assert patched["req_radarr_root_folder"] == "/data/Kids"
        assert patched["req_radarr_quality_profile_id"] == 9
        assert patched["req_auto_send"] is False
        # Untouched fields stay null — patching one override must not concrete the rest.
        assert patched["req_min_year"] is None
        assert patched["req_sonarr_root_folder"] is None

    def test_an_override_can_be_cleared_back_to_inherit(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        client.patch(f"/api/collections/{created['id']}", json={"name": "Kids", "req_min_rating": 6.0})
        cleared = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Kids", "req_min_rating": None}
        ).json()
        assert cleared["req_min_rating"] is None

    def test_false_is_stored_rather_than_read_as_unset(self, client: TestClient):
        """`auto_send=False` means "queue everything from this row", which is a real choice — it must
        not be confused with "inherit". Only null inherits."""
        created = client.post("/api/collections", json={"name": "Manual", "build": "per_person"}).json()
        patched = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Manual", "req_auto_send": False}
        ).json()
        assert patched["req_auto_send"] is False

    def test_zero_is_stored_rather_than_read_as_unset(self, client: TestClient):
        """`req_max_per_row=0` means "never auto-send from this row" — also a real choice."""
        created = client.post("/api/collections", json={"name": "Never", "build": "per_person"}).json()
        patched = client.patch(f"/api/collections/{created['id']}", json={"name": "Never", "req_max_per_row": 0}).json()
        assert patched["req_max_per_row"] == 0

    def test_a_rows_sonarr_monitor_mode_round_trips_and_clears(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        assert created["req_sonarr_monitor"] is None

        patched = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Kids", "req_sonarr_monitor": "firstSeason"}
        ).json()
        assert patched["req_sonarr_monitor"] == "firstSeason"

        cleared = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Kids", "req_sonarr_monitor": None}
        ).json()
        assert cleared["req_sonarr_monitor"] is None

    def test_a_rows_language_settings_round_trip_and_clear(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        assert (created["req_language_mode"], created["req_preferred_languages"], created["req_min_rating_other"]) == (
            None,
            None,
            None,
        )

        patched = client.patch(
            f"/api/collections/{created['id']}",
            json={
                "name": "Kids",
                "req_language_mode": "only",
                "req_preferred_languages": ["en"],
                "req_min_rating_other": 9.0,
            },
        ).json()
        assert patched["req_language_mode"] == "only"
        assert patched["req_preferred_languages"] == ["en"]
        assert patched["req_min_rating_other"] == 9.0

        cleared = client.patch(
            f"/api/collections/{created['id']}",
            json={
                "name": "Kids",
                "req_language_mode": None,
                "req_preferred_languages": None,
                "req_min_rating_other": None,
            },
        ).json()
        assert (cleared["req_language_mode"], cleared["req_preferred_languages"], cleared["req_min_rating_other"]) == (
            None,
            None,
            None,
        )

    def test_an_empty_row_language_list_is_stored_rather_than_read_as_unset(self, client: TestClient):
        """[] is a row that CLEARED its languages, which in "only" mode requests nothing. NULL is a
        row that inherits the owner's list. Collapsing the two changes what the row asks Radarr for."""
        created = client.post("/api/collections", json={"name": "None", "build": "per_person"}).json()
        patched = client.patch(
            f"/api/collections/{created['id']}", json={"name": "None", "req_preferred_languages": []}
        ).json()
        assert patched["req_preferred_languages"] == []

    def test_row_language_codes_are_normalised_and_deduped_on_the_way_in(self, client: TestClient):
        """Asserted against the COLUMN, not the response.

        `_serialize` normalises this field on the way out too, so a response-only assertion passes
        with the write-side normalisation deleted — proving the reader works and nothing about the
        writer. Reads normalising anyway is exactly why this has to look at what was stored.
        """
        from shortlist.server.db.models import Collection

        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}",
            json={"name": "Kids", "req_preferred_languages": ["EN", "  ja  ", "en"]},
        )
        assert resp.status_code == 200, resp.text

        with client.app.state.sessions() as session:
            row = session.query(Collection).filter(Collection.id == created["id"]).one()
            assert row.req_preferred_languages == ["en", "ja"], "lowercased, trimmed, deduped in the DB"

    def test_a_long_list_of_bad_row_codes_says_how_many_it_did_not_show(self, client: TestClient):
        """Truncating without a count means the owner fixes the five they were shown, resubmits, and
        is rejected again for values the first message implied were fine."""
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}",
            json={"name": "Kids", "req_preferred_languages": [f"{c}1" for c in "abcdefg"]},
        )
        assert resp.status_code == 422
        assert "(+2 more)" in resp.json()["detail"]

    def test_a_row_language_mode_outside_the_offered_set_is_refused(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Kids", "req_language_mode": "english_only"}
        )
        assert resp.status_code == 422
        assert "req_language_mode" in resp.json()["detail"]

    def test_a_row_language_code_that_is_not_iso_639_1_is_refused(self, client: TestClient):
        """A typo here would silently reclassify a whole language as "other" and raise the bar on it,
        which reads as the feature misbehaving rather than as bad input."""
        created = client.post("/api/collections", json={"name": "Kids", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Kids", "req_preferred_languages": ["english"]}
        )
        assert resp.status_code == 422

    def test_a_row_holding_a_retired_mode_can_still_be_saved(self, client: TestClient):
        """`future`/`missing`/`existing`/`recent` were offered by an earlier build and then retired,
        so a row can hold one. The editor PATCHes the whole row back, so serving the raw value made
        the closed-set check refuse it — and the owner could not save that row at all, not even to
        rename it. It reads as "inherits" instead, which is what the run does with it too."""
        from shortlist.server.db.models import Collection

        created = client.post("/api/collections", json={"name": "Legacy", "build": "per_person"}).json()
        with client.app.state.sessions() as session:
            row = session.query(Collection).filter_by(id=created["id"]).one()
            row.req_sonarr_monitor = "recent"  # as written by the build that still offered it
            session.commit()

        served = next(c for c in client.get("/api/collections").json() if c["id"] == created["id"])
        assert served["req_sonarr_monitor"] is None, "a retired mode reads as inherit, not as itself"

        # The rename the owner actually wanted, carrying the field back exactly as the editor does.
        resp = client.patch(
            f"/api/collections/{created['id']}",
            json={"name": "Legacy renamed", "req_sonarr_monitor": served["req_sonarr_monitor"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Legacy renamed"

    def test_a_monitor_mode_sonarr_does_not_accept_is_refused(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Bad", "build": "per_person"}).json()
        resp = client.patch(
            f"/api/collections/{created['id']}", json={"name": "Bad", "req_sonarr_monitor": "seasonsIWant"}
        )
        assert resp.status_code == 422
        assert "req_sonarr_monitor" in resp.text

    def test_an_out_of_range_rating_is_refused(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Bad", "build": "per_person"}).json()
        assert (
            client.patch(f"/api/collections/{created['id']}", json={"name": "Bad", "req_min_rating": 11.0}).status_code
            == 422
        )


class TestRowShowDaysApi:
    """The API half of "When it appears" (issue #102): which days a row is on people's Home.

    `show_days` is ISO weekdays (1=Mon .. 7=Sun) and an EMPTY list means every day — the value every
    existing row carries after migration 0088, so the upgrade changes nothing. There is deliberately
    no way to spell "never": switching the row off already means that.
    """

    def _spec(self, client: TestClient, slug: str):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        return next(s for s in specs if s.slug == slug)

    def test_show_days_round_trips(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Date Night", "show_days": [5, 6]})

        assert created.status_code == 201
        assert created.json()["show_days"] == [5, 6]

    def test_a_row_with_no_days_is_shown_every_day(self, client: TestClient):
        """The upgrade default. If this ever resolves to anything but the owner's own placement,
        upgrading silently changes what every existing row does."""
        created = client.post("/api/collections", json={"name": "Always On", "placement": "home"})

        assert created.json()["show_days"] == []
        assert self._spec(client, "always_on").placement == "home"

    @pytest.mark.parametrize("bad", [[0], [8], [1, 9], [-1], ["mon"]])
    def test_a_weekday_outside_one_to_seven_is_rejected(self, bad, client: TestClient):
        """0 is the trap: JavaScript's `Date.getDay()` calls Sunday 0, so an untyped UI would send it
        and the row would quietly never appear on a Sunday."""
        r = client.post("/api/collections", json={"name": "Bad Days", "show_days": bad})

        assert r.status_code == 422, f"{bad} should be refused"

    def test_days_are_stored_sorted_and_deduplicated(self, client: TestClient):
        """So two rows meaning the same thing compare equal, and the summary line reads in order."""
        created = client.post("/api/collections", json={"name": "Messy", "show_days": [5, 1, 5, 3]})

        assert created.json()["show_days"] == [1, 3, 5]

    def test_every_day_selected_is_stored_as_no_schedule_at_all(self, client: TestClient):
        """One stored form per meaning. Left as [1..7] the row reads as "scheduled" to the midnight
        job — which would then converge the whole server every night for a row that is never hidden —
        and the Rows page would badge the default back as an override."""
        created = client.post("/api/collections", json={"name": "All Week", "show_days": [1, 2, 3, 4, 5, 6, 7]})

        assert created.status_code == 201
        assert created.json()["show_days"] == []
        assert created.json()["shown_today"] is True
        assert self._spec(client, "all_week").placement == "both"

    def test_changing_the_days_is_applied_now_rather_than_at_the_next_midnight(self, client: TestClient, monkeypatch):
        """Set "weekdays only" on a Saturday and the row has to go NOW. Waiting for midnight is the
        exact bug the `collection.disable` rule was added to fix: you save, nothing happens, and it
        reads as broken."""
        cid = client.post("/api/collections", json={"name": "Weekdays"}).json()["id"]

        r = client.patch(f"/api/collections/{cid}", json={"name": "Weekdays", "show_days": [1, 2, 3, 4, 5]})

        assert r.status_code == 200
        assert [(j["kind"], j["payload"]) for j in _plex_jobs(client)] == [
            ("rows.visibility", {"row": "weekdays", "dry_run": False})
        ]

    def test_an_edit_that_leaves_the_days_alone_queues_no_visibility_work(self, client: TestClient, monkeypatch):
        """Renaming a row must not fire a server-wide converge."""
        cid = client.post("/api/collections", json={"name": "Weekdays", "show_days": [1]}).json()["id"]
        before = {job["id"] for job in _plex_jobs(client)}

        client.patch(f"/api/collections/{cid}", json={"name": "Weekdays Renamed"})

        assert not any(job["kind"] == "rows.visibility" for job in _plex_jobs(client) if job["id"] not in before)


class TestAnUnknownFieldIsRefused:
    """A misspelt setting used to be dropped without a word: the row saved, the setting never took."""

    def test_on_create(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Typo", "reqmin_rating": 7.5})
        assert r.status_code == 422, r.text
        assert "reqmin_rating" in r.text

    def test_on_edit(self, client: TestClient):
        cid = client.post("/api/collections", json={"name": "Typo"}).json()["id"]
        r = client.patch(f"/api/collections/{cid}", json={"name": "Typo", "seasons_lead_days": 10})
        assert r.status_code == 422, r.text


class TestANewRowsRequestSettings:
    def test_a_new_row_keeps_the_request_settings_it_was_created_with(self, client: TestClient):
        """The editor offers a row's own request floors before the row is first saved, and the create
        constructor set none of them: they were dropped without a word, and only an edit after saving stuck."""
        body = {"name": "Picky", "req_min_rating": 7.5, "req_max_per_row": 2, "req_auto_send": False}

        created = client.post("/api/collections", json=body).json()

        assert (created["req_min_rating"], created["req_max_per_row"], created["req_auto_send"]) == (7.5, 2, False)
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            row = session.get(Collection, created["id"])
            assert (row.req_min_rating, row.req_max_per_row, row.req_auto_send) == (7.5, 2, False)


class TestRequestsRowFields:
    """A "Your requests" row (issue #127): three per-row settings that have to survive the POST, the
    serializer AND the spec build, and the shapes such a row cannot take."""

    def test_requests_row_fields_round_trip_and_reach_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        body = {
            "name": "📬 {library_name} you asked for",
            "build": "per_person",
            "requests_row": True,
            "requests_window_days": 30,
            "requests_tag_pattern": "req-{username}",
            "size": 20,
        }
        r = client.post("/api/collections", json=body)
        assert r.status_code == 201, r.text
        out = r.json()
        assert (out["requests_row"], out["requests_window_days"], out["requests_tag_pattern"]) == (
            True,
            30,
            "req-{username}",
        )

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )
        spec = next(s for s in specs if s.slug == out["slug"])
        assert (spec.requests_row, spec.requests_window_days, spec.requests_tag_pattern) == (True, 30, "req-{username}")

    @pytest.mark.parametrize(
        ("bad", "msg"),
        [
            ({"build": "shared"}, "one row per person"),
            ({"rewatch": True}, "rewatch"),
            ({"seasons": ["halloween"]}, "seasonal"),
            ({"requests_tag_pattern": "req-sarah"}, "{username}"),
            ({"requests_window_days": 4000}, "less than or equal to 3650"),
        ],
    )
    def test_a_requests_row_rejects_shapes_it_cannot_be(self, client: TestClient, bad: dict, msg: str):
        body = {"name": "n", "build": "per_person", "requests_row": True, **bad}
        r = client.post("/api/collections", json=body)
        assert r.status_code == 422 and msg in r.text, r.text

    def test_patch_can_turn_a_row_into_a_requests_row(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "n", "build": "per_person"}).json()
        assert created["requests_row"] is False
        # `name` rides along because `CollectionIn` requires it on a PATCH too; only the fields SENT move.
        r = client.patch(f"/api/collections/{created['id']}", json={"name": "n", "requests_row": True})
        assert r.status_code == 200, r.text
        assert r.json()["requests_row"] is True
        assert (r.json()["requests_window_days"], r.json()["requests_tag_pattern"]) == (90, "")

    @pytest.mark.parametrize(
        ("existing", "patch", "msg"),
        [
            ({"rewatch": True}, {"requests_row": True}, "rewatch"),
            ({"requests_row": True}, {"rewatch": True}, "rewatch"),
            ({"requests_row": True}, {"build": "shared"}, "one row per person"),
            ({"requests_row": True}, {"seasons": ["halloween"]}, "seasonal"),
        ],
    )
    def test_a_patch_is_judged_against_the_merged_row(self, client: TestClient, existing: dict, patch: dict, msg: str):
        """A PATCH sends only what changed, so the body alone never shows the clash — a rewatch row
        turning into a requests row sends no `rewatch`, and it is the STORED value that forbids it."""
        created = client.post("/api/collections", json={"name": "n", "build": "per_person", **existing})
        assert created.status_code == 201, created.text
        r = client.patch(f"/api/collections/{created.json()['id']}", json={"name": "n", **patch})
        assert r.status_code == 422 and msg in r.text, r.text
