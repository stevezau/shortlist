"""Support Mode: the gate, the tools, and the plain text a reporter pastes back.

The copy block is the actual deliverable of this feature — a maintainer debugging someone else's
server reads that text and nothing else — so it is asserted as hard as the JSON is. A tool whose
JSON is right and whose text omits the finding has failed at the only job it has.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from shortlist.server.api.support import ENABLED_UNTIL_KEY
from shortlist.server.db.models import Collection, Event, PickRow, Server, User, WatchedTitle, WatchSyncState
from shortlist.server.settings_store import SettingsStore


def _enable(client) -> None:
    assert client.post("/api/support/enable").status_code == 200


def _rows_on_plex(monkeypatch, slugs) -> None:
    """The per-person rows that EXIST on the server — what `sharing` measures its verdict against.

    Every sharing test has to say this, because "which labels belong in everyone's filter" is a
    question only the PMS can answer. Measuring against the enabled-user list instead is the bug
    this guards: a user with no row yet has no label anywhere, so expecting one made every account
    read as leaking (issue #76).
    """
    from types import SimpleNamespace

    import shortlist.server.api.support as support
    from shortlist.engine.models import OwnedRow

    owned = {slug: OwnedRow(label=f"shortlist_{slug}") for slug in slugs}
    monkeypatch.setattr(
        support,
        "_plex_client",
        lambda _store: SimpleNamespace(
            owned_collections=lambda _prefix: owned,
            # Consulted only when NO labels came back, to tell "no rows exist" from "the rows lost
            # their labels" — an empty server has no marked collections either.
            owned_row_surfaces=lambda *_a, **_k: [
                {"marked": True, "title": s, "library": "Movies", "label": f"shortlist_{s}"} for s in slugs
            ],
        ),
    )


def _set_row_cap(app, pct: float | None) -> None:
    """Point the app's own seeded `picked` row at a cap. Updating rather than inserting, because a
    fresh app already ships that row and its slug is unique."""
    with app.state.sessions() as session:
        row = session.query(Collection).filter(Collection.slug == "picked").one()
        row.watched_pct = pct
        session.commit()


def _seed_teacup(app, *, viewed: int | None, total: int | None, delivered: bool, slug: str = "sarah") -> None:
    """The reported bug, as data: a show in someone's row, with a watch record we control.

    `viewed=None` means no watched row at all — the case the log could never distinguish from
    "watched it but under the threshold", and the whole reason this tool exists.
    """
    with app.state.sessions() as session:
        user = session.query(User).filter(User.slug == slug).one()
        if viewed is not None:
            session.add(
                WatchedTitle(
                    user_id=user.id,
                    section_key="1",
                    rating_key=900,
                    tmdb_id=226637,
                    media_type="show",
                    title="Teacup",
                    year=2024,
                    watch_count=max(1, viewed),
                    viewed_leaf_count=viewed,
                    leaf_count=total,
                )
            )
        if delivered:
            # Get-or-create: a fresh app already seeds a default `picked` row, and this helper is
            # called twice in the ordering test.
            if session.query(Collection).filter(Collection.slug == "picked").one_or_none() is None:
                session.add(Collection(slug="picked", name="Picked for You", media="both", size=15, watched_pct=None))
                session.flush()
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=226637,
                    media_type="show",
                    rating_key=900,
                    rank=2,
                    collection_slug="picked",
                    section_key="1",
                    library="TV Shows",
                    title="Teacup",
                )
            )
        session.commit()


class TestTheGate:
    """Support mode is off by default and lapses on its own — the two properties that make it a
    boundary rather than a hidden page."""

    def test_tools_are_refused_until_the_owner_turns_the_mode_on(self, client):
        assert client.get("/api/support/status").json() == {
            "enabled": False,
            "expires_at": None,
            "seconds_remaining": 0,
        }
        for path in ("/api/support/health", "/api/support/title?q=x", "/api/support/rows"):
            assert client.get(path).status_code == 403, path

    def test_enabling_unlocks_the_tools_and_reports_an_expiry(self, client):
        body = client.post("/api/support/enable").json()
        assert body["enabled"] is True
        assert body["expires_at"] is not None
        # 24h, less the moment the request took.
        assert 23 * 3600 < body["seconds_remaining"] <= 24 * 3600
        assert client.get("/api/support/rows").status_code == 200

    def test_disabling_locks_them_again_immediately(self, client):
        _enable(client)
        assert client.post("/api/support/disable").json()["enabled"] is False
        assert client.get("/api/support/rows").status_code == 403

    def test_an_expired_mode_refuses_without_anyone_turning_it_off(self, client):
        """The self-reversing half of the promise. Without this the 'mode' is just a hidden page
        with extra steps — someone flips it on during a bug report and it stays on for ever."""
        _enable(client)
        with client.app.state.sessions() as session:
            stale = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            SettingsStore(session).set(ENABLED_UNTIL_KEY, stale)
            session.commit()
        assert client.get("/api/support/rows").status_code == 403
        assert client.get("/api/support/status").json()["enabled"] is False

    def test_a_corrupt_expiry_reads_as_off_rather_than_as_open(self, client):
        """Fail closed. An unparseable value must never be treated as 'no expiry, therefore on'."""
        with client.app.state.sessions() as session:
            SettingsStore(session).set(ENABLED_UNTIL_KEY, "not-a-timestamp")
            session.commit()
        assert client.get("/api/support/status").json()["enabled"] is False
        assert client.get("/api/support/rows").status_code == 403

    def test_the_key_survives_a_legacy_purge(self, client):
        """The boot-time settings sweep must not switch the mode off mid-session.

        (`purge_legacy` only deletes `LEGACY_KEYS`, so this holds regardless of DEFAULTS — an earlier
        comment here claimed otherwise. Kept as a guard on the sweep, not as evidence about DEFAULTS.)
        """
        _enable(client)
        with client.app.state.sessions() as session:
            store = SettingsStore(session)
            store.purge_legacy()
            session.commit()
        assert client.get("/api/support/status").json()["enabled"] is True

    def test_the_mode_cannot_be_switched_on_through_the_settings_api(self, client):
        """Found by architecture review 2026-08-05.

        While the expiry was an ordinary settings key, `PUT /api/settings` could write a far-future
        timestamp — switching every tool on with no `support.enable` event and no 24h lapse, which is
        both halves of what makes this a boundary rather than a hidden page. A settings restore or a
        stray Settings-page save could trip it by accident.
        """
        far_future = "2999-01-01T00:00:00+00:00"

        response = client.put("/api/settings", json={"values": {ENABLED_UNTIL_KEY: far_future}})

        # Rejected as an unknown key, or accepted-and-ignored — either is fine; what must NOT happen
        # is the mode coming on.
        assert client.get("/api/support/status").json()["enabled"] is False, response.text
        assert client.get("/api/support/rows").status_code == 403

    def test_the_expiry_is_never_exposed_in_the_settings_response(self, client):
        """It is in PRIVATE_KEYS, so it must not appear in the generic settings payload either."""
        _enable(client)

        values = client.get("/api/settings").json()

        assert ENABLED_UNTIL_KEY not in values

    def test_switching_the_mode_is_audited(self, client):
        _enable(client)
        client.post("/api/support/disable")
        with client.app.state.sessions() as session:
            scopes = [e.scope for e in session.query(Event).all()]
        assert "support.enable" in scopes and "support.disable" in scopes


class TestTitleLookup:
    """The tool the surface was built for: does Shortlist think this person watched this title?"""

    def test_names_the_person_who_was_delivered_a_title_with_no_watched_record(self, client):
        """The reported bug. The run log records how MANY titles a watch read returned and never
        WHICH, so this distinction — delivered, and no watch record at all — was unreachable."""
        _seed_teacup(client.app, viewed=None, total=None, delivered=True)
        _enable(client)

        body = client.get("/api/support/title", params={"q": "Teacup"}).json()

        assert body["flagged"] == ["sarah"]
        row = body["rows"][0]
        assert row["watched_record"] is False
        assert row["counts_as_watched"] is False
        assert row["delivered"] == [{"row": "picked", "rank": 2, "library": "TV Shows"}]
        # The text is what the reporter pastes back, so the finding must survive into it.
        assert "PROBLEM: delivered but not counted as watched: sarah" in body["text"]

    def test_a_part_watched_show_answers_with_the_rule_that_actually_applied(self, client):
        """Two episodes of eight, answered per the ROW's cap — the seam between this release's halves.

        Since 1.2 there are two rules: at cap 0 a started show counts as watched, above 0 only a
        finished one does. Answering with the finished rule everywhere was the pre-1.2 answer, and it
        got the diagnosis backwards — "I'm two episodes in and it's still in my row" would read as the
        bug 1.2 just fixed, rather than the row not having rebuilt since. (Found by architecture
        review 2026-08-05; the previous version of this test asserted the old behaviour.)

        The tool still shows BOTH numbers either way, because the gap between them is the explanation.
        """
        _seed_teacup(client.app, viewed=2, total=8, delivered=True)
        _enable(client)

        _set_row_cap(client.app, 0.0)
        row = client.get("/api/support/title", params={"q": "Teacup"}).json()["rows"][0]
        assert (row["viewed_leaf_count"], row["leaf_count"]) == (2, 8)
        assert row["watched_record"] is True
        assert row["counts_as_watched"] is True, "at 0% a started show IS watched"
        assert row["problem"] is False, "counted as watched, so its delivery is not the reported bug"

        _set_row_cap(client.app, 0.4)
        row = client.get("/api/support/title", params={"q": "Teacup"}).json()["rows"][0]
        assert row["counts_as_watched"] is False, "above 0% only a FINISHED show counts"

    def test_a_rewatch_row_keeps_the_finished_rule_even_at_zero(self, client):
        """A rewatch row is built FROM watched titles, so it never takes the 0% exclusion — and the
        tool must not claim otherwise."""
        _seed_teacup(client.app, viewed=2, total=8, delivered=True)
        with client.app.state.sessions() as session:
            row = session.query(Collection).filter(Collection.slug == "picked").one()
            row.watched_pct, row.rewatch = 0.0, True
            session.commit()
        _enable(client)

        row = client.get("/api/support/title", params={"q": "Teacup"}).json()["rows"][0]

        assert row["rewatch"] is True
        assert row["counts_as_watched"] is False

    def test_a_finished_show_counts_and_is_not_flagged(self, client):
        _seed_teacup(client.app, viewed=8, total=8, delivered=True)
        _enable(client)

        body = client.get("/api/support/title", params={"q": "Teacup"}).json()

        assert body["rows"][0]["counts_as_watched"] is True
        assert body["flagged"] == []
        assert "PROBLEM" not in body["text"]

    def test_the_threshold_is_the_engines_own_and_not_a_second_copy_of_it(self, client):
        """Three of eight is the engine's exact boundary (`min(80%, max(3, 15%))`). Asserting the
        boundary here is what catches this tool drifting away from the code it describes — a
        diagnostic that confidently contradicts the engine is worse than none."""
        _seed_teacup(client.app, viewed=3, total=8, delivered=False)
        _enable(client)

        assert client.get("/api/support/title", params={"q": "Teacup"}).json()["rows"][0]["counts_as_watched"] is True

    def test_a_watched_movie_is_finished_with_no_fraction_applied(self, client):
        app = client.app
        with app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                WatchedTitle(
                    user_id=user.id,
                    section_key="2",
                    rating_key=7,
                    tmdb_id=949,
                    media_type="movie",
                    title="Heat",
                    watch_count=1,
                )
            )
            session.commit()
        _enable(client)

        assert client.get("/api/support/title", params={"q": "Heat"}).json()["rows"][0]["counts_as_watched"] is True

    def test_an_unknown_title_says_so_instead_of_returning_an_empty_table(self, client):
        _enable(client)
        body = client.get("/api/support/title", params={"q": "Nothing Here"}).json()
        assert body["rows"] == []
        assert "No watched record and no delivery" in body["text"]

    def test_two_titles_matching_one_query_do_not_merge_into_one_verdict(self, client):
        """A false ALL-CLEAR, caught by architecture review 2026-08-05.

        Grouping by user alone kept only the last watched record and then judged it against an
        unrelated delivered pick. So on a franchise query — "Doctor Who", "Star Wars", "The Office" —
        a finished title could vouch for a completely different one that was delivered and never
        watched, and the page rendered the green "nothing unexpected" for the exact bug this tool
        exists to find.
        """
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            # Finished, never delivered — the innocent one.
            session.add(
                WatchedTitle(
                    user_id=user.id,
                    section_key="1",
                    rating_key=1,
                    tmdb_id=111,
                    media_type="show",
                    title="Doctor Who: Classic",
                    watch_count=7,
                    viewed_leaf_count=7,
                    leaf_count=7,
                )
            )
            # Delivered on a 0% row with NO watched record — the actual problem.
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=222,
                    media_type="show",
                    rating_key=2,
                    rank=1,
                    collection_slug="picked",
                    library="TV Shows",
                    title="Doctor Who",
                )
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/title", params={"q": "Doctor Who"}).json()

        assert len(body["rows"]) == 2, "one row per person PER TITLE, not per person"
        problem = next(r for r in body["rows"] if r["problem"])
        assert (problem["title"], problem["tmdb_id"]) == ("Doctor Who", 222)
        assert problem["watched_record"] is False
        innocent = next(r for r in body["rows"] if not r["problem"])
        assert innocent["counts_as_watched"] is True
        assert body["flagged"] == ["sarah"]
        assert "PROBLEM" in body["text"]
        # The title has to be IN the block, or a two-title verdict cannot be read.
        assert "Doctor Who" in body["text"]

    def test_a_capped_search_says_so_rather_than_looking_complete(self, client, monkeypatch):
        """A silently truncated result set reads as "that's everyone", which is the wrong answer."""
        import shortlist.server.api.support as support

        monkeypatch.setattr(support, "_MATCH_CAP", 1)
        _seed_teacup(client.app, viewed=8, total=8, delivered=True)
        _enable(client)

        body = client.get("/api/support/title", params={"q": "Teacup"}).json()

        assert body["capped"] is True
        assert "only the first 1 matches" in body["text"]

    def test_the_problem_row_sorts_to_the_top(self, client):
        """The reporter screenshots or pastes the first few lines. The finding cannot be on line 40."""
        app = client.app
        _seed_teacup(app, viewed=8, total=8, delivered=False, slug="mike")
        _seed_teacup(app, viewed=None, total=None, delivered=True, slug="sarah")
        _enable(client)

        assert [r["user"] for r in client.get("/api/support/title", params={"q": "Teacup"}).json()["rows"]] == [
            "sarah",
            "mike",
        ]


class TestPerson:
    """Separating 'watches nothing' from 'a library refused their token' — indistinguishable today."""

    def test_a_library_that_was_never_read_is_called_out_by_name(self, client):
        app = client.app
        with app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            # Read successfully once; section 3 has no state row at all, so it has NEVER been read.
            session.add(WatchSyncState(user_id=user.id, section_key="2", last_full_at=datetime.now(UTC), item_count=9))
            session.add(
                WatchedTitle(user_id=user.id, section_key="3", rating_key=1, tmdb_id=5, media_type="show", title="X")
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/person/sarah").json()

        assert body["never_read"] == ["3"]
        # No Plex to list from here, so the fallback names the bare section key. And it is stated as
        # a fact, not a fault: an unshared library is never read either, and that is correct config.
        assert "NEVER READ for this person: section 3" in body["text"]
        assert "not shared with them" in body["text"]

    def test_counts_movies_and_shows_separately(self, client):
        app = client.app
        with app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            for i in range(3):
                session.add(
                    WatchedTitle(
                        user_id=user.id, section_key="2", rating_key=i, tmdb_id=i, media_type="movie", title=f"M{i}"
                    )
                )
            session.add(
                WatchedTitle(user_id=user.id, section_key="1", rating_key=99, tmdb_id=99, media_type="show", title="S")
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/person/sarah").json()
        assert (body["watched_movies"], body["watched_shows"]) == (3, 1)

    def test_a_library_with_no_rows_at_all_is_still_listed(self, client, monkeypatch):
        """The bug a live check caught (2026-08-05).

        A library that has NEVER been read for someone has no watched titles and no sync state, so
        deriving the list from their own rows made the one library being looked for invisible — the
        table showed only what worked and reported no problem. The section list has to come from
        PLEX. This is the tool's entire purpose, so it gets the regression test.
        """
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        monkeypatch.setattr(
            support,
            "_plex_client",
            lambda _store: SimpleNamespace(
                sections=lambda: [
                    SimpleNamespace(key="1", title="TV Shows", type="show", totalSize=277),
                    SimpleNamespace(key="2", title="Movies", type="movie", totalSize=371),
                ]
            ),
        )
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            # Only section 2 was ever read. Section 1 has no trace of this person whatsoever.
            session.add(WatchSyncState(user_id=user.id, section_key="2", item_count=9))
            session.commit()
        _enable(client)

        body = client.get("/api/support/person/sarah").json()

        assert body["never_read"] == ["1"]
        assert [lib["library"] for lib in body["libraries"]] == ["TV Shows", "Movies"]
        # Named, not keyed — the reporter has to match this against what they see in Plex.
        assert "NEVER READ for this person: TV Shows" in body["text"]

    def test_an_unknown_person_is_a_404_naming_what_was_looked_up(self, client):
        _enable(client)
        r = client.get("/api/support/person/nobody")
        assert r.status_code == 404
        assert "nobody" in r.json()["detail"]


class TestRowSettings:
    """Answering 'but I set it to 0%' by showing which value actually won."""

    def test_reports_the_global_default_and_says_it_came_from_the_global(self, client):
        _set_row_cap(client.app, None)
        _enable(client)

        body = client.get("/api/support/rows").json()
        row = body["rows"][0]
        assert (row["watched_pct"], row["watched_pct_source"]) == (0.0, "global")

    def test_a_row_override_wins_and_is_labelled_as_the_override(self, client):
        """The failure this exists to catch: the owner sets the GLOBAL to 0% while the row carries
        its own 40%, and nothing in the row editor shows which one the run used."""
        _set_row_cap(client.app, 0.4)
        _enable(client)

        row = client.get("/api/support/rows").json()["rows"][0]
        assert (row["watched_pct"], row["watched_pct_source"]) == (0.4, "row")
        assert "40%" in row_text(client) and "row" in row_text(client)


def row_text(client) -> str:
    return client.get("/api/support/rows").json()["text"]


class TestCopyBlocks:
    """The wire format. Rendered server-side so it is testable and cannot drift from the UI."""

    @pytest.mark.parametrize("path", ["/api/support/health", "/api/support/rows", "/api/support/libraries"])
    def test_every_block_is_stamped_and_terminated(self, client, path):
        """Read without the screen that produced it, a block is useless unless it says which build
        and when. The end marker is what tells a reader a paste was not truncated."""
        _enable(client)
        body = client.get(path).json()["text"]
        assert body.startswith("=== Shortlist support ·")
        assert "version" in body and "generated" in body
        assert body.rstrip().endswith("=== end ===")

    @pytest.mark.parametrize("path", ["/api/support/health", "/api/support/rows", "/api/support/libraries"])
    def test_no_line_is_wide_enough_for_discord_or_reddit_to_mangle(self, client, path):
        """Both destinations wrap or truncate wide text, which destroys the column alignment that
        makes these readable at a glance."""
        _enable(client)
        for line in client.get(path).json()["text"].splitlines():
            assert len(line) <= 76, line

    def test_the_bundle_concatenates_every_block_for_a_single_download(self, client):
        _enable(client)
        r = client.get("/api/support/bundle.txt")
        assert r.status_code == 200
        assert r.text.count("=== Shortlist support ·") >= 4  # header + health + libraries + rows

    def test_no_bundle_section_fails(self, client):
        """Counting headers is not enough — a section that RAISED still renders a header.

        `bundle` calls the handlers directly, bypassing FastAPI's dependency injection, so a
        `Query(default=...)` parameter arrives as a params object rather than its default. That cost
        the timeline section on every install while the header count stayed happy.
        """
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "THIS SECTION FAILED" not in text, text
        # Named explicitly: this is the one that broke, and it takes a parameter.
        assert "=== Shortlist support · timeline ===" in text
        assert "times shown LOCAL" in text


class TestDegradesInsteadOfBlanking:
    """These tools are wanted precisely when something is broken."""

    def test_health_reports_a_disconnected_plex_as_content_not_as_an_error(self, client):
        """No Plex is configured in the test app. A 500 here would hide the very fact being sought."""
        _enable(client)
        r = client.get("/api/support/health")
        assert r.status_code == 200
        plex = next(c for c in r.json()["checks"] if c["name"] == "Plex server")
        assert plex["ok"] is False and plex["detail"] == "not connected"
        assert "BAD  Plex server" in r.json()["text"]

    def test_every_health_check_reports_independently(self, client):
        """One broken probe must not take the others with it — the panel's whole job is the contrast
        between what works and what doesn't."""
        _enable(client)
        checks = client.get("/api/support/health").json()["checks"]
        assert {c["name"] for c in checks} >= {"Plex server", "Database", "Clocks", "Last run"}
        assert next(c for c in checks if c["name"] == "Database")["ok"] is True

    def test_libraries_reports_an_unreachable_server_in_the_copy_block(self, client):
        _enable(client)
        body = client.get("/api/support/libraries").json()
        assert body["libraries"] == []
        assert "COULD NOT READ" in body["text"]


class TestReadsAreAudited:
    def test_running_a_tool_records_who_looked_at_what(self, client):
        _enable(client)
        client.get("/api/support/title", params={"q": "Teacup"})
        with client.app.state.sessions() as session:
            reads = [e.message for e in session.query(Event).filter(Event.scope == "support.read").all()]
        assert {"tool": "title", "q": "Teacup", "matches": 0} in reads


class TestRowSchedule:
    """The tool that answers "I changed the setting and nothing happened"."""

    def test_reports_the_rebuild_cadence_and_how_stale_the_row_is(self, client):
        """Freshness is a CADENCE, not a nightly shuffle, and the engine logs that decision nowhere.
        At the 0.5 default a row re-selects about weekly and redelivers unchanged in between — so a
        correct setting genuinely does nothing until the row next rebuilds."""
        with client.app.state.sessions() as session:
            row = session.query(Collection).filter(Collection.slug == "picked").one()
            row.freshness = 0.5
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="picked",
                    title="Old Pick",
                    created_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
            session.commit()
        _enable(client)

        row = next(r for r in client.get("/api/support/row-schedule").json()["rows"] if r["slug"] == "picked")

        assert row["rebuild_every_days"] > 1  # weekly-ish, not nightly
        assert row["days_since_built"] == 30
        assert row["due"] is True

    def test_a_frozen_row_reads_as_frozen_not_as_broken(self, client):
        """Freshness 0 means "never refresh once built" — a deliberate pinned row. Reporting it as
        overdue for ever would send someone hunting a bug that is a setting."""
        with client.app.state.sessions() as session:
            session.query(Collection).filter(Collection.slug == "picked").one().freshness = 0.0
            session.commit()
        _enable(client)

        row = next(r for r in client.get("/api/support/row-schedule").json()["rows"] if r["slug"] == "picked")

        assert row["rebuild_every_days"] == 0
        assert row["due"] is False
        assert "never (frozen)" in client.get("/api/support/row-schedule").json()["text"]


class TestOperationalTools:
    def test_jobs_surfaces_a_failure_with_its_error(self, client):
        from shortlist.server.db.models import Job

        with client.app.state.sessions() as session:
            session.add(Job(kind="privacy_sync", status="failed", attempts=3, detail="gave up", error="boom"))
            session.commit()
        _enable(client)

        body = client.get("/api/support/jobs").json()
        assert body["failed"] == 1
        assert "FAILED" in body["text"] and "boom" in body["text"]

    def test_clocks_states_the_offset_rather_than_leaving_it_to_be_inferred(self, client):
        """Every db timestamp is UTC and every log line is local. Reading one as the other inverts
        the order of events, so the offset is spelled out."""
        _enable(client)
        body = client.get("/api/support/clocks").json()
        assert "db stores UTC" in body["text"]
        assert body["utc_now"] and body["local_now"]

    def test_database_proves_the_schema_rather_than_trusting_the_head(self, client):
        """A migration that no-ops on a real database still stamps its version — this project has
        shipped exactly that — so tables are counted, not assumed."""
        _enable(client)
        body = client.get("/api/support/database").json()
        assert body["head"]
        assert body["missing_tables"] == []
        assert body["tables_present"] >= body["tables_expected"]

    def test_config_never_renders_a_secret_only_whether_one_is_set(self, client, monkeypatch):
        """Rule 9. This text goes into a chat window, so a token must be unable to reach it even
        when the setting it lives in is being reported on."""
        monkeypatch.setenv("PLEX_TOKEN", "super-secret-value")
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "super-secret-value")
            session.commit()
        _enable(client)

        body = client.get("/api/support/config").json()

        assert "super-secret-value" not in body["text"]
        token_row = next(r for r in body["settings"] if r["key"] == "plex.token")
        assert token_row["secret"] is True and token_row["value"] == ""
        assert "(secret set)" in body["text"]


