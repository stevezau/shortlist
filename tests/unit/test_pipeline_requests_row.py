"""The requests row in the pipeline, and tag ambiguity across every enabled requests row."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import rows as rows_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    row_marker,
)
from shortlist.engine.models import (
    ArrTarget,
    MediaType,
    RequestSources,
    RowSpec,
    SeerrTarget,
    UserProfile,
    UserRunReport,
)
from shortlist.engine.requests_row import RequestedTitle, RequestLedger
from tests.conftest import fake_media_item, make_profile, plextv_user
from tests.unit.pipeline_support import ctx  # noqa: F401


def _run_one(ctx: EngineContext, mock_plextv, profile) -> UserRunReport:
    """One person through the real pipeline with the rows the test configured, returning their report."""
    mock_plextv.users = [plextv_user(profile.plex_account_id, profile.username)]
    report = pipeline_mod.run(ctx, [profile])
    return report.users[0]


def _requests_spec(slug: str = "asked", **kw) -> RowSpec:
    return RowSpec(slug=slug, name_template="{library_name} you asked for", size=5, requests_row=True, **kw)


def _ledger(
    *tmdb_ids: int, complete: bool = True, person: int = 100, media_type: MediaType = MediaType.MOVIE
) -> RequestLedger:
    at = datetime(2026, 9, 27, tzinfo=UTC)
    return RequestLedger(
        titles=[
            RequestedTitle(
                tmdb_id=t,
                media_type=media_type,
                plex_account_id=person,
                requested_at=at,
                landed_at=at,
                on_disk=True,
                seasons_landed=True,
                found_in=("overseerr",),
            )
            for t in tmdb_ids
        ],
        complete=complete,
    )


def _by_username_row() -> RowSpec:
    return RowSpec(
        slug="asked-u",
        name_template="{library_name} you asked for",
        size=5,
        requests_row=True,
        requests_tag_pattern="req-{username}",
    )


def _by_name_row() -> RowSpec:
    return RowSpec(
        slug="asked-n",
        name_template="{library_name} requested by name",
        size=5,
        requests_row=True,
        requests_tag_pattern="req-{name}",
    )


class TestRequestsRow:
    """The requests row is built from the ledger, never gathered, and is the one row kind that removes
    its own collection when the person has nothing ready — gated on a COMPLETE source read."""

    def test_a_requests_row_delivers_the_ledger_and_never_gathers(self, ctx: EngineContext, mock_plextv, mock_tmdb):
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(10, 20)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        assert sorted(p.tmdb_id for p in report.picks) == [10, 20]
        assert all(p.sources == ["requests"] for p in report.picks)
        assert all(p.collection_slug == "asked" for p in report.picks)
        mock_tmdb.suggestions.assert_not_called()
        decisions = {entry["decision"] for entry in report.trace["selection"]}
        assert decisions == {"requests"}

    @staticmethod
    def _seed_server(ctx: EngineContext) -> tuple[object, object]:
        """The person's requests row already on Plex, beside a SIBLING row under the same label.

        Real `remove_row`, real `find_owned_collections`: the sibling is what a title-blind delete
        would take with it, and the tests below assert it is left alone.
        """
        ctx.plex.sections.return_value[0].key = "1"
        existing = fake_media_item(4242, "Movies you asked for" + row_marker(100))
        sibling = fake_media_item(4343, "✨ Movies Picked for You" + row_marker(100))
        ctx.plex.find_owned_collections.return_value = [existing, sibling]
        return existing, sibling

    def test_an_empty_requests_row_is_removed_when_the_read_was_complete(self, ctx: EngineContext, mock_plextv):
        existing, _sibling = self._seed_server(ctx)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=True)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is existing
        assert report.removed_deliveries == [{"row_slug": "asked", "library_key": "1"}]
        assert report.diff.deleted == ["Movies you asked for"]
        assert report.picks == []

    def test_a_dry_run_reports_the_removal_and_deletes_nothing(self, ctx: EngineContext, mock_plextv):
        """Rule 8 covers this DELETE too: previewed in the diff, nothing written, nothing forgotten."""
        self._seed_server(ctx)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, dry_run=True)
        ctx.request_ledger = _ledger(complete=True)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.diff.deleted == ["Movies you asked for"]
        assert report.removed_deliveries == []

    @staticmethod
    def _no_picks_lines(run) -> list[str]:
        from loguru import logger as loguru_logger

        lines: list[str] = []
        sink = loguru_logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            run()
        finally:
            loguru_logger.remove(sink)
        return [line for line in lines if "no picks produced" in line]

    def test_the_no_picks_line_says_what_was_removed_when_the_run_removed_a_row(self, ctx: EngineContext, mock_plextv):
        """ "Left as they are" was false for a person whose row this very run had just deleted."""
        self._seed_server(ctx)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=True)

        found = self._no_picks_lines(lambda: _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100)))

        assert len(found) == 1
        assert "removed 1 row(s) this run; any other rows are left as they are" in found[0]
        assert "existing rows are left as they are" not in found[0]

    def test_the_no_picks_line_says_would_remove_on_a_dry_run(self, ctx: EngineContext, mock_plextv):
        self._seed_server(ctx)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, dry_run=True)
        ctx.request_ledger = _ledger(complete=True)

        found = self._no_picks_lines(lambda: _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100)))

        assert len(found) == 1
        assert "would remove 1 row(s) this run; any other rows are left as they are" in found[0]

    def test_the_no_picks_line_counts_rows_the_unhideable_sweep_removed(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """The sweep's removals join the person's diff only after the line is logged."""

        def sweep(*args, deleted, **kwargs):
            deleted["sarah"] = ["Broken row"]

        monkeypatch.setattr(pipeline_mod, "sweep_broken_rows", sweep)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=True)

        found = self._no_picks_lines(lambda: _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100)))

        assert len(found) == 1
        assert "removed 1 row(s) this run; any other rows are left as they are" in found[0]

    def test_the_no_picks_line_keeps_its_wording_when_nothing_was_removed(self, ctx: EngineContext, mock_plextv):
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=True)

        found = self._no_picks_lines(lambda: _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100)))

        assert len(found) == 1
        assert "no picks produced — existing rows are left as they are (history=" in found[0]

    def test_an_empty_requests_row_is_left_alone_when_the_read_was_incomplete(self, ctx: EngineContext, mock_plextv):
        self._seed_server(ctx)
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=False)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.removed_deliveries == []
        assert not (report.diff and report.diff.deleted)

    def test_the_empty_library_is_removed_while_the_other_is_delivered(self, ctx: EngineContext, mock_plextv):
        """Per LIBRARY: a person whose only ready request is a show loses the Movies copy and gets TV."""
        existing, _sibling = self._seed_server(ctx)
        movies = ctx.plex.sections.return_value[0]
        shows = MagicMock()
        shows.type, shows.title, shows.key = "show", "TV Shows", "2"
        shows.collections.return_value = []
        ctx.plex.sections.return_value = [movies, shows]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        ctx.plex.build_library_index.side_effect = lambda s: (
            {900: 999, 10: 1010, 20: 1020} if s is movies else {30: 2030}
        )
        on_server = ctx.plex.find_owned_collections.return_value
        ctx.plex.find_owned_collections.side_effect = lambda section, label: on_server if section is movies else []
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(30, media_type=MediaType.SHOW)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is existing
        assert report.removed_deliveries == [{"row_slug": "asked", "library_key": "1"}]
        assert [(p.tmdb_id, p.rating_key) for p in report.picks] == [(30, 2030)]
        assert ctx.plex.create_collection.call_args.args[0] is shows

    def test_a_row_whose_every_title_is_hidden_from_them_is_removed(self, ctx: EngineContext, mock_plextv):
        """Ready titles their Plex restrictions hide are not in the row, so the row is empty — and gone."""
        existing, _sibling = self._seed_server(ctx)
        ctx.token_for_user = lambda user: "their-token"
        ctx.plex.visible_to.return_value = set()
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(10, 20, complete=True)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is existing
        assert report.picks == []
        results = {r["tmdb_id"]: r["result"] for r in report.trace["selection"][0]["requests"]}
        assert results == {10: "hidden", 20: "hidden"}

    def test_a_requests_row_beside_a_picked_row_leaves_the_picked_row_to_gather(
        self, ctx: EngineContext, mock_plextv, mock_tmdb
    ):
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec(), RowSpec(slug="picked", name_template="Picked for You", size=5)],
            rows_defined=True,
        )
        ctx.request_ledger = _ledger(10)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        by_row: dict[str, list[int]] = {}
        for pick in report.picks:
            by_row.setdefault(pick.collection_slug, []).append(pick.tmdb_id)
        assert by_row["asked"] == [10]
        assert sorted(by_row["picked"]) == [10, 20]
        assert mock_tmdb.suggestions.call_count == 1
        assert report.status == "ok"

    def test_a_cold_person_still_gets_their_requests_row(self, ctx: EngineContext, mock_plextv, mock_tmdb):
        ctx.history_source.fetch.return_value = []  # below min_history
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, cold_start="skip")
        ctx.request_ledger = _ledger(10)

        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))

        assert [p.tmdb_id for p in report.picks] == [10]
        mock_tmdb.suggestions.assert_not_called()
        ctx.plex.top_rated.assert_not_called()

    def test_the_pipeline_builds_the_ledger_once_when_a_requests_row_exists(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        calls: list[frozenset[str]] = []
        monkeypatch.setattr(
            pipeline_mod, "collect_requests", lambda sources, people, **kw: calls.append(kw["patterns"]) or _ledger(10)
        )
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec(requests_tag_pattern="req-{username}"), _requests_spec(slug="asked-2")],
            rows_defined=True,
            request_sources=RequestSources(overseerr=SeerrTarget(url="http://s", api_key="k")),
        )
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        assert calls == [frozenset({"req-{username}"})]
        assert ctx.request_ledger is not None and [t.tmdb_id for t in ctx.request_ledger.titles] == [10]

    def test_no_sources_means_no_ledger_and_no_row(self, ctx: EngineContext, mock_plextv, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "collect_requests", lambda *a, **kw: pytest.fail("read with no source"))
        monkeypatch.setattr(rows_mod, "remove_row", lambda *a, **kw: pytest.fail("removed with no source"))
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, request_sources=None)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert ctx.request_ledger is None
        assert report.users[0].picks == []

    def test_a_run_scoped_to_another_row_reads_no_sources_and_removes_nothing(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """A requests row that is not due tonight (another row's own cron) is neither built nor removed:
        `_run_user` drops not-due rows before its requests branch, and the ledger is not even read."""
        monkeypatch.setattr(pipeline_mod, "collect_requests", lambda *a, **kw: pytest.fail("read for a row not due"))
        self._seed_server(ctx)
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec(), RowSpec(slug="picked", name_template="Picked for You", size=5)],
            rows_defined=True,
            request_sources=RequestSources(overseerr=SeerrTarget(url="http://s", api_key="k")),
            build_only=frozenset({"picked"}),
        )
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert ctx.request_ledger is None
        assert report.users[0].rows_considered["asked"] == "not_due"
        assert {p.collection_slug for p in report.users[0].picks} == {"picked"}
        ctx.plex.delete_owned_collection.assert_not_called()

    def test_the_ledger_is_read_over_the_whole_roster_not_the_users_in_scope(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Two people typed the same tag. A run scoped to ONE of them must not credit that person with
        the other's requests: the ledger reads the full roster, so the tag stays ambiguous."""
        real_collect = pipeline_mod.collect_requests
        radarr = MagicMock()
        radarr.app_name = "Radarr"
        radarr.tags.return_value = {7: "children"}
        radarr.movies.return_value = [{"tmdbId": 10, "tags": [7], "hasFile": True, "movieFile": {}, "title": "A"}]
        monkeypatch.setattr(
            pipeline_mod,
            "collect_requests",
            lambda sources, people, **kw: real_collect(sources, people, radarr=radarr, **kw),
        )
        sarah = make_profile("sarah", account_id=100, requested_by_tag="children")
        kid = make_profile("kid", account_id=101, requested_by_tag="children")
        ctx.roster = [sarah, kid]
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec()],
            rows_defined=True,
            request_sources=RequestSources(
                radarr=ArrTarget(url="http://r", api_key="k", quality_profile_id=0, root_folder="", tag="shortlist")
            ),
        )
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "kid")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].picks == []
        assert ctx.request_ledger is not None and ctx.request_ledger.titles == []
        assert [(m.label, m.ambiguous) for m in ctx.request_ledger.tag_matches] == [("children", True)]

    def test_without_a_roster_the_users_in_scope_are_the_roster(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A direct engine caller passes no roster; `users` doubles as it."""
        seen: list[list[str]] = []
        monkeypatch.setattr(
            pipeline_mod,
            "collect_requests",
            lambda sources, people, **kw: seen.append([p.username for p in people]) or _ledger(),
        )
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec()],
            rows_defined=True,
            request_sources=RequestSources(overseerr=SeerrTarget(url="http://s", api_key="k")),
        )
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert seen == [["sarah"]]


class TestTagAmbiguityAcrossEveryEnabledRequestsRow:
    """A requester tag is judged against the pattern of every ENABLED requests row, due tonight or not.

    Judged against the due rows alone, a night when one of two overlapping rows ran by itself handed
    `req-bob` to whoever that row's pattern names — the wrong-person outcome the cross-pattern check
    exists to stop. Which rows RECEIVE titles is unchanged: only the rows being built.
    """

    @staticmethod
    def _alice_nicknamed_bob_and_bob() -> list[UserProfile]:
        return [
            make_profile("alice", account_id=101, nickname="bob"),
            make_profile("bob", account_id=102, nickname="Robert"),
        ]

    @staticmethod
    def _run(
        ctx: EngineContext,
        mock_plextv: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        *,
        roster: list[UserProfile],
        users: list[UserProfile],
        rows: list[RowSpec],
        build_only: frozenset[str] | None = None,
    ) -> dict[tuple[str, str], list[int]]:
        """One night with `req-bob` on movie 10 in Radarr; ``{(username, row slug): picked tmdb ids}``."""
        real_collect = pipeline_mod.collect_requests
        radarr = MagicMock()
        radarr.app_name = "Radarr"
        radarr.tags.return_value = {7: "req-bob"}
        radarr.movies.return_value = [{"tmdbId": 10, "tags": [7], "hasFile": True, "movieFile": {}, "title": "A"}]
        monkeypatch.setattr(
            pipeline_mod,
            "collect_requests",
            lambda sources, people, **kw: real_collect(sources, people, radarr=radarr, **kw),
        )
        ctx.roster = roster
        ctx.config = replace(
            ctx.config,
            rows=rows,
            rows_defined=True,
            build_only=build_only,
            request_sources=RequestSources(
                radarr=ArrTarget(url="http://r", api_key="k", quality_profile_id=0, root_folder="", tag="shortlist")
            ),
        )
        mock_plextv.users = [plextv_user(p.plex_account_id, p.username) for p in roster]

        report = pipeline_mod.run(ctx, users)

        return {
            (u.username, spec.slug): [p.tmdb_id for p in u.picks if p.collection_slug == spec.slug]
            for u in report.users
            for spec in rows
        }

    def test_a_tag_two_enabled_rows_render_for_two_people_goes_to_nobody_when_only_one_row_is_due(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        people = self._alice_nicknamed_bob_and_bob()

        picked = self._run(
            ctx,
            mock_plextv,
            monkeypatch,
            roster=people,
            users=people,
            rows=[_by_username_row(), _by_name_row()],
            build_only=frozenset({"asked-u"}),
        )

        assert picked == {
            ("alice", "asked-u"): [],
            ("alice", "asked-n"): [],
            ("bob", "asked-u"): [],
            ("bob", "asked-n"): [],
        }
        assert ctx.request_ledger is not None and ctx.request_ledger.titles == []
        assert [(m.label, m.plex_account_id, m.ambiguous) for m in ctx.request_ledger.tag_matches] == [
            ("req-bob", None, True)
        ]

    def test_with_the_overlapping_row_disabled_the_tag_goes_to_its_single_owner(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """A disabled row never reaches the engine's row list, so its pattern names nobody."""
        people = self._alice_nicknamed_bob_and_bob()

        picked = self._run(ctx, mock_plextv, monkeypatch, roster=people, users=people, rows=[_by_username_row()])

        assert picked == {("alice", "asked-u"): [], ("bob", "asked-u"): [10]}
        assert [(m.label, m.plex_account_id, m.ambiguous) for m in ctx.request_ledger.tag_matches] == [
            ("req-bob", 102, False)
        ]

    @pytest.mark.parametrize(
        "build_only,scoped_to_bob",
        [
            pytest.param(None, False, id="full-run"),
            pytest.param(None, True, id="one-person"),
            pytest.param(frozenset({"asked-u"}), False, id="one-row"),
            pytest.param(frozenset({"asked-u"}), True, id="one-row-one-person"),
            pytest.param(frozenset({"asked-n"}), False, id="the-other-row"),
        ],
    )
    def test_a_scoped_run_judges_the_tag_exactly_as_a_full_run_does(
        self, ctx: EngineContext, mock_plextv, monkeypatch, build_only, scoped_to_bob
    ):
        people = self._alice_nicknamed_bob_and_bob()

        picked = self._run(
            ctx,
            mock_plextv,
            monkeypatch,
            roster=people,
            users=people[1:] if scoped_to_bob else people,
            rows=[_by_username_row(), _by_name_row()],
            build_only=build_only,
        )

        assert all(ids == [] for ids in picked.values())
        assert [(m.label, m.plex_account_id, m.ambiguous) for m in ctx.request_ledger.tag_matches] == [
            ("req-bob", None, True)
        ]

    def test_one_person_both_enabled_patterns_render_it_for_is_not_ambiguous(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        bob = make_profile("bob", account_id=102, nickname="bob")
        alice = make_profile("alice", account_id=101, nickname="Alice")

        picked = self._run(
            ctx,
            mock_plextv,
            monkeypatch,
            roster=[alice, bob],
            users=[alice, bob],
            rows=[_by_username_row(), _by_name_row()],
            build_only=frozenset({"asked-u"}),
        )

        # Only the due row is built: the not-due `asked-n` receives nothing tonight, as before.
        assert picked == {
            ("alice", "asked-u"): [],
            ("alice", "asked-n"): [],
            ("bob", "asked-u"): [10],
            ("bob", "asked-n"): [],
        }
        assert [(m.label, m.plex_account_id, m.ambiguous) for m in ctx.request_ledger.tag_matches] == [
            ("req-bob", 102, False)
        ]