class TestExplainers:
    def test_why_here_reports_the_seed_and_source_behind_a_pick(self, client):
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=226637,
                    media_type="show",
                    rating_key=900,
                    rank=2,
                    collection_slug="picked",
                    library="TV Shows",
                    title="Teacup",
                    sources="tmdb_similar",
                    affinity=0.92,
                    seed_title="FROM",
                )
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/pick", params={"user": "sarah", "title": "Teacup"}).json()

        assert body["picks"][0]["seed_title"] == "FROM"
        assert "tmdb_similar" in body["text"] and "FROM" in body["text"]

    def test_why_missing_distinguishes_never_suggested_from_suggested_then_dropped(self, client):
        """The two need completely different fixes — widen the sources, versus loosen a filter — and
        an empty row looks identical either way."""
        _enable(client)
        body = client.get("/api/support/missing", params={"user": "sarah", "title": "Dune"}).json()
        assert "never been built" in body["verdict"] or "Never even suggested" in body["verdict"]

    def test_why_missing_says_so_when_the_title_is_actually_present(self, client):
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=3,
                    collection_slug="picked",
                    title="Dune",
                )
            )
            session.commit()
        _enable(client)

        assert (
            "It IS in their row"
            in client.get("/api/support/missing", params={"user": "sarah", "title": "Dune"}).json()["verdict"]
        )

    def test_the_funnel_names_the_stage_that_ate_the_row(self, client):
        """A short row has one cause per stage and they need different fixes; only the per-stage
        counts tell them apart."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    trace={
                        "gathers": [
                            {"pool": "default", "pooled": 468, "disposition": {"not_in_library": 427, "kept": 41}}
                        ]
                    },
                )
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/funnel", params={"user": "sarah"}).json()

        assert body["stages"][0]["disposition"]["not_in_library"] == 427
        assert "not_in_library" in body["text"]

    def test_ai_says_plainly_when_no_curator_is_configured(self, client):
        _enable(client)
        body = client.get("/api/support/ai", params={"user": "sarah"}).json()
        assert body["provider"] == "none"
        assert "No AI curator configured" in body["text"]

    def test_per_person_tools_404_on_an_unknown_username(self, client):
        _enable(client)
        for path, params in (
            ("/api/support/pick", {"user": "nobody", "title": "x"}),
            ("/api/support/missing", {"user": "nobody", "title": "x"}),
            ("/api/support/funnel", {"user": "nobody"}),
            ("/api/support/ai", {"user": "nobody"}),
        ):
            assert client.get(path, params=params).status_code == 404, path


class TestHistoryTools:
    def test_the_timeline_renders_local_time_and_says_so(self, client):
        """Mixing UTC and local inverts the order of events. The timeline picks one and labels it."""
        _enable(client)
        body = client.get("/api/support/timeline").json()
        assert "times shown LOCAL (db stores UTC)" in body["text"]
        for entry in body["entries"]:
            assert entry["at_utc"] and entry["at_local"]

    def test_settings_history_warns_when_a_change_postdates_the_last_build(self, client):
        """The pairing that answers the original bug report: the setting was right, the row simply
        had not rebuilt since it changed."""
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                PickRow(
                    user_id=user.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="picked",
                    title="Built Before The Change",
                    created_at=datetime.now(UTC) - timedelta(days=2),
                )
            )
            session.add(Event(scope="settings.update", message={"recommendations.watched_pct": 0.0}))
            session.commit()
        _enable(client)

        body = client.get("/api/support/settings-history").json()

        # A flag, not the sentence: the prose WRAPS at 76 columns, so any assertion on a full
        # sentence is really an assertion about where the wrap lands. The marker is stable.
        assert body["change_after_last_build"] is True
        assert "NOTE:" in body["text"]


class TestLivePlexTools:
    """Read-only, and each degrades into content rather than a 500 — Plex is unconfigured here."""

    def test_connection_flags_a_person_with_no_share_token(self, client):
        """A refused library and a person who watches nothing produce the same empty watched set.
        Only this pairing separates them."""
        _enable(client)
        body = client.get("/api/support/connection").json()
        assert "sarah" in body["problems"]
        assert "have a library we have never read" in body["text"]

    def test_read_as_refuses_an_endpoint_that_is_not_on_the_allowlist(self, client):
        """An allowlist, never a free URL field: this container sits on someone's home network, so
        an arbitrary-URL fetcher behind owner auth is a port scanner with extra steps."""
        _enable(client)
        r = client.get("/api/support/read-as", params={"user": "sarah", "endpoint": "http://192.168.1.1/admin"})
        assert r.status_code == 400
        assert "Unknown check" in r.json()["detail"]

    def test_read_as_does_not_echo_the_owners_filesystem_layout(self, client, monkeypatch):
        """`/library/sections` returns a `<Location path=…>` per library, and this response sits behind
        a Copy button. A storage layout is routinely named after a person (`/Users/johnsmith/Media`),
        and it answers nothing this tool asks — which is whose token can read which library."""
        import httpx

        xml = (
            '<MediaContainer size="1" machineIdentifier="7ee8abc1bcdcc79389ad1e15c30e2692714bc940">'
            '<Directory key="1" title="Movies"><Location id="1" path="/Users/johnsmith/Media/Movies"/>'
            '</Directory><Video><Part file="/Users/johnsmith/Media/Movies/Heat.mkv"/></Video>'
            "</MediaContainer>"
        )
        monkeypatch.setattr(httpx, "get", lambda *_a, **_k: httpx.Response(200, text=xml))
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "owner-token")
            session.query(User).filter(User.slug == "sarah").update({"user_type": "owner"})
            session.commit()
        _enable(client)

        body = client.get("/api/support/read-as", params={"user": "sarah", "endpoint": "libraries"}).json()

        for field in ("body", "text"):
            assert "johnsmith" not in body[field], field
            assert "/Media/Movies" not in body[field], field
        assert 'path="<path>"' in body["body"], body["body"]
        assert 'file="<path>"' in body["body"]
        assert "Movies" in body["body"], "the library TITLE is the diagnostic part and must survive"

    def test_read_as_refuses_when_no_token_exists_rather_than_reading_as_the_owner(self, client):
        """Silently falling back to the owner's token would answer a DIFFERENT question and look
        like it worked — the exact confusion the tool exists to end."""
        # Plex connected, so the missing TOKEN is what refuses this rather than the missing server.
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "owner-token")
            session.commit()
        _enable(client)

        r = client.get("/api/support/read-as", params={"user": "sarah", "endpoint": "libraries"})

        assert r.status_code == 409
        assert "No share token" in r.json()["detail"]

    def test_sharing_reports_an_unreachable_plex_as_content(self, client):
        _enable(client)
        body = client.get("/api/support/sharing").json()
        assert body["accounts"] == []
        assert "COULD NOT READ" in body["text"]

    def test_drift_refuses_to_call_anything_missing_when_plex_could_not_be_read(self, client):
        """The most alarming possible false alarm: an unread server reported as an empty one would
        mark every delivered row as missing."""
        from shortlist.server.db.models import Delivery

        with client.app.state.sessions() as session:
            session.add(
                Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=555, title="x")
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/drift").json()

        assert body["error"]
        assert body["missing_on_plex"] == []
        assert "not the same as an empty one" in body["text"]

    def test_drift_reports_a_delivered_row_that_is_not_on_the_server(self, client, monkeypatch):
        """The bug class this targets: the database records a change that never reached Plex."""
        import shortlist.server.api.support as support

        monkeypatch.setattr(
            support,
            "_plex_client",
            lambda _store: type(
                "P",
                (),
                {
                    "list_owned_collections": lambda self: [
                        {"rating_key": 999, "title": "Other", "label": "shortlist_x"}
                    ],
                    "owned_row_surfaces": lambda self, *a, **k: [
                        {"rating_key": 999, "title": "Other", "label": "shortlist_x", "marked": True}
                    ],
                },
            )(),
        )
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Delivery

            session.add(
                Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=555, title="x")
            )
            session.commit()
        _enable(client)

        body = client.get("/api/support/drift").json()

        assert [m["row"] for m in body["missing_on_plex"]] == ["picked"]
        assert body["orphans_on_plex"][0]["title"] == "Other"
        assert "MISSING on Plex: picked for sarah" in body["text"]


#: Every no-argument tool. The class below asserts the properties that must hold for ALL of them, so
#: a tool added later cannot quietly skip the gate or the paste-width limit.
class TestSurfacesAnswersWhoCanSeeWhichRow:
    """Issue #75: the admin could see another user's row and nothing in the app could say why.

    The owner has no share filter (plex-safety rule 5), so a row's own promotion flags are the only
    thing keeping it off their screen — and no tool reported those flags.
    """

    @staticmethod
    def _surfaces(monkeypatch, rows):
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        monkeypatch.setattr(
            support,
            "_plex_client",
            lambda _store: SimpleNamespace(
                owned_row_surfaces=lambda *_a, **_k: rows,
                list_owned_collections=lambda *_a, **_k: [],
                owned_collections=lambda *_a, **_k: {},
            ),
        )

    @staticmethod
    def _owner(client, slug="mike"):
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == slug).one_or_none()
            if user is None:
                user = User(plex_account_id=1, username=slug, slug=slug)
                session.add(user)
            user.user_type, user.enabled = "owner", True
            session.commit()

    @staticmethod
    def _row(label, **flags):
        base = {
            "library": "Movies",
            "library_key": "1",
            "title": f"Picked for You [acct {abs(hash(label)) % 999}]",
            "label": label,
            "marked": True,
            "rating_key": 555,
            "recommended": False,
            "own_home": False,
            "shared_home": True,
        }
        return base | flags

    def test_someone_elses_row_on_the_owners_home_is_called_a_bug(self, client, monkeypatch):
        """The invariant. No setting makes this correct: the owner cannot be filtered, so a
        non-owner's row claiming `promotedToOwnHome` is visible to them with nothing able to hide it."""
        self._owner(client)
        self._surfaces(monkeypatch, [self._row("shortlist_sarah", own_home=True)])
        _enable(client)

        body = client.get("/api/support/surfaces").json()

        assert len(body["on_owner_home"]) == 1
        assert "BUG: 1 row(s) that are not yours sit on YOUR Home screen." in body["text"]

    def test_the_owners_own_row_on_their_home_is_fine(self, client, monkeypatch):
        self._owner(client)
        self._surfaces(monkeypatch, [self._row("shortlist_mike", own_home=True)])
        _enable(client)

        body = client.get("/api/support/surfaces").json()

        assert body["on_owner_home"] == []
        assert "BUG:" not in body["text"]

    def test_a_shared_row_may_sit_on_the_owners_home(self, client, monkeypatch):
        """A shared row is ONE public collection for everybody, so the owner's Home is a legitimate
        placement for it — flagging it would be crying wolf on a correct server."""
        from shortlist.engine.models import SHARED_LABEL_PREFIX

        self._owner(client)
        self._surfaces(monkeypatch, [self._row(f"{SHARED_LABEL_PREFIX}popular", own_home=True)])
        _enable(client)

        body = client.get("/api/support/surfaces").json()

        assert body["on_owner_home"] == []

    def test_a_recommended_row_is_explained_not_blamed(self, client, monkeypatch):
        """The other half of #75, and it is NOT a bug: Plex has one Recommended flag per collection
        and the owner has no filter, so a row set to show on everyone else's library shelf lands on
        the owner's too. The tool must say that is a settings change, not a fault."""
        self._owner(client)
        self._surfaces(monkeypatch, [self._row("shortlist_sarah", recommended=True)])
        _enable(client)

        body = client.get("/api/support/surfaces").json()

        assert len(body["on_owner_shelf"]) == 1
        assert body["on_owner_home"] == []
        assert "BUG" not in body["text"]
        assert "Recommended flag per collection" in body["text"]

    def test_a_collection_of_ours_with_no_label_is_called_out(self, client, monkeypatch):
        """Issue #76's shape: nothing can hide an unlabelled row, and the next run's sweep deletes it
        as an orphan."""
        self._owner(client)
        self._surfaces(monkeypatch, [self._row("", marked=True)])
        _enable(client)

        body = client.get("/api/support/surfaces").json()

        assert len(body["unlabelled"]) == 1
        assert "carry NO label" in body["text"]

    def test_drift_distinguishes_deleted_rows_from_unreadable_labels(self, client, monkeypatch):
        """Both used to report `on plex: 0`. A reporter read that as "my rows were deleted" when the
        rows were there and only their labels were invisible (issue #76)."""
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        monkeypatch.setattr(
            support,
            "_plex_client",
            lambda _store: SimpleNamespace(
                list_owned_collections=lambda *_a, **_k: [],  # nothing matches by LABEL
                owned_row_surfaces=lambda *_a, **_k: [self._row("", marked=True)],  # but it is there
            ),
        )
        _enable(client)

        body = client.get("/api/support/drift").json()

        assert body["plex_count"] == 0
        assert body["marked_count"] == 1
        assert "lost their label" in body["text"]


ALL_TOOLS = [
    "/api/support/health",
    "/api/support/libraries",
    "/api/support/rows",
    "/api/support/row-schedule",
    "/api/support/jobs",
    "/api/support/clocks",
    "/api/support/database",
    "/api/support/config",
    "/api/support/timeline",
    "/api/support/settings-history",
    "/api/support/connection",
    "/api/support/sharing",
    "/api/support/surfaces",
    "/api/support/drift",
]


class TestEveryToolIsGatedAndFormatted:
    @pytest.mark.parametrize("path", ALL_TOOLS)
    def test_refused_while_support_mode_is_off(self, client, path):
        assert client.get(path).status_code == 403, path

    @pytest.mark.parametrize("path", ALL_TOOLS)
    def test_stamped_terminated_and_narrow_enough_to_paste(self, client, path):
        _enable(client)
        text = client.get(path).json()["text"]
        assert text.startswith("=== Shortlist support ·"), path
        assert text.rstrip().endswith("=== end ==="), path
        for line in text.splitlines():
            assert len(line) <= 76, f"{path}: {line!r}"

    def test_the_bundle_survives_one_tool_failing(self, client, monkeypatch):
        """The bundle is most wanted when something is broken, which is exactly when a tool may
        raise. One bad section must cost its own section and not the file."""
        import shortlist.server.api.support as support

        async def boom(_request):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(support, "drift", boom)
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "THIS SECTION FAILED: RuntimeError: kaboom" in text
        assert "=== Shortlist support · health ===" in text  # the rest still rendered


class TestNoSecretCanReachACopyBlock:
    """Rule 9, enforced centrally rather than trusted to each tool.

    No tool renders a token deliberately — but these blocks quote EXCEPTION MESSAGES, and an HTTP
    client's error carries the URL it called. One library that puts a credential in a query string,
    now or later, would paste it into a chat window.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("GET https://plex.tv/api?X-Plex-Token=abc123 failed", "X-Plex-Token=<redacted>"),
            ("http://pms:32400/x?token=deadbeef", "token=<redacted>"),
            ("http://radarr/api?apikey=zzz&x=1", "apikey=<redacted>"),
            ("...api_key=hunter2...", "api_key=<redacted>"),
            # The quoted, colon and dict-repr forms an HTTP client actually produces. All three
            # slipped through the first pattern, which only matched `key=value` (review 2026-08-05).
            ('BadRequest: (401); X-Plex-Token="abc123"', 'X-Plex-Token="<redacted>"'),
            ("X-Plex-Token: abc123", "X-Plex-Token: <redacted>"),
            ("{'X-Plex-Token': 'sk-abc123'}", "'X-Plex-Token': '<redacted>'"),
            ("api-key=hunter2", "api-key=<redacted>"),
        ],
    )
    def test_credential_shaped_query_params_are_stripped(self, raw, expected):
        from shortlist.server.api.support import _Block

        rendered = _Block("t").line(raw).render()

        assert expected in rendered
        for secret in ("abc123", "deadbeef", "zzz", "hunter2", "sk-abc123"):
            assert secret not in rendered

    def test_the_scrubber_applies_to_labels_and_table_cells_too(self):
        """Applied in `_Block`, not at each call site, so a tool added later cannot forget it."""
        from shortlist.server.api.support import _Block

        rendered = _Block("t").kv("error", "boom at ?token=leaky").table(["a"], [["?apikey=alsoleaky"]], [40]).render()

        assert "leaky" not in rendered and "alsoleaky" not in rendered

    def test_the_json_error_fields_are_scrubbed_too_not_only_the_copy_block(self, client, monkeypatch):
        """Found by architecture review 2026-08-05: `_scrub` was wired into `_Block` alone.

        The page prints `checks[].detail` and `error` on screen verbatim, so a credential in a JSON
        field is just as exposed as one in the block — and the block-only guard read as if it covered
        everything.
        """
        import shortlist.server.api.support as support

        def explode(_store):
            raise RuntimeError("Client error for url 'https://plex.tv/x?X-Plex-Token=SEKRIT'")

        monkeypatch.setattr(support, "_plex_client", explode)
        _enable(client)

        health = client.get("/api/support/health").json()
        libraries = client.get("/api/support/libraries").json()

        assert "SEKRIT" not in json.dumps(health), "a health probe's detail reaches the page raw"
        assert "SEKRIT" not in json.dumps(libraries), "so does an error field"
        assert "SEKRIT" not in health["text"] and "SEKRIT" not in libraries["text"]

    def test_a_failed_plex_read_reports_the_failure_without_the_url_credential(self, client, monkeypatch):
        import shortlist.server.api.support as support

        def explode(_store):
            raise RuntimeError("Client error for url 'https://plex.tv/api/users?X-Plex-Token=SEKRIT'")

        monkeypatch.setattr(support, "_plex_client", explode)
        _enable(client)

        text = client.get("/api/support/libraries").json()["text"]

        assert "SEKRIT" not in text
        assert "COULD NOT READ" in text


class TestSharingCountsLabelsNotClauses:
    """Found by architecture review 2026-08-05.

    `merge_label_excludes` unions EVERY shortlist label into a single `label!=` condition. Splitting
    on `|` and calling a whole clause "ours" therefore (a) counted 2 exclusions on a server hiding
    forty rows, so the tool could not answer "is every row hidden from this person" — the load-bearing
    check now that the automatic Privacy Check is gone — and (b) reported the owner's own `label!=Kids`
    restriction as something Shortlist had added.
    """

    @staticmethod
    def _accounts(monkeypatch, filters_by_user: dict[str, dict[str, str]]):
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        users = [
            SimpleNamespace(username=name, id=1000 + i, filters=filters)
            for i, (name, filters) in enumerate(filters_by_user.items())
        ]
        monkeypatch.setattr(support, "_plextv_client", lambda _store, _mid: SimpleNamespace(list_users=lambda: users))
        monkeypatch.setattr(support, "_machine_id", lambda _session: "m1")

    def test_every_label_is_counted_not_the_clause_it_shares(self, client, monkeypatch):
        # The exact string `merge_label_excludes` produces: one condition, many labels, and a
        # pre-existing foreign label sitting in the same one.
        self._accounts(
            monkeypatch,
            {
                "sarah": {
                    "filterMovies": "label!=Kids,shortlist_mike,shortlist_dan,shortlist_ana",
                    "filterTelevision": "label!=shortlist_mike,shortlist_dan,shortlist_ana",
                }
            },
        )
        # `zoe` exists on the server and is NOT in sarah's filter, so the per-account detail line
        # prints — which is what carries the count being asserted.
        _rows_on_plex(monkeypatch, ["mike", "dan", "ana", "zoe"])
        _enable(client)

        row = client.get("/api/support/sharing").json()["accounts"][0]

        assert row["shortlist_excludes"] == ["shortlist_ana", "shortlist_dan", "shortlist_mike"]
        assert "hides 3 of 4" in client.get("/api/support/sharing").json()["text"]

    def test_a_foreign_label_in_our_clause_is_not_attributed_to_shortlist(self, client, monkeypatch):
        """The owner's own restriction, sharing the condition we merged into. Reporting it as ours
        would send someone hunting for a Shortlist bug in their own configuration."""
        self._accounts(
            monkeypatch,
            {"sarah": {"filterMovies": "label!=Kids,shortlist_mike|contentRating!=R"}},
        )
        _enable(client)

        row = client.get("/api/support/sharing").json()["accounts"][0]

        assert row["shortlist_excludes"] == ["shortlist_mike"]
        assert any("Kids" in c for c in row["other_conditions"]), row["other_conditions"]
        assert any("contentRating" in c for c in row["other_conditions"])

    def test_a_healthy_server_gets_a_summary_not_a_line_per_account(self, client, monkeypatch):
        """Measured on a real 50-user server: printing every account made this section 446 lines of a
        779-line report — 57% of it, saying "fine" fifty times. That pushed the report past what a
        chat message holds and buried the two lines that mattered."""
        self._accounts(
            monkeypatch,
            {name: {"filterMovies": "label!=shortlist_other"} for name in ("a", "b", "c", "d", "e")},
        )
        with client.app.state.sessions() as session:
            # Exactly one person has a row, and every account hides it — so nothing is missing and
            # the block has nothing per-account to say.
            for existing in session.query(User).all():
                existing.enabled = False
            session.add(User(plex_account_id=99, username="other", slug="other", enabled=True))
            session.commit()
        _rows_on_plex(monkeypatch, ["other"])
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["missing_excludes_for"] == []
        assert "5 of 5 accounts hide every row that is not theirs." in body["text"]
        # No per-account detail when there is nothing to say about any of them.
        assert len(body["text"].splitlines()) < 12, body["text"]

    def test_a_person_missing_an_exclusion_is_named_along_with_the_row_they_can_see(self, client, monkeypatch):
        """The question the tool exists for, and it must name the ROW, not just count.

        The expectation comes from OUR roster of enabled users, never from `len(accounts) - 1`. That
        arithmetic is wrong in both directions: the owner has a row but is absent from the plex.tv
        roster (`list_users` returns shared + Home users only), and a disabled user is in the roster
        with no row. Either way the highest-stakes tool in the app would be crying wolf.
        """
        with client.app.state.sessions() as session:
            for slug, account_id in (("mike", 1001), ("dan", 1002)):
                existing = session.query(User).filter(User.slug == slug).one_or_none()
                if existing is None:
                    session.add(User(plex_account_id=account_id, username=slug, slug=slug, enabled=True))
                else:
                    existing.plex_account_id, existing.enabled = account_id, True
            session.query(User).filter(User.slug == "sarah").one().plex_account_id = 1000
            session.commit()

        self._accounts(
            monkeypatch,
            {
                "sarah": {"filterMovies": "label!=shortlist_mike,shortlist_dan"},
                "mike": {"filterMovies": "label!=shortlist_sarah,shortlist_dan"},
                # dan is missing sarah's — a row of hers is visible to him.
                "dan": {"filterMovies": "label!=shortlist_mike"},
            },
        )
        _rows_on_plex(monkeypatch, ["sarah", "mike", "dan"])
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["missing_excludes_for"] == ["dan"]
        dan = next(a for a in body["accounts"] if a["user"] == "dan")
        assert dan["missing"] == ["shortlist_sarah"]
        assert "NOT HIDDEN: shortlist_sarah" in body["text"]
        assert "PROBLEM: these people can see a row that is not theirs: dan" in body["text"]
        # And nobody is told to hide their OWN row — that would hide them from it permanently.
        assert all(f"shortlist_{a['user']}" not in a["should_hide"] for a in body["accounts"])

    def test_an_enabled_user_absent_from_the_plex_roster_still_counts(self, client, monkeypatch):
        """The OWNER is the real case: they have a row, and `list_users` never returns them. Counting
        `len(accounts) - 1` reported a correctly-configured server as short by exactly one."""
        with client.app.state.sessions() as session:
            owner = session.query(User).filter(User.slug == "mike").one_or_none()
            if owner is None:
                owner = User(plex_account_id=999, username="mike", slug="mike")
                session.add(owner)
            owner.enabled, owner.user_type, owner.plex_account_id = True, "owner", 999
            session.query(User).filter(User.slug == "sarah").one().plex_account_id = 1000
            session.commit()

        # Only sarah is in the roster; the owner (mike) is not — but mike has a row.
        self._accounts(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_mike"}})
        _rows_on_plex(monkeypatch, ["mike"])
        _enable(client)

        body = client.get("/api/support/sharing").json()

        sarah = body["accounts"][0]
        assert sarah["should_hide"] == ["shortlist_mike"], "the owner's row must be expected"
        assert sarah["missing"] == []
        assert body["missing_excludes_for"] == [], "a correct server must not be reported as short"

    def test_an_enabled_user_with_no_row_yet_is_not_reported_as_a_leak(self, client, monkeypatch):
        """Issue #76. The engine only excludes labels it FOUND on the PMS, so a user who has never
        received a row (cold start, zero picks, a delivery that failed) has no label in anybody's
        filter — correctly. Expecting one made every single account read as short, and this tool
        told a reporter `0 of 19 accounts hide every row` on a server that was fine. One of their
        servers had 13 of 24 users sitting on picks=0.
        """
        with client.app.state.sessions() as session:
            for slug, account_id in (("mike", 1001), ("ghost", 1002)):
                existing = session.query(User).filter(User.slug == slug).one_or_none()
                if existing is None:
                    session.add(User(plex_account_id=account_id, username=slug, slug=slug, enabled=True))
                else:
                    existing.plex_account_id, existing.enabled = account_id, True
            session.query(User).filter(User.slug == "sarah").one().plex_account_id = 1000
            session.commit()

        # sarah hides mike's row — the only row that exists. `ghost` is enabled but has none.
        self._accounts(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_mike"}})
        _rows_on_plex(monkeypatch, ["mike"])
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["missing_excludes_for"] == [], "a user with no row must not be expected in a filter"
        sarah = next(a for a in body["accounts"] if a["user"] == "sarah")
        assert "shortlist_ghost" not in sarah["should_hide"]
        assert sarah["missing"] == []
        assert "1 of 1 accounts hide every row that is not theirs." in body["text"]

    def test_a_plex_read_that_failed_is_not_reported_as_a_healthy_server(self, client, monkeypatch):
        """The fail-safe half: with no list of rows, every account trivially hides all zero of them.
        Printing `N of N accounts hide every row` off a failed read is the most reassuring possible
        lie, on the tool whose whole job is catching a leak."""
        import shortlist.server.api.support as support

        def boom(_store):
            raise RuntimeError("PMS unreachable")

        self._accounts(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_mike"}})
        monkeypatch.setattr(support, "_plex_client", boom)
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["rows_error"]
        assert "COULD NOT READ THE ROWS ON PLEX" in body["text"]
        assert "hide every row that is not theirs" not in body["text"]

    def test_rows_that_lost_their_labels_are_not_reported_as_nothing_to_hide(self, client, monkeypatch):
        """The state issue #76's reporter was actually in, and the most reassuring possible lie.

        A label read that SUCCEEDS and returns nothing looks identical to a server with no rows. But
        rows that exist without labels are visible to EVERYONE — no `label!=` can match them — so
        printing "there is nothing for anyone to hide" is exactly backwards. The title marker is
        independent of the label, so the two disagreeing says which of the two is true.
        """
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        monkeypatch.setattr(
            support,
            "_plex_client",
            lambda _store: SimpleNamespace(
                owned_collections=lambda _prefix: {},  # no LABELS came back...
                owned_row_surfaces=lambda *_a, **_k: [{"marked": True}, {"marked": True}],  # ...but rows are there
            ),
        )
        self._accounts(monkeypatch, {"sarah": {"filterMovies": ""}})
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["rows_error"], "an empty read beside marked rows is UNKNOWN, not 'nothing to hide'"
        assert "2 collection(s) are ours by title but carry no label" in body["rows_error"]
        assert "no per-person rows exist" not in body["text"].lower()
        assert "COULD NOT READ THE ROWS ON PLEX" in body["text"]

    def test_no_rows_on_the_server_says_so_rather_than_claiming_health(self, client, monkeypatch):
        """A fresh install, or the window right after an uninstall — nothing exists to hide yet."""
        self._accounts(monkeypatch, {"sarah": {"filterMovies": ""}})
        _rows_on_plex(monkeypatch, [])
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["rows_on_plex"] == []
        assert "No per-person rows exist on Plex yet" in body["text"]

    def test_a_shared_row_is_not_something_every_account_must_hide(self, client, monkeypatch):
        """Shared rows are public (or audience-scoped) by design and are deliberately NOT excluded
        from everyone, so counting them here would report every account as leaking one."""
        from types import SimpleNamespace

        import shortlist.server.api.support as support
        from shortlist.engine.models import SHARED_LABEL_PREFIX, OwnedRow

        owned = {
            "mike": OwnedRow(label="shortlist_mike"),
            "_shared_popular": OwnedRow(label=f"{SHARED_LABEL_PREFIX}popular"),
        }
        monkeypatch.setattr(
            support, "_plex_client", lambda _store: SimpleNamespace(owned_collections=lambda _prefix: owned)
        )
        with client.app.state.sessions() as session:
            existing = session.query(User).filter(User.slug == "mike").one_or_none()
            if existing is None:
                session.add(User(plex_account_id=1001, username="mike", slug="mike", enabled=True))
            else:
                existing.plex_account_id, existing.enabled = 1001, True
            session.query(User).filter(User.slug == "sarah").one().plex_account_id = 1000
            session.commit()
        self._accounts(monkeypatch, {"sarah": {"filterMovies": "label!=shortlist_mike"}})
        _enable(client)

        body = client.get("/api/support/sharing").json()

        assert body["rows_on_plex"] == ["shortlist_mike"]
        assert body["missing_excludes_for"] == []

    def test_an_unparseable_filter_is_reported_rather_than_mis_attributed(self, client, monkeypatch):
        """The engine refuses to rewrite a filter it cannot fully represent; this refuses to judge one."""
        self._accounts(monkeypatch, {"sarah": {"filterMovies": "garbage-with-no-operator"}})
        _enable(client)

        row = client.get("/api/support/sharing").json()["accounts"][0]

        assert row["shortlist_excludes"] == []
        assert any("unparseable" in c for c in row["other_conditions"])


class TestTheClientMethodsActuallyExist:
    """The mocks in this file name methods on the real clients. If a mock names one that does not
    exist, every test using it passes against a fiction and the tool 500s (or degrades to "COULD NOT
    READ") only in production.

    That happened: `sharing` called `client.users()` where the real method is `list_users()`. The
    monkeypatched stub had the same wrong name, so the whole class went green while a live check
    reported `AttributeError: 'PlexTvClient' object has no attribute 'users'`.
    """

    def test_every_plex_client_method_the_support_tools_call_is_real(self):
        from shortlist.engine.clients.plex_pms import PlexClient
        from shortlist.engine.clients.plextv import PlexTvClient

        for cls, names in (
            (PlexTvClient, ("list_users", "shared_server_tokens")),
            (PlexClient, ("sections", "list_owned_collections")),
        ):
            for name in names:
                assert hasattr(cls, name), f"{cls.__name__}.{name} does not exist"


class TestTitleGroupingEdgeCases:
    def test_two_unidentified_watched_titles_do_not_merge(self, client):
        """`WatchedTitle.tmdb_id` is nullable, and a bare `or 0` fallback would collapse every
        un-identified row for one person into a single group — the merge-two-titles bug in miniature.
        Unreachable today (the watch cache only stores rows carrying a `tmdb://` guid), asserted
        because the cost of being wrong here is a false verdict."""
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            for rating_key, title in ((11, "Mystery One"), (12, "Mystery Two")):
                session.add(
                    WatchedTitle(
                        user_id=user.id,
                        section_key="1",
                        rating_key=rating_key,
                        tmdb_id=None,
                        media_type="show",
                        title=title,
                        watch_count=1,
                        viewed_leaf_count=1,
                        leaf_count=10,
                    )
                )
            session.commit()
        _enable(client)

        rows = client.get("/api/support/title", params={"q": "Mystery"}).json()["rows"]

        assert sorted(r["title"] for r in rows) == ["Mystery One", "Mystery Two"]
        # And an absent id is reported as absent, not as TMDB id 0.
        assert all(r["tmdb_id"] is None for r in rows)


class TestTimelineShowsSignalNotItsOwnFootprints:
    def test_the_diagnostics_own_read_events_are_excluded(self, client):
        """Found by exercising the tools by hand (2026-08-05).

        Every check writes a `support.read` audit row, so within one support session they outnumbered
        everything else and the tool that answers "what has been happening" showed nothing but the
        fact that someone had been looking. The audit rows still exist — they are just not what this
        question is asking.
        """
        _enable(client)
        for path in ("/api/support/health", "/api/support/rows", "/api/support/jobs"):
            client.get(path)

        body = client.get("/api/support/timeline").json()

        assert body["entries"], "the timeline came back empty"
        assert all("support.read" not in e["what"] for e in body["entries"]), body["entries"]
        # Switching the mode IS a state change, and rare — it stays.
        assert any("support.enable" in e["what"] for e in body["entries"])

    def test_an_event_reads_as_words_not_a_python_dict(self, client):
        """`str(message)` renders `{'user': 'sarah'}`, which is noise to someone who has never seen
        this app's internals."""
        with client.app.state.sessions() as session:
            session.add(Event(scope="privacy.sync", level="info", message={"user": "sarah", "added": 3}))
            session.commit()
        _enable(client)

        entries = client.get("/api/support/timeline").json()["entries"]

        privacy = next(e for e in entries if "privacy.sync" in e["what"])
        assert privacy["what"] == "privacy.sync user=sarah", privacy["what"]


class TestSuggestions:
    """Type-ahead for the checks that take a name. A username typed from memory is the commonest way
    a check comes back empty — and empty is indistinguishable from "nothing is wrong"."""

    def test_offers_every_person_and_the_titles_worth_asking_about(self, client):
        _seed_teacup(client.app, viewed=2, total=8, delivered=True)
        _enable(client)

        body = client.get("/api/support/suggestions").json()

        assert "sarah" in [p["slug"] for p in body["people"]]
        assert "Teacup" in body["titles"]

    def test_disabled_people_are_offered_too_but_ranked_after_the_enabled_ones(self, client):
        """A disabled person is a legitimate thing to ask about — "why did their row vanish" — so
        excluding them would hide the answer. Enabled first, because that is the common case."""
        _enable(client)

        people = client.get("/api/support/suggestions").json()["people"]

        assert {p["slug"] for p in people} >= {"sarah", "mike"}
        assert people[0]["enabled"] is True
        assert any(p["enabled"] is False for p in people), "mike ships disabled in the fixture"

    def test_it_needs_no_plex_connection(self, client, monkeypatch):
        """It populates the inputs of the checks used WHEN PLEX IS DOWN, so it must not need Plex."""
        import shortlist.server.api.support as support

        def explode(_store):
            raise RuntimeError("PMS unreachable")

        monkeypatch.setattr(support, "_plex_client", explode)
        _enable(client)

        assert client.get("/api/support/suggestions").status_code == 200


class TestTheReportCarriesWhatABugReportActuallyNeeds:
    """The question that prompted these: does the downloadable report let someone else troubleshoot?

    It used to be a configuration-and-state snapshot with NO log lines and no run detail — so the two
    things a maintainer asks for first ("what's the error?", "which run failed?") were exactly what it
    left out.
    """

    def test_the_bundle_includes_recent_errors_and_recent_runs(self, client):
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "=== Shortlist support · recent warnings and errors ===" in text
        assert "=== Shortlist support · recent runs ===" in text
        assert "THIS SECTION FAILED" not in text

    def test_a_logged_error_reaches_the_report(self, client, tmp_path):
        """Read through `log_reader`, which redacts with `http_retry.redact` — a wider net than this
        module's own scrubber."""
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            "2026-08-05 10:00:00.000 | ERROR    | shortlist.engine.pipeline:run:1 - "
            "boom talking to http://pms:32400/x?X-Plex-Token=SEKRIT\n"
        )
        _enable(client)

        body = client.get("/api/support/errors").json()

        assert body["total_matched"] >= 1
        assert "boom talking to" in body["text"]
        assert "SEKRIT" not in body["text"], "the log reader must have redacted this"

    def test_a_failed_run_names_the_person_and_the_error(self, client):
        """ "A run failed" is not actionable; "it failed for sarah, with this error" is."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            run = Run(trigger="schedule", status="error")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=user.id, status="error", error="no token for sarah"))
            session.commit()
        _enable(client)

        body = client.get("/api/support/runs").json()

        assert body["runs"][0]["failed"] == [{"user": "sarah", "error": "no token for sarah"}]
        assert "FAILED sarah: no token for sarah" in body["text"]

    def test_a_missing_log_directory_degrades_instead_of_500ing(self, client):
        """A fresh install has no log file yet, and this must not be the thing that breaks."""
        _enable(client)

        body = client.get("/api/support/errors").json()

        assert body["text"].rstrip().endswith("=== end ===")


class TestTheDownloadCarriesTheLogsToo:
    """The text report is capped so it can be pasted; the download is for when that is not enough.

    Asking someone to then find the Logs page and export separately is a step that does not happen,
    so the one button gives them everything.
    """

    def test_the_zip_holds_the_report_and_the_log_files(self, client):
        import io
        import zipfile

        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text("2026-08-05 10:00:00.000 | INFO | a:b:1 - hello\n")
        _enable(client)

        response = client.get("/api/support/report.zip")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "shortlist-report.zip" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            assert "shortlist-report.txt" in names
            assert any(n.startswith("logs/") for n in names), names
            report = archive.read("shortlist-report.txt").decode()
            assert report.startswith("=== Shortlist support")

    def test_the_logs_in_the_zip_are_redacted(self, client):
        """An export is the single most likely thing to end up in a public issue tracker."""
        import io
        import zipfile

        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            "2026-08-05 10:00:00.000 | ERROR | a:b:1 - GET /x?X-Plex-Token=SUPERSECRET failed\n"
        )
        _enable(client)

        with zipfile.ZipFile(io.BytesIO(client.get("/api/support/report.zip").content)) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())

        assert b"SUPERSECRET" not in blob

    def test_an_unreadable_log_directory_still_produces_a_zip(self, client, monkeypatch):
        """The download is most wanted when something is broken; the logs failing must cost the logs
        and not the report."""
        import io
        import zipfile

        from shortlist.server.services import log_reader

        monkeypatch.setattr(log_reader, "build_zip", lambda *_a: (_ for _ in ()).throw(OSError("disk gone")))
        _enable(client)

        with zipfile.ZipFile(io.BytesIO(client.get("/api/support/report.zip").content)) as archive:
            assert "shortlist-report.txt" in archive.namelist()
            assert "logs/UNAVAILABLE.txt" in archive.namelist()
            assert b"disk gone" in archive.read("logs/UNAVAILABLE.txt")

    def test_the_report_includes_detail_for_the_people_it_flagged(self, client):
        """The report answers the follow-up it has just invited. Without this, "here is the server, now
        ask me about each of 46 people" is a round trip — and a round trip with a non-technical
        reporter costs a day."""
        from shortlist.server.db.models import Run, RunUser

        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            run = Run(trigger="schedule", status="error")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=user.id, status="error", error="no token"))
            session.commit()
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "=== Shortlist support · person ===" in text
        assert "person        sarah" in text

    def test_a_healthy_server_gets_no_per_person_sections(self, client, monkeypatch):
        """A clean report stays short — otherwise the flagged sections stop meaning anything."""
        import shortlist.server.api.support as support

        async def clean(_request):
            return {"users": [], "problems": [], "error": None, "text": "=== Shortlist support · x ===\n=== end ==="}

        monkeypatch.setattr(support, "connection", clean)
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "=== Shortlist support · person ===" not in text


class TestWhatTheReportDiscloses:
    """The report is destined for a PUBLIC issue tracker. What is in it is a privacy decision.

    Found by auditing an actual report rather than trusting the copy beside the button: it named every
    person on the server by their Plex username and printed the server's address verbatim. "No
    passwords or tokens" was true, and misleading — it sat next to a button that posts publicly.
    """

    def test_the_server_address_is_reduced_to_a_shape(self, client):
        """A bare `http://172.16.10.240:32400` hands over someone's LAN topology, and a `plex.direct`
        hostname embeds their server's machine id. Scheme and port are the only diagnostic parts."""
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://172.16.10.240:32400")
            store.set("tautulli.url", "https://tautulli.mydomain.example:8181")
            session.commit()
        _enable(client)

        body = client.get("/api/support/config").json()

        assert "172.16.10.240" not in body["text"]
        assert "tautulli.mydomain.example" not in body["text"]
        assert "http://<host>:32400" in body["text"], body["text"]
        assert "https://<host>:8181" in body["text"]

    def test_the_report_names_people_and_the_copy_beside_it_says_so(self, client):
        """Name-hiding was REMOVED at the owner's request (2026-08-06): a person who wants names out
        can take them out, and the tickbox governed only the report — not the per-check Copy buttons
        beside it — so it read like a page-wide privacy setting it never was. What matters now is that
        the page does not imply otherwise, which `issue-page.test.tsx` pins on the copy."""
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            "2026-08-05 03:31:02.000 | WARNING | a:b:1 - watch cache: sarah section 2 failed\n"
        )
        _seed_teacup(client.app, viewed=None, total=None, delivered=True)
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "sarah" in text
        assert "names hidden" not in text, "no anonymised mode exists to announce"

    def test_the_zip_does_not_carry_this_servers_machine_id_in_its_logs(self, client):
        """Found by a positive-control audit of a real 88 MB report: the machine id was still in two of
        the zipped log files, URL-encoded as `uri=server%3A%2F%2F<id>%2F…`.

        `report_zip` shaped hosts itself instead of letting `build_zip` do all of the redaction, and
        that private copy lacked the known-literal pass — so the only thing standing between the id and
        the zip was a `\\b` pattern that cannot match after the `F` of a `%2F`.
        """
        import io
        import zipfile

        machine_id = "7ee8abc1bcdcc79389ad1e15c30e2692714bc940"
        with client.app.state.sessions() as session:
            session.query(Server).update({"machine_id": machine_id, "url": "http://172.16.10.240:32400"})
            session.commit()
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        # Both escapings: `%2F` is what a real log carries and the pattern now catches it, `%252F` is
        # reachable ONLY by the literal pass — so this cannot pass with either half missing.
        (logs / "shortlist.log").write_text(
            f"2026-08-05 03:31:02.000 | DEBUG | a:b:1 - PUT /library/collections/9"
            f"?type=1&uri=server%3A%2F%2F{machine_id}%2Fcom.plexapp\n"
            f"2026-08-05 03:31:03.000 | DEBUG | a:b:1 - retry uri=server%253A%252F%252F{machine_id}%252Fcom\n"
            "2026-08-05 03:31:04.000 | DEBUG | a:b:1 - GET 172.16.10.240 -> 200 in 0.03s\n"
        )
        _enable(client)

        with zipfile.ZipFile(io.BytesIO(client.get("/api/support/report.zip").content)) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())

        assert machine_id.encode() not in blob, "machine id survived"
        assert b"172.16.10.240" not in blob, "address survived"
        assert b"<machine-id>" in blob

    def test_the_errors_check_shapes_addresses_in_its_json_not_only_its_text(self, client):
        """`read_lines` serves the live Logs view, which is allowed to show the owner their own
        address. The same lines reaching a bug report are not, and the JSON `lines` field was the one
        part of this response nothing had shaped."""
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            "2026-08-05 03:31:02.000 | ERROR | a:b:1 - host='172.16.10.240', port=32400 unreachable\n"
        )
        _enable(client)

        body = client.get("/api/support/errors").json()

        assert "172.16.10.240" not in body["text"]
        assert "172.16.10.240" not in json.dumps(body["lines"]), body["lines"]
        assert "<host>" in json.dumps(body["lines"])

    def test_the_plain_log_download_carries_the_same_guarantee(self, client):
        """`/api/system/logs/download` is the OTHER export whose docstring calls it "the attachment for
        a bug report", and it had only the credential pass — so it shipped the machine id and every one
        of the tens of thousands of addresses a log file accumulates."""
        import io
        import zipfile

        machine_id = "7ee8abc1bcdcc79389ad1e15c30e2692714bc940"
        with client.app.state.sessions() as session:
            session.query(Server).update({"machine_id": machine_id, "url": "http://172.16.10.240:32400"})
            session.commit()
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            f"2026-08-05 03:31:02.000 | DEBUG | a:b:1 - uri=server%253A%252F%252F{machine_id}%252Fcom\n"
            "2026-08-05 03:31:03.000 | DEBUG | a:b:1 - GET 172.16.10.240 -> 200\n"
        )

        with zipfile.ZipFile(io.BytesIO(client.get("/api/system/logs/download").content)) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())

        assert machine_id.encode() not in blob
        assert b"172.16.10.240" not in blob


class TestNoDisplayNameReachesTheReport:
    """The report renders SLUGS, never the display names an owner types.

    A slug is the identifier the privacy system already keys on and it appears in Plex as
    `shortlist_<slug>`, so a maintainer can act on it. A nickname or friendly name is free text and
    routinely someone's real name — "Aaron Shays Partner" on the maintainer's own server — which
    identifies a person far beyond what the report needs. Checked there (2026-08-05: 47 such names
    existed, none appeared); this pins it so a future check that prints a display name fails here
    rather than in someone's public GitHub issue.
    """

    def test_a_nickname_never_appears_in_the_report(self, client):
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            user.nickname = "Zaphod"
            user.friendly_name = "Beeblebrox"
            session.commit()
        _seed_teacup(client.app, viewed=None, total=None, delivered=True)
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        for name in ("Zaphod", "Beeblebrox"):
            assert name not in text, f"{name} reached the report — render the slug instead"


class TestTheFindingsFromTheFifthReviewPass:
    """Five defects an architecture review found that four earlier passes — including my own by-hand
    checks — did not. Each is pinned here with the scenario that produced it."""

    def test_the_server_address_never_reaches_the_report_via_an_exception(self, client, monkeypatch):
        """HIGH. `config` shaped the settings it printed, and the test only checked `config` — but the
        same address arrives in every quoted exception. "My Plex is unreachable" is the single most
        likely line in a support report, and it printed `host='172.16.10.240', port=32400` verbatim.
        """
        import shortlist.server.api.support as support

        def explode(_store):
            raise RuntimeError(
                "HTTPConnectionPool(host='172.16.10.240', port=32400): "
                "Max retries exceeded with url: https://192-168-1-5.abc123def456abc123def456abc12345.plex.direct:32400/x"
            )

        monkeypatch.setattr(support, "_plex_client", explode)
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "172.16.10.240" not in text
        assert "192-168-1-5" not in text
        assert "abc123def456abc123def456abc12345" not in text, "a plex.direct name embeds the machine id"
        assert "<host>" in text

    def test_a_machine_id_in_a_plextv_path_is_hidden(self):
        """The other form: `plex.tv/api/servers/<32-hex>/shared_servers`."""
        from shortlist.server.api.support import _scrub

        out = _scrub("HTTPError: 500 for https://plex.tv/api/servers/a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6/shared_servers")

        assert "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6" not in out
        assert "<machine-id>" in out

    def test_the_scheme_and_port_survive_because_they_are_diagnostic(self):
        from shortlist.server.api.support import _scrub

        assert _scrub("GET https://pms.example:32400/library") == "GET https://<host>:32400/library"

    def test_a_share_added_since_the_last_sync_still_reaches_the_report(self, client, monkeypatch):
        """`sharing` prints usernames from the LIVE plex.tv roster, so a share added since the last
        user sync is absent from our `users` table — and is GUARANTEED to be flagged, because with no
        excludes yet it can see everyone's rows. It must still appear, which is the whole point."""
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        roster = [SimpleNamespace(username="NewFriendBob", id=987654, filters={"filterMovies": ""})]
        monkeypatch.setattr(support, "_plextv_client", lambda _s, _m: SimpleNamespace(list_users=lambda: roster))
        monkeypatch.setattr(support, "_machine_id", lambda _s: "m1")
        # A row has to EXIST for the new share to be leaking one — that is what flags them.
        _rows_on_plex(monkeypatch, ["mike"])
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "NewFriendBob" in text

    def test_a_privacy_fault_yields_a_person_section_when_username_differs_from_slug(self, client, monkeypatch):
        """HIGH. `sharing` returned usernames; `person()` keys on slugs, and `slugify` lowercases and
        replaces punctuation — so they differ for essentially every real Plex account. The per-person
        detail 404'd for exactly the people with a privacy fault."""
        from types import SimpleNamespace

        import shortlist.server.api.support as support

        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            user.username, user.plex_account_id, user.enabled = "Sarah_Plex", 4242, True
            for other in session.query(User).filter(User.slug != "sarah").all():
                other.enabled = False
            session.add(User(plex_account_id=99, username="Other", slug="other", enabled=True))
            session.commit()

        roster = [SimpleNamespace(username="Sarah_Plex", id=4242, filters={"filterMovies": ""})]
        monkeypatch.setattr(support, "_plextv_client", lambda _s, _m: SimpleNamespace(list_users=lambda: roster))
        monkeypatch.setattr(support, "_machine_id", lambda _s: "m1")
        # `other`'s row exists on the server, so Sarah_Plex's empty filter really is a fault.
        _rows_on_plex(monkeypatch, ["other"])
        _enable(client)

        sharing_body = client.get("/api/support/sharing").json()
        text = client.get("/api/support/bundle.txt").text

        assert sharing_body["missing_excludes_for"] == ["Sarah_Plex"], "the block shows the username"
        assert sharing_body["missing_excludes_slugs"] == ["sarah"], "the machine-readable field is a slug"
        assert "THIS SECTION FAILED" not in text, text[-600:]
        assert "person        sarah" in text, "the per-person section must actually render"

    def test_the_zip_is_built_off_the_event_loop(self, client):
        """LOW. Tens of megabytes of unzip + regex + rezip inline stalled the SSE stream, the UI and
        Docker's HEALTHCHECK — on the page for a server that is already struggling."""
        import inspect

        import shortlist.server.api.support as support

        assert "run_in_threadpool" in inspect.getsource(support.report_zip)


class TestKnownIdentifiersAreRedactedAsLiterals:
    """Pattern-matching missed this server's machine id three times.

    The last miss was a URL-encoded log line — `uri=server%3A%2F%2F<id>%2F…` — where `\\b` cannot match
    because the `F` of `%2F` is itself a hex character. Found by a positive-control audit against the
    real server: enumerate every secret that exists, then assert none appears. The exact values are
    not a guess, so they are redacted as literals; the regexes are only the net for ids belonging to
    something else.
    """

    MACHINE = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    def _link(self, client):
        from shortlist.server.db.models import Server

        with client.app.state.sessions() as session:
            server = session.query(Server).first()
            server.machine_id = self.MACHINE
            server.url = "http://172.16.10.240:32400"
            session.commit()

    def test_a_url_encoded_machine_id_is_redacted(self, client, monkeypatch):
        """The exact form that escaped: percent-escapes either side, no word boundary available."""
        self._link(client)
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            f"2026-08-05 03:31:02.000 | WARNING | a:b:1 - PUT /x?uri=server%3A%2F%2F{self.MACHINE}%2Fcom.plexapp\n"
        )
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert self.MACHINE not in text
        assert "<machine-id>" in text

    def test_a_bare_host_in_a_log_line_is_redacted(self, client):
        """`http_retry` logs every PMS call as a bare host with no scheme — tens of thousands of lines
        on a real server, and 17,234 of them leaked past the scheme-based pattern."""
        self._link(client)
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text(
            "2026-08-05 03:31:02.000 | WARNING | http_retry:_send:134 - GET 172.16.10.240 -> 500 in 0.05s\n"
        )
        _enable(client)

        text = client.get("/api/support/bundle.txt").text

        assert "172.16.10.240" not in text
        assert "<host>" in text

    def test_a_version_string_is_not_mistaken_for_an_address(self, client):
        """`1.43.3.10861` must survive — over-redaction that eats the PMS version costs a diagnostic."""
        from shortlist.server.api.support import _scrub

        assert _scrub("PMS 1.43.3.10861-07dfddaeb ready") == "PMS 1.43.3.10861-07dfddaeb ready"

    def test_the_gate_populates_the_literals_so_no_tool_can_forget(self):
        """Set once on `require_support_mode`, so a tool added later inherits it."""
        import inspect

        import shortlist.server.api.support as support

        assert "_KNOWN.set" in inspect.getsource(support.require_support_mode)


class TestNeverReadIsNotStatedAsAFault:
    """Run on the maintainer's own healthy 46-user server and it flagged two people as a PROBLEM.

    Their only "problem" was a Sports library that had never been shared with them — correct
    configuration. And an unshared library is indistinguishable from a failing one at this layer:
    `watch_sync` skips a 403 without recording state, and `force_full_next_time` only touches a row
    that already exists, so neither leaves a trace. A check that cannot tell them apart must not claim
    one; it points at the log, which can.
    """

    def test_the_connection_check_states_a_fact_and_names_the_ambiguity(self, client):
        _enable(client)

        text = client.get("/api/support/connection").json()["text"]

        assert "have a library we have never read" in text
        assert "not shared with them" in text
        assert "What errors has it logged?" in text
        assert "PROBLEM:" not in text, "it cannot prove a fault, so it must not assert one"

    def test_a_person_with_an_unread_library_is_reported_the_same_way(self, client):
        with client.app.state.sessions() as session:
            user = session.query(User).filter(User.slug == "sarah").one()
            session.add(
                WatchedTitle(user_id=user.id, section_key="7", rating_key=1, tmdb_id=5, media_type="show", title="X")
            )
            session.commit()
        _enable(client)

        text = client.get("/api/support/person/sarah").json()["text"]

        assert "NEVER READ for this person" in text
        assert "not shared with them" in text
