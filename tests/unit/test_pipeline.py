"""Pipeline orchestration: per-user isolation, code-based pick selection, cold start, dry-run,
and the leak-safe ordering (deliver unpromoted → sync filters → promote last)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import shortlist.engine.picker as picker_mod
import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.tmdb import NullCache
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import render_row_name, resolve_row_template, row_marker, strip_marker
from shortlist.engine.models import EngineConfig, MediaType, OwnedRow, Pick, RowOverride, RowSpec, UserType
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user


def _ranked(items: list[dict], affinity: float = 1.0) -> list[tuple[dict, float]]:
    """`TmdbClient.suggestions` returns (item, affinity) pairs. These tests predate affinity and
    don't exercise it, so everything sits at the neutral top-of-list 1.0."""
    return [(item, affinity) for item in items]


def spy_build_picks(monkeypatch) -> list[list]:
    """Record the candidate pools handed to ``picker.build_picks`` — the code-based pick-selection
    step that replaced the old LLM ``curate`` call. Returns one entry per call: the candidate list
    that row+library was offered. ``build_picks`` still runs for real, so the picks are unchanged.
    """
    calls: list[list] = []
    real = picker_mod.build_picks

    def spy(candidates, k):
        calls.append(list(candidates))
        return real(candidates, k)

    monkeypatch.setattr(picker_mod, "build_picks", spy)
    return calls


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    plex = MagicMock()
    movie_section = MagicMock()
    movie_section.type = "movie"
    movie_section.title = "Movies"  # fills {library_name} in the default row title
    plex.sections.return_value = [movie_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section}
    movie_section.collections.return_value = []
    # Library: watched item 900 (ratingKey 999) + candidates 10 and 20.
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []  # delivery finds by title; promotion enumerates rows
    plex.stored_label.side_effect = lambda collection, label: label.replace("shortlist", "Shortlist", 1)
    plex.fetch_items.side_effect = lambda keys: [fake_media_item(k, f"item{k}") for k in keys]

    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]

    # (item, affinity) pairs — see TmdbClient.suggestions. These predate affinity and don't
    # exercise it, so both sit at the neutral top-of-list 1.0.
    mock_tmdb.suggestions.return_value = [
        ({"id": 10, "title": "Candidate Ten", "genre_ids": [], "vote_average": 8.0}, 1.0),
        ({"id": 20, "title": "Candidate Twenty", "genre_ids": [], "vote_average": 7.0}, 1.0),
    ]
    mock_tmdb.genre_names.return_value = {}

    def put(account_id, fields):
        for u in mock_plextv.users:
            if u.id == account_id:
                u.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put

    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


class TestRun:
    def test_happy_path_delivers_syncs_then_promotes(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

        # A row does not exist until created (delivery takes the create path); capture each created
        # collection by the label it is stored under, so promotion — which enumerates a user's rows
        # by label — finds it.
        created_by_label: dict[str, MagicMock] = {}

        def stored_label(collection, label):
            created_by_label[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = lambda section, title, items: MagicMock()
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [created_by_label[label.lower()]] if label.lower() in created_by_label else []
        )

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        assert all(u.status == "ok" for u in report.users)
        assert all(u.privacy_synced for u in report.users)
        # Real deliver_row ran: collections created with the row title, stored labels title-cased.
        assert ctx.plex.create_collection.call_count == 2
        # Each user's filter excludes exactly the OTHER user's stored (title-cased) label.
        sarah_filters = next(u for u in mock_plextv.users if u.id == 100).filters
        assert sarah_filters["filterMovies"] == "label!=Shortlist_mike"
        mike_filters = next(u for u in mock_plextv.users if u.id == 200).filters
        assert mike_filters["filterMovies"] == "label!=Shortlist_sarah"
        # Promotion happened last, for both users' collections.
        assert ctx.plex.promote.call_count == 2

    def test_promotion_only_after_filters_are_merged(self, ctx: EngineContext, mock_plextv):
        """Leak-window regression: no promote call may precede the plex.tv filter writes."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        order = []
        original_put = mock_plextv.update_user_filters.side_effect

        def put(account_id, fields):
            order.append("filter")
            original_put(account_id, fields)

        mock_plextv.update_user_filters.side_effect = put
        ctx.plex.promote.side_effect = lambda *a, **k: order.append("promote")
        existing = MagicMock()
        existing.title = "✨ Picked for You"
        existing.items.return_value = []
        ctx.plex.find_owned_collections.return_value = [existing]

        pipeline_mod.run(ctx, [sarah, mike])

        assert "promote" in order and "filter" in order
        assert order.index("filter") < order.index("promote")
        first_promote = order.index("promote")
        assert all(entry == "promote" for entry in order[first_promote:])

    def test_sync_failure_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.update_user_filters.side_effect = RuntimeError("plex.tv down")

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        ctx.plex.promote.assert_not_called()

    def test_batched_readback_missing_exclude_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """The per-user read-back moved to one roster read after all writes (RANK 1). A write that
        returns fine but silently doesn't stick must still block promotion: the batched read-back
        finds the exclude missing, sets sync_failed, and nothing is promoted."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.update_user_filters.side_effect = lambda *a: None  # write returns ok but doesn't persist

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        mock_plextv.update_user_filters.assert_called()  # the write WAS attempted
        assert "read-back missing" in (report.error or "") or any(
            "read-back missing" in (u.error or "") for u in report.users
        )
        ctx.plex.promote.assert_not_called()

    def test_verification_roster_read_raising_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """If the single post-write roster read (used to verify persistence) itself fails, we cannot
        confirm any exclude stuck -> fail safe, nothing promoted. list_users is called twice per run:
        once to build the roster, once to verify; only the second (verify) read raises here."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        calls = {"n": 0}

        def list_users():
            calls["n"] += 1
            if calls["n"] >= 2:  # the verification read-back
                raise RuntimeError("plex.tv roster read failed")
            return mock_plextv.users

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        assert "could not verify filters" in (report.error or "")
        ctx.plex.promote.assert_not_called()

    def test_account_absent_from_verification_roster_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """A write happens, but the verification roster read-back no longer lists that account (its
        share vanished mid-run) -> its just-merged exclude cannot be confirmed -> fail safe, nothing
        promoted. Reproduces the `remote2 is None -> got=''` branch of the batched verify."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        full = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.users = full
        calls = {"n": 0}

        def list_users():
            calls["n"] += 1
            if calls["n"] >= 2:  # verification read-back has lost sarah
                return [u for u in full if u.id != 100]
            return full

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        ctx.plex.promote.assert_not_called()

    def _managed_remote(self, profile: str):
        """A Plex Home account. `restricted` is True either way — only the PROFILE says whether Plex
        will accept a label filter, or hides anything at all (#20)."""
        from shortlist.engine.clients.plextv import PlexTvUser

        return PlexTvUser(
            id=500,
            username="kid",
            user_type=UserType.MANAGED,
            home=True,
            restricted=True,
            protected=False,
            restriction_profile=profile,
            filters=dict.fromkeys(("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos"), ""),
        )

    def test_a_parental_profile_account_never_reaches_the_write_and_promotion_proceeds(
        self, ctx: EngineContext, mock_plextv
    ):
        """`little_kid` is skipped in privacy.py before the write fires, so one such account cannot
        block promotion for the whole server (#14). Without the profile set this test passed for the
        WRONG reason — no refusal happened at all, because the write simply succeeded."""
        sarah = make_profile("sarah", account_id=100)
        kid = make_profile("kid", account_id=500)
        mock_plextv.users = [plextv_user(100, "sarah"), self._managed_remote("little_kid")]

        report = pipeline_mod.run(ctx, [sarah, kid])

        assert report.ok
        assert not report.promotion_blockers
        # The kid is never written to; sarah is.
        assert [c.args[0] for c in mock_plextv.update_user_filters.call_args_list] == [100]

    def test_a_422_on_a_managed_account_with_NO_profile_BLOCKS_promotion(self, ctx: EngineContext, mock_plextv):
        """The branch the #20 fix opens. Profile-less managed accounts now get write attempts, so they
        reach the 422 handler for the first time. Treating that as a known-safe skip would promote every
        private row while this account holds no excludes at all — #20's leak, with the check that should
        catch it switched off."""
        from shortlist.engine.clients.plextv import FilterWriteRefused

        sarah = make_profile("sarah", account_id=100)
        kid = make_profile("kid", account_id=500)
        mock_plextv.users = [plextv_user(100, "sarah"), self._managed_remote("")]

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [sarah, kid])

        assert report.promotion_blockers, "a 422 with no parental profile is an UNKNOWN failure"
        assert any("500" in b for b in report.promotion_blockers)
        # `promotion_blockers` IS the consequence here: `_promote_phase` skips every user when it is
        # non-empty. Asserting `promote` was not called would prove nothing in this fixture — these
        # users deliver no collections, so it is never called either way.

    def test_when_profiles_cannot_be_read_a_restricted_422_does_not_block_the_server(
        self, ctx: EngineContext, mock_plextv
    ):
        """The permanent-outage case. `restrictionProfile` comes from the v1 `/api/home/users` surface;
        if that ever goes away, every profiled account reads as "no profile", 422s on the write, and
        would be treated as an unknown failure — blocking promotion for the WHOLE server, every night,
        until somebody disabled those users by hand. That is #14's shape, one endpoint removal away.

        So "we could not find out" is kept distinct from "no profile", and falls back to trusting
        `restricted` exactly as the code did before #20."""
        from shortlist.engine.clients.plextv import FilterWriteRefused

        sarah = make_profile("sarah", account_id=100)
        kid = make_profile("kid", account_id=500)
        mock_plextv.users = [plextv_user(100, "sarah"), self._managed_remote("")]
        mock_plextv.home_profile_known.return_value = False  # the endpoint could not be read

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [sarah, kid])

        assert not report.promotion_blockers, "one restricted account must not stop the whole server"
        # sarah's filters were still written — the run carried on rather than aborting on the kid.
        assert 100 in [c.args[0] for c in mock_plextv.update_user_filters.call_args_list]

    def test_the_tail_phases_narrate_themselves(self, ctx: EngineContext, mock_plextv):
        """Everything after the last user — filters, promotion, ordering — used to emit nothing.

        The sidebar activity pill shows the most recent stage event, so it froze on the last user's
        last stage for the whole tail. On a real server that was ~25 minutes reading
        "kateystreet — gathering candidates" while the run was actually ordering collections: a
        healthy run indistinguishable from a wedged one.
        """
        stages: list[tuple[str, str]] = []
        ctx.progress = lambda slug, stage, counts, reason=None: stages.append((slug, stage))
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        tail = [stage for slug, stage in stages if slug == "Shortlist"]
        assert "filters" in tail, "the share-filter merge is invisible"
        assert "promoting" in tail, "promotion is invisible"
        assert "ordering" in tail, "the long ordering pass is invisible"
        # And they come after the per-user work, not before it.
        assert stages.index(("Shortlist", "ordering")) > stages.index(("Shortlist", "filters"))

    def test_every_tail_phase_narrates_itself_including_converge(self, ctx: EngineContext, mock_plextv):
        """The third time this bug has appeared, so this asserts the WHOLE tail, not a sample.

        Converge, the shelf reorder and the requests pass emitted nothing at all — they ran after
        every per-user card was already terminal, so the feed's last line stayed on whoever finished
        last while minutes of real work went by. Add a phase to `run()` without an `_emit` and this
        fails.
        """
        stages: list[tuple[str, str]] = []
        ctx.progress = lambda slug, stage, counts, reason=None: stages.append((slug, stage))
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        tail = [stage for slug, stage in stages if slug == "Shortlist"]
        for stage in ("users_done", "converging", "converged", "shelves", "finished"):
            assert stage in tail, f"the {stage} phase is invisible in the activity feed"
        # "all users done" must land before the server-wide tail, and "finished" must be last —
        # that pair is what makes "is it still going?" answerable from the feed alone.
        assert tail.index("users_done") < tail.index("filters")
        assert tail[-1] == "finished"

    def test_the_narration_counts_out_the_long_per_account_phases(self, ctx: EngineContext, mock_plextv):
        """One plex.tv write per account, throttled — a bare "filters" line sits there for minutes."""
        emitted: list[tuple[str, dict]] = []
        ctx.progress = lambda slug, stage, counts, reason=None: emitted.append((stage, counts))
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        progress = [counts for stage, counts in emitted if stage == "filters" and counts]
        assert progress, "the share-filter merge reports no progress at all"
        assert progress[-1]["done"] == progress[-1]["total"], "the count never reaches its total"

    def test_a_roster_that_omits_someone_is_not_knowledge_about_them(self, ctx: EngineContext, mock_plextv):
        """A 200 is not the same as a complete answer.

        `/api/home/users` returning an empty `<MediaContainer>` — or simply omitting an account —
        used to satisfy a single global "the read succeeded" flag. A genuinely profiled child then
        read as having NO profile, so their 422 looked unexpected, and promotion was blocked for
        EVERY user on the server, every night, behind a green suite. That is #14's shape re-created
        by the very guard added to prevent it.

        Knowledge is per account: somebody the roster never mentioned is unknown, whatever the
        status code was, and falls back to trusting `restricted` like any other unknown.
        """
        from shortlist.engine.clients.plextv import FilterWriteRefused

        sarah = make_profile("sarah", account_id=100)
        kid = make_profile("kid", account_id=500)
        mock_plextv.users = [plextv_user(100, "sarah"), self._managed_remote("")]
        # The read SUCCEEDED, but the roster covered sarah and never mentioned the kid.
        mock_plextv.home_profile_known.side_effect = lambda account_id: account_id != 500

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [sarah, kid])

        assert not report.promotion_blockers, "an account the Home roster omitted must not stop the server"
        assert 100 in [c.args[0] for c in mock_plextv.update_user_filters.call_args_list]

    def test_a_covered_account_with_no_profile_still_blocks_on_a_422(self, ctx: EngineContext, mock_plextv):
        """The other side of the same coin, and the reason the guard exists at all.

        When the roster DID cover the account and reported no profile, a 422 is genuinely
        unexpected — that account holds no excludes, so promoting anything would publish private
        rows to them. Blocking is correct here and must survive the per-account change above.
        """
        from shortlist.engine.clients.plextv import FilterWriteRefused

        sarah = make_profile("sarah", account_id=100)
        kid = make_profile("kid", account_id=500)
        mock_plextv.users = [plextv_user(100, "sarah"), self._managed_remote("")]
        mock_plextv.home_profile_known.return_value = True  # covered, and reported no profile

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [sarah, kid])
        assert report.promotion_blockers, "a 422 on an account with a KNOWN-absent profile must block"

    def test_a_profile_on_a_NON_restricted_account_keeps_its_excludes_and_never_blocks(
        self, ctx: EngineContext, mock_plextv
    ):
        """The cell both guards exist for, and the only one that reaches the handler's skip branch.

        `restricted` and `restrictionProfile` come from different endpoints and nothing enforces a
        relationship between them. An account plex.tv calls unrestricted while reporting a profile is
        typed SHARED and used to receive excludes — so the skip must not swallow it (privacy.py
        requires BOTH flags), and if plex.tv then refuses the write that refusal is a known-safe one.
        """
        from shortlist.engine.clients.plextv import FilterWriteRefused, PlexTvUser

        sarah = make_profile("sarah", account_id=100)
        odd = make_profile("odd", account_id=700)
        odd_remote = PlexTvUser(
            id=700,
            username="odd",
            user_type=UserType.SHARED,
            home=False,
            restricted=False,  # plex.tv says unrestricted...
            protected=False,
            restriction_profile="teen",  # ...while reporting a profile
            filters=dict.fromkeys(("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos"), ""),
        )
        mock_plextv.users = [plextv_user(100, "sarah"), odd_remote]

        def refuse_the_odd_one(account_id, fields):
            if account_id == 700:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 700: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_odd_one

        report = pipeline_mod.run(ctx, [sarah, odd])

        # The write was ATTEMPTED — the subset guard means this account never silently loses excludes.
        assert 700 in [c.args[0] for c in mock_plextv.update_user_filters.call_args_list]
        # And the 422 is treated as expected, so one odd account cannot stop the server (#14).
        assert not report.promotion_blockers

    def test_filter_write_refused_on_non_restricted_account_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """A 422 on a NON-restricted account is an unknown failure — must block promotion (leak risk)."""
        from shortlist.engine.clients.plextv import FilterWriteRefused

        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

        def refuse_mike(account_id, fields):
            if account_id == 200:
                raise FilterWriteRefused("plex.tv 422 for account 200")
            mock_plextv.users[0].filters.update(fields)

        mock_plextv.update_user_filters.side_effect = refuse_mike

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.promotion_blockers  # promotion was blocked
        assert any("200" in b for b in report.promotion_blockers)
        ctx.plex.promote.assert_not_called()

    def test_on_user_done_fires_once_per_user(self, ctx: EngineContext, mock_plextv):
        """The live-persist hook fires as each user finishes (so the UI fills in person by person),
        with that user's finished report."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        seen: list[tuple[str, str]] = []
        ctx.on_user_done = lambda profile, report: seen.append((profile.slug, report.status))

        pipeline_mod.run(ctx, [sarah, mike])

        assert sorted(slug for slug, _ in seen) == ["mike", "sarah"]
        assert all(status in ("ok", "cold_start", "error") for _, status in seen)

    def test_on_user_done_error_never_sinks_the_run(self, ctx: EngineContext, mock_plextv):
        """A persistence hiccup in the hook must not fail the user or the run — the end-of-run persist
        is the backstop."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ran = []

        def boom(_profile, _report):
            ran.append(True)
            raise RuntimeError("db locked")

        ctx.on_user_done = boom
        report = pipeline_mod.run(ctx, [sarah])

        assert ran  # the hook DID run (and raised)
        assert any(u.slug == "sarah" for u in report.users)  # yet the user is still processed + reported

    def test_one_user_failing_never_stops_the_others(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        good_history = ctx.history_source.fetch.return_value

        def fetch(user, *, min_completion):
            if user.slug == "sarah":
                raise RuntimeError("tautulli exploded")
            return good_history

        ctx.history_source.fetch.side_effect = fetch
        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        by_slug = {u.slug: u for u in report.users}
        assert by_slug["sarah"].status == "error"
        assert "tautulli exploded" in by_slug["sarah"].error
        assert by_slug["mike"].status == "ok"
        # Privacy sync still ran for the errored user (delivery and sync are independent).
        assert by_slug["sarah"].privacy_synced or by_slug["sarah"].error

    def test_picks_are_built_in_code_with_because_you_watched_reasons(self, ctx: EngineContext, mock_plextv):
        """There is no LLM curate step: picks are selected and reasoned in code (picker.build_picks).
        A default run delivers a full row whose reasons point back at the seeding history."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "ok"
        assert user_report.counts.picks > 0
        assert user_report.picks[0].reason.startswith("Because you watched")

    def test_a_pool_smaller_than_the_row_delivers_what_it_has_ranked_in_order(self, ctx: EngineContext, mock_plextv):
        """The row size is 5 but only two candidates exist in the library; the row fills to what the
        pool holds (no invented titles), ranked 1..n."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].counts.picks == 2  # both library candidates used
        assert [p.rank for p in report.users[0].picks] == [1, 2]

    def test_cold_start_uses_popular_row(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        # The guid parse now lives in PlexClient.top_rated; cold start just consumes (tmdb_id, item)
        # pairs. A movies-only server yields one movie pick.
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "cold_start"
        assert [p.title for p in user_report.picks] == ["Top Rated"]
        assert user_report.picks[0].reason == "Popular on this server"

    def test_cold_start_files_a_trace_so_the_how_we_picked_button_appears(self, ctx: EngineContext, mock_plextv):
        # A cold user used to file picks but NO trace, so the run page showed no "How we picked" button
        # and they read as skipped (the reported Cassie bug). The cold path must file a history stage
        # (their thin watches, no seeds — nothing was searched) plus a synthetic cold_start gather.
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]

        report = pipeline_mod.run(ctx, [sarah])

        trace = report.users[0].trace
        assert trace, "a cold user must file a trace — has_trace gates the 'How we picked' button"
        # History stage present with the honest full count, and NO seeds (nothing was searched from them).
        assert trace["history"]["total"] == 1
        assert trace["seeds"] == []
        # Exactly one synthetic cold_start gather, labelled by media, contributing the delivered picks.
        gathers = trace["gathers"]
        assert [g["pool"] for g in gathers] == ["movie · cold_start"]
        assert gathers[0]["sources"][0] == {
            "source": "cold_start",
            "status": "ok",
            "contributed": 1,
            "detail": "",
        }

    def _make_cold(self, ctx: EngineContext, mock_plextv) -> object:
        """One user, one watch (below min_history), one top-rated title to fall back to."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]
        return sarah

    def test_cold_start_skip_builds_no_row_at_all(self, ctx: EngineContext, mock_plextv):
        """The whole point of issue #66: 'skip' means no row, not a row of popular titles."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.picks == []
        ctx.plex.create_collection.assert_not_called()
        # Popular titles are never even LOOKED UP — skipping must not pay for the fallback it declines.
        ctx.plex.top_rated.assert_not_called()

    def test_cold_start_skip_keeps_the_user_flagged_cold_not_skipped(self, ctx: EngineContext, mock_plextv):
        """`run_persistence` derives `user.cold_start` from this status, and the Users page reads that
        flag to explain the missing row. Reporting "skipped" would clear it and leave the UI silent."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "cold_start"
        assert "1 of 3 titles" in user_report.reason  # engine_config sets min_history=3

    def test_cold_start_skip_removes_a_row_they_already_have(self, ctx: EngineContext, mock_plextv):
        """Someone warm last month already has this row on their Home. Skipping has to mean GONE —
        otherwise it sits there going stale for ever with nothing that ever cleans it up."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"
        existing = fake_media_item(4242, "✨ Movies Picked for You" + row_marker(100))
        ctx.plex.find_owned_collections.return_value = [existing]

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is existing

    def test_cold_start_skip_leaves_a_warm_user_alone(self, ctx: EngineContext, mock_plextv):
        """The setting is scoped to thin history — it must not touch anyone above the threshold."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.config.cold_start = "skip"
        ctx.config.min_history = 1  # their 4 watches are plenty

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].status == "ok"
        assert report.users[0].picks
        ctx.plex.delete_owned_collection.assert_not_called()

    def test_cold_start_skip_removes_a_top_seed_row_via_the_delivery_ledger(self, ctx: EngineContext, mock_plextv):
        """The headline capability, end to end through `_ledger_keys`.

        A `{top_seed}` row's title was different every run, so nothing computed from config can find
        it — `remove_row` correctly refuses to title-match it. The delivery ledger is the ONLY handle,
        so without this wiring a skipped (or muted) `{top_seed}` row could never actually be removed
        and the feature would be silently dead for exactly the row it matters most for.
        """
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"
        ctx.config.rows = [RowSpec(slug="because", name_template="Because you watched {top_seed}", size=5)]
        ctx.config.rows_defined = True
        existing = fake_media_item(4242, "Because you watched The Bear" + row_marker(100))
        ctx.plex.find_owned_collections.return_value = [existing]
        # The ledger is keyed by SECTION key, so the fixture's section needs a real one.
        section_key = str(ctx.plex.sections.return_value[0].key)
        # Another user's entry rides along so the `slug == user.slug` filter has something to exclude.
        ctx.delivered_keys = {
            ("sarah", "because", section_key): 4242,
            ("mike", "because", section_key): 9999,
        }

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is existing

    def test_cold_start_skip_forgets_the_ledger_entry_it_just_deleted(self, ctx: EngineContext, mock_plextv):
        """A key whose collection is gone must not survive to the next run.

        This path REPEATS — a cold user is skipped again every night — so a kept key is re-presented
        for as long as they stay cold, and Plex reuses `metadata_items.id`. The adapter prunes these
        on persist, the way the on-demand reconciles already call `_forget_deliveries`.
        """
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"
        section_key = str(ctx.plex.sections.return_value[0].key)
        ctx.plex.find_owned_collections.return_value = [
            fake_media_item(4242, "✨ Movies Picked for You" + row_marker(100))
        ]
        ctx.delivered_keys = {("sarah", "picked", section_key): 4242}

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].removed_deliveries == [{"row_slug": "picked", "library_key": section_key}]

    def test_a_dry_run_forgets_no_ledger_entries(self, ctx: EngineContext, mock_plextv):
        """Nothing was deleted, so the ledger is still the truth — forgetting would blind the next
        REAL reconcile of a `{top_seed}` row, whose entry is the only thing that can address it."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"
        ctx.config.dry_run = True
        ctx.plex.find_owned_collections.return_value = [
            fake_media_item(4242, "✨ Movies Picked for You" + row_marker(100))
        ]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].removed_deliveries == []

    def test_the_skip_reason_does_not_claim_a_muted_rows_deletion_as_its_own(self, ctx: EngineContext, mock_plextv):
        """`_remove_muted_and_retired` appends to the SAME diff earlier in the run, so a total (rather
        than a delta) told the owner the skip removed a collection when it removed nothing."""
        sarah = make_profile("sarah", account_id=100, row_overrides={"gems": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]
        ctx.config.cold_start = "skip"
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=5),
            RowSpec(slug="gems", name_template="Hidden Gems", size=5),
        ]
        ctx.config.rows_defined = True
        # ONE collection on the server, titled for the MUTED row. The cold-skipped row's own title
        # ("✨ Movies Picked for You") matches nothing here, so the skip removes nothing — while the
        # mute removes this one and appends it to the very diff the reason used to count.
        ctx.plex.find_owned_collections.return_value = [fake_media_item(7777, "Hidden Gems" + row_marker(100))]

        report = pipeline_mod.run(ctx, [sarah])

        reason = report.users[0].reason
        assert "removed" not in reason, f"the skip claimed a removal it never made: {reason!r}"

    def test_cold_start_skip_writes_nothing_in_a_dry_run(self, ctx: EngineContext, mock_plextv):
        """Rule 8 covers a DELETE here, and the general dry-run test uses warm users."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "skip"
        ctx.config.dry_run = True
        ctx.plex.find_owned_collections.return_value = [
            fake_media_item(4242, "✨ Movies Picked for You" + row_marker(100))
        ]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_not_called()
        # ...but it still REPORTS the would-be removal, or a dry run could never be used to preview this.
        assert report.users[0].diff.deleted == ["✨ Movies Picked for You"]

    def test_a_rows_own_cold_start_beats_the_global(self, ctx: EngineContext, mock_plextv):
        """Two rows, opposite settings: the `{top_seed}` one skips, the plain one still gets popular
        titles. This is the case the per-row override exists for."""
        sarah = self._make_cold(ctx, mock_plextv)
        ctx.config.cold_start = "popular"
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=5),
            RowSpec(slug="because", name_template="Because you watched {top_seed}", size=5, cold_start="skip"),
        ]
        ctx.config.rows_defined = True

        report = pipeline_mod.run(ctx, [sarah])

        assert {p.collection_slug for p in report.users[0].picks} == {"picked"}

    def test_dry_run_makes_zero_plex_writes(self, ctx: EngineContext, mock_plextv):
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        mock_plextv.update_user_filters.assert_not_called()
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()
        # No collections exist yet, so there is nothing to exclude — dry run says so honestly.
        assert not any(u.privacy_synced for u in report.users)

    def test_dry_run_steady_state_reports_no_filter_changes(self, ctx: EngineContext, mock_plextv):
        """With existing collections + correct filters, a dry run is a full no-op."""
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow("Shortlist_sarah", [1]),
            "mike": OwnedRow("Shortlist_mike", [2]),
        }
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={"filterMovies": "label!=Shortlist_mike", "filterTelevision": "label!=Shortlist_mike"},
            ),
            plextv_user(
                200,
                "mike",
                filters={"filterMovies": "label!=Shortlist_sarah", "filterTelevision": "label!=Shortlist_sarah"},
            ),
        ]

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        assert not any(u.privacy_synced for u in report.users)
        mock_plextv.update_user_filters.assert_not_called()

    def test_no_picks_leaves_existing_row_untouched(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = _ranked([])  # nothing suggested -> no candidates

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].counts.picks == 0
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()


class TestPerRowOverrides:
    """A per-user override can mute or resize one row without touching it for others."""

    def test_picks_are_tagged_with_their_row_slug(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        assert picks and all(p.collection_slug == "picked" for p in picks)  # the default row's slug
        # Each pick also carries the library it was delivered into, so the report can split a
        # multi-library row per library. section_key is the Plex key; library its display name.
        assert all(p.section_key and p.library for p in picks)

    def test_muting_the_only_row_delivers_nothing(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].picks == []
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()

    def test_per_row_size_override_wins(self, ctx: EngineContext, mock_plextv):
        # The fixture pool has 2 candidates; an override of size 1 must cap this user's row at 1.
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(size=1)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert len(report.users[0].picks) == 1

    def test_per_user_recent_count_override_reaches_the_gather(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # recent_count caps how many recent watches the llm_web source searches. A per-user override
        # must reach the gather as its resolved recent_count — beating the row's own value AND the
        # global default. (That the gather then slices seeds[:recent_count] is test_candidates' job.)
        from shortlist.engine import candidates as candidates_mod

        seen: list[int] = []
        real_gather = candidates_mod.gather_candidates

        def spy_gather(*args, **kwargs):
            seen.append(kwargs["recent_count"])
            return real_gather(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows.candidates_mod, "gather_candidates", spy_gather)
        ctx.config.recent_count = 10  # global default
        # The row sets its own recent_count too, so seen==[3] proves the user override beats BOTH the
        # row's value (8) and the global default (10) — not just the global.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, candidate_sources=["llm_web"], recent_count=8)
        ]
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(recent_count=3)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        assert seen == [3]  # the person's override, beating the row's 8 and the global 10

    def test_per_row_max_seeds_caps_the_seeds_the_row_is_built_from(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # max_seeds decides how many watched titles a row is derived from — what EVERY source searches
        # from, not just the web one. A row's own value must beat the global (issue #57: a
        # `{top_seed}` row named after one watch was still built from thirty).
        seen: list[int] = []
        real_derive = pipeline_mod.rows.derive_seeds

        def spy_derive(*args, **kwargs):
            seen.append(kwargs["max_seeds"])
            return real_derive(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows, "derive_seeds", spy_derive)
        ctx.config.max_seeds = 10  # the global budget this row must override
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, max_seeds=2)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert seen == [2]

    def test_two_rows_differing_only_in_max_seeds_do_not_share_seeds(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        # Both rows target the same media and libraries, so they hit the same memo key on every
        # other axis. If max_seeds were left out of that key the second row would silently reuse the
        # first row's seed set — and its own setting would do nothing at all.
        seen: list[int] = []
        real_derive = pipeline_mod.rows.derive_seeds

        def spy_derive(*args, **kwargs):
            seen.append(kwargs["max_seeds"])
            return real_derive(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows, "derive_seeds", spy_derive)
        ctx.config.max_seeds = 10
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=1),
            RowSpec(slug="deep", name_template="Deep", size=5, max_seeds=4),
            RowSpec(slug="default", name_template="Default", size=5),  # inherits the global 10
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert sorted(seen) == [1, 4, 10]

    def test_rows_with_different_max_seeds_gather_separately(self, ctx: EngineContext, mock_plextv):
        # The OUTCOME test, not a spy: the two above pass even with `max_seeds` removed from
        # `pool_key`, because the up-front `counts.seeds` loop calls seeds_for for every spec whatever
        # the pools then do. Without that key entry the rows SHARE one pool — whichever reaches
        # pools_for first builds it — and the second row's budget is silently inert. Which is issue
        # #57 shipping "fixed" and not fixed.
        ctx.history_source.fetch.return_value = [
            make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)
        ]
        ctx.config.max_seeds = 10
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=4),
            RowSpec(slug="because", name_template="Because {top_seed}", size=5, max_seeds=1),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        # One suggestions() call per seed: 4 + 1 across two pools. A shared pool would be 4.
        assert ctx.tmdb.suggestions.call_count == 5

    def test_a_rewatch_row_keeps_finished_titles_that_a_normal_row_drops(self, ctx: EngineContext, mock_plextv):
        """The OUTCOME test for `excludes_finished` in `pool_key`.

        A normal row at watched_pct 0 has finished titles removed from its POOL; a rewatch row must
        keep them. If the two rows shared one pool — whichever built it first would win — the rewatch
        row could never deliver a rewatch, and the flag would look implemented while doing nothing.
        """
        # Seeds come from the default Fargo watches (tmdb 900, via the library index). Candidate 10 is
        # ALSO something they have watched, but is not a seed — seeds are excluded from every pool, so a
        # title cannot be both the seed and the rewatch under test.
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Candidate Ten", days_ago=6, tmdb_id=10),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [
            RowSpec(slug="fresh", name_template="Fresh", size=2),
            RowSpec(slug="again", name_template="Again", size=2, rewatch=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        by_row: dict[str, set[int]] = {}
        for pick in report.users[0].picks:
            by_row.setdefault(pick.collection_slug, set()).add(pick.tmdb_id)
        assert by_row, "the run produced no picks at all — the test fixture, not the feature"
        assert 10 not in by_row.get("fresh", set()), "a normal row must not deliver a finished title"
        assert 10 in by_row.get("again", set()), "the rewatch row must be able to deliver one"

    def test_a_rewatch_row_leads_with_the_rewatch(self, ctx: EngineContext, mock_plextv):
        """Not just present — FIRST. `watched_pct` alone could admit it at the bottom of the row."""
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            # 20 is the lower-rated candidate, so ranking puts it AFTER 10 only if rewatch reorders.
            make_watched("Candidate Twenty", days_ago=6, tmdb_id=20),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [RowSpec(slug="again", name_template="Again", size=2, rewatch=True)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert delivered[0] == 20, f"the already-watched title must lead the row, got {delivered}"

    def test_an_unstarted_only_row_drops_a_barely_started_show(self, ctx: EngineContext, mock_plextv):
        """Stricter than the finished filter, and its own pool.

        A show 1 episode into 40 is NOT finished, so every other row may still offer it. An
        "unstarted only" row must not — that is the whole claim of "a series to start".
        """
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Started Show", "genre_ids": [], "vote_average": 8.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        # The seed show (900) plus show 30 at ONE episode of forty: started, nowhere near finished.
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            make_watched(
                "Started Show",
                days_ago=6,
                media_type=MediaType.SHOW,
                tmdb_id=30,
                viewed_leaf_count=1,
                leaf_count=40,
            ),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [
            RowSpec(slug="anything", name_template="Anything", size=2, media=MediaType.SHOW),
            RowSpec(slug="tostart", name_template="To start", size=2, media=MediaType.SHOW, unstarted_only=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        by_row: dict[str, set[int]] = {}
        for pick in report.users[0].picks:
            by_row.setdefault(pick.collection_slug, set()).add(pick.tmdb_id)
        assert by_row, "the run produced no picks at all — the test fixture, not the feature"
        assert 30 in by_row.get("anything", set()), "a part-watched show is fair game for a normal row"
        assert 30 not in by_row.get("tostart", set()), "a started series must never reach an unstarted row"
        assert 40 in by_row.get("tostart", set()), "the never-opened one is exactly what it wants"

    def test_a_rewatch_row_shares_the_pool_of_a_watched_pct_row(self, ctx: EngineContext, mock_plextv):
        """The OTHER direction of `excludes_finished`, which no membership assertion can catch.

        Both rows want finished titles kept in the pool, so they must share ONE gather. Without this,
        `excludes_finished` could regress to keying on the raw percentage — splitting the pool and
        paying a second time for every rate-limited/LLM source — and every other test still passes.
        """
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]
        ctx.config.rows = [
            RowSpec(slug="again", name_template="Again", size=2, rewatch=True),
            RowSpec(slug="capped", name_template="Capped", size=2, watched_pct=0.5),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        # One suggestions() call per seed per pool. One seed, one shared pool = exactly one call.
        assert ctx.tmdb.suggestions.call_count == 1, "a rewatch row and a >0 row must share one pool"

    def test_rewatch_works_for_shows_where_finished_is_a_different_predicate(self, ctx: EngineContext, mock_plextv):
        """For movies "finished" is any watch; for shows it is the `watched_show_pct` fraction plus a
        length-scaled floor (`_watched_titles`) — a different predicate, so a different cell."""
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Finished Show", "genre_ids": [], "vote_average": 6.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 9.0},
            ]
        )
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            # 10 of 10 episodes: finished by any measure, so it belongs in a rewatch row.
            make_watched(
                "Finished Show",
                days_ago=6,
                media_type=MediaType.SHOW,
                tmdb_id=30,
                viewed_leaf_count=10,
                leaf_count=10,
            ),
        ]
        ctx.config.rows = [
            RowSpec(slug="again", name_template="Again", size=2, media=MediaType.SHOW, rewatch=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        # 40 is rated higher, so only the rewatch preference can put the finished show first.
        assert delivered and delivered[0] == 30, f"the finished SHOW must lead the row, got {delivered}"

    def test_unstarted_only_applies_on_a_both_media_row_not_just_a_shows_row(self, ctx: EngineContext, mock_plextv):
        """`media="both"` is its own cell: the filter must not be gated on the row being shows-only.

        Movie immunity is asserted at the unit level instead (`TestStartedShows` — `_started_shows`
        yields only SHOW keys, so nothing it returns can match a movie candidate). Doing it here would
        need a second seed of the other type, because candidates inherit their SEED's media type — so
        a movie-seeded gather types even a TV title as a movie and the test would pass for the wrong
        reason.
        """
        show_section, movie_section = MagicMock(), MagicMock()
        show_section.type, show_section.title = "show", "TV Shows"
        movie_section.type, movie_section.title = "movie", "Movies"
        for sec in (show_section, movie_section):
            sec.collections.return_value = []
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {
            MediaType.MOVIE: movie_section,
            MediaType.SHOW: show_section,
        }
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Started Show", "genre_ids": [], "vote_average": 9.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            make_watched(
                "Started Show", days_ago=6, media_type=MediaType.SHOW, tmdb_id=30, viewed_leaf_count=1, leaf_count=40
            ),
        ]
        # media defaults to "both" — deliberately NOT narrowed to shows.
        ctx.config.rows = [RowSpec(slug="mixed", name_template="Mixed", size=3, unstarted_only=True)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = {p.tmdb_id for p in report.users[0].picks}
        assert delivered, "no picks at all — the fixture, not the feature"
        assert 30 not in delivered, "the started series must be excluded on a both-media row too"
        assert 40 in delivered

    def test_pools_that_differ_only_in_seed_count_are_labelled_apart(self, ctx: EngineContext, mock_plextv):
        # The trace labels a gather by media + sources. Two rows differing only in max_seeds share
        # both, so without the seed count they record under two IDENTICAL names and the trace cannot
        # say which gather belonged to which row. (The "How we picked" page doesn't render the label
        # today — it merges a library's gathers into one source list — so this is about the stored
        # record, not the screen.) The media prefix must stay first: `poolCoversMedia` splits on
        # " · " to place a gather in a library.
        ctx.history_source.fetch.return_value = [
            make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=4),
            RowSpec(slug="because", name_template="Because {top_seed}", size=5, max_seeds=1),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        labels = [g["pool"] for g in report.users[0].trace["gathers"]]
        assert len(set(labels)) == 2, labels
        assert all(lbl.startswith("movie · ") or lbl.startswith("both · ") for lbl in labels), labels
        assert any("1 seed" in lbl for lbl in labels) and any("4 seeds" in lbl for lbl in labels), labels

    def test_a_single_pool_is_not_labelled_with_a_seed_count(self, ctx: EngineContext, mock_plextv):
        # The count is noise when nothing differs — every row inheriting the default is the common
        # case, and its trace should read exactly as it did before per-row budgets existed.
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert all("seed" not in g["pool"] for g in report.users[0].trace["gathers"])

    def test_per_row_candidate_sources_gate_which_apis_run(self, ctx: EngineContext, mock_plextv):
        # A row pinned to tmdb_discover only must query discover and NOT the tmdb_similar endpoint —
        # per-row sources override the global set for that row.
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_discover"])]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 20, "title": "Discovered", "genre_ids": [18], "vote_average": 8.5}
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        assert ctx.tmdb.discover.called  # the row's own source ran
        assert not ctx.tmdb.suggestions.called  # tmdb_similar was NOT in this row's sources

    def test_same_sources_in_different_order_share_one_pool(self, ctx: EngineContext, mock_plextv):
        # Two rows list the same sources in a different order. gather is set-based, so they must
        # reuse ONE pool (keyed on the sorted set) — not rebuild it, re-hitting the source APIs.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_similar", "tmdb_discover"]),
            RowSpec(slug="gems", name_template="Gems", size=5, candidate_sources=["tmdb_discover", "tmdb_similar"]),
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: []

        pipeline_mod.run(ctx, [sarah])

        # One seed, one shared pool -> tmdb_similar queried once, not once per row.
        assert ctx.tmdb.suggestions.call_count == 1

    def test_row_pinned_to_a_non_lowest_key_library_is_delivered_and_promoted_there(
        self, ctx: EngineContext, mock_plextv
    ):
        # Regression: promotion is the only thing that hides a collection from LIBRARY BROWSE
        # (share filters only cover Home/Recommended/Related), so a row delivered to a library that
        # isn't the lowest-key one of its type must still be promoted there — or it leaks into browse.
        lib1 = MagicMock()
        lib1.type = "movie"
        lib1.key = "1"
        lib1.title = "Movies"
        lib2 = MagicMock()
        lib2.type = "movie"
        lib2.key = "2"  # the SECOND movie library — never returned by sections_by_type()
        lib2.title = "4K Movies"
        ctx.plex.sections.return_value = [lib1, lib2]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: lib1}  # lowest-key only
        ctx.plex.build_library_index.side_effect = lambda s: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010, 20: 2020}
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        made: list[MagicMock] = []

        def create_collection(section, title, items):
            c = MagicMock()
            c._section = section
            made.append(c)
            return c

        ctx.plex.create_collection.side_effect = create_collection
        ctx.plex.find_owned_collections.side_effect = lambda section, label: [c for c in made if c._section is section]

        pipeline_mod.run(ctx, [sarah])

        # Delivered into lib2 with lib2's ratingKeys (not lib1's 10xx), and PROMOTED there.
        assert ctx.plex.create_collection.call_args.args[0] is lib2
        assert ctx.plex.fetch_items.call_args.args[0] == [2010, 2020]
        promoted_sections = {getattr(call.args[0], "_section", None) for call in ctx.plex.promote.call_args_list}
        assert lib2 in promoted_sections, "the row in the non-lowest-key library was never promoted (leak)"

    def test_a_pinned_row_only_recommends_titles_its_own_library_holds(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """A row pinned to a library was selected against the UNION of every library of its type, and
        delivery then dropped every pick the pinned library didn't hold — a short row, or an empty
        one, reported as ok. The pool must be narrowed to the row's own libraries first."""
        lib1 = MagicMock()
        lib1.type = "movie"
        lib1.key = "1"
        lib1.title = "Movies"
        lib2 = MagicMock()
        lib2.type = "movie"
        lib2.key = "2"
        lib2.title = "4K Movies"
        ctx.plex.sections.return_value = [lib1, lib2]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: lib1}
        # Candidate 10 is in BOTH libraries; candidate 20 lives only in lib1.
        ctx.plex.build_library_index.side_effect = lambda s: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010}
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        # 20 isn't in lib2, so the pick builder must never have been offered it.
        offered_ids = {c.tmdb_id for call in offered for c in call}
        assert 10 in offered_ids
        assert 20 not in offered_ids, "the row was offered a title its own library doesn't hold"

    def test_a_shows_only_row_survives_a_movie_heavy_pool(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The media filter used to run AFTER the pre-rank truncation, so a movie-heavy watcher's
        shows-only row could lose every show to the 40-candidate cut and deliver nothing."""
        movie_section = MagicMock()
        movie_section.type = "movie"
        movie_section.key = "1"
        movie_section.title = "Movies"
        show_section = MagicMock()
        show_section.type = "show"
        show_section.key = "2"
        show_section.title = "TV Shows"
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section, MediaType.SHOW: show_section}
        ctx.config.candidates_pre_rank = 5  # a tiny cut, so crowding-out is easy to trigger
        movies = {900: 999, **{i: 1000 + i for i in range(1, 60)}}
        shows = {5000: 5999, 5001: 5001}
        ctx.plex.build_library_index.side_effect = lambda s: movies if s is movie_section else shows
        ctx.config.rows = [RowSpec(slug="tv", name_template="TV Picks", size=2, media="show")]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        # 59 high-rated movies flood the pool and ONE lower-rated show — from the SAME source, so a
        # source quota can't rescue it. Only filtering by media BEFORE the cut can.
        def suggestions(tid, mt):  # returns (item, affinity) pairs
            if mt is MediaType.MOVIE:
                return _ranked(
                    [{"id": i, "title": f"Movie {i}", "genre_ids": [], "vote_average": 9.0} for i in range(1, 60)]
                )
            return _ranked([{"id": 5001, "title": "A Show", "genre_ids": [], "vote_average": 6.0}])

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.config.candidate_sources = ["tmdb_similar"]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        # A show seed so the SHOW media type is in play at all (typed as a SHOW, or no show seed is
        # derived and tmdb_discover is never asked for shows).
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Breaking Bad", days_ago=2, rating_key=5999, media_type=MediaType.SHOW),
        ]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        offered_ids = [c.tmdb_id for call in offered for c in call]
        assert offered_ids, "the shows-only row was offered no candidates at all"
        assert all(i >= 5000 for i in offered_ids), f"a shows-only row was offered movies: {offered_ids}"

    def test_one_rows_dead_source_does_not_kill_the_users_other_rows(self, ctx: EngineContext, mock_plextv):
        """A row pinned to a single source (Trakt-only) whose source is down must fail alone. It used
        to raise out of the whole user, so their healthy rows delivered nothing either."""
        trakt = MagicMock()
        trakt.related.side_effect = RuntimeError("trakt 502")
        ctx.trakt = trakt
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5),  # inherits the (working) global sources
            RowSpec(slug="next", name_template="What to watch next", size=5, candidate_sources=["trakt"]),
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].status == "ok", "a healthy row's user must not be failed by a dead sibling"
        assert {p.collection_slug for p in report.users[0].picks} == {"picked"}

    def test_a_user_whose_every_source_is_down_is_an_error_not_a_cheerful_ok(self, ctx: EngineContext, mock_plextv):
        """The other half: if nothing worked, we know nothing about this person — reporting ok would
        leave yesterday's row in place and call it a success."""
        ctx.tmdb.suggestions.side_effect = RuntimeError("tmdb 429")
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_similar"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].status == "error"
        assert "429" in report.users[0].error

    def test_disabling_every_row_delivers_nothing(self, ctx: EngineContext, mock_plextv):
        """When the server manages rows (rows_defined=True), an empty row list means every row is
        DISABLED — deliver nothing. It used to resurrect the synthesized default for everyone."""
        ctx.config.rows = []
        ctx.config.rows_defined = True
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()

    def test_an_unconfigured_run_still_gets_a_default_row(self, ctx: EngineContext, mock_plextv):
        """A caller that doesn't manage rows (rows_defined=False) passing an empty list means
        'unconfigured' — synthesize the legacy default so a bare engine run still builds a row."""
        ctx.config.rows = []
        ctx.config.rows_defined = False
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.create_collection.assert_called_once()
        section, title, items = ctx.plex.create_collection.call_args.args
        assert section.type == "movie"  # the only library this ctx fixture configures
        assert title == "✨ Movies Picked for You" + row_marker(100)  # the synthesized default row's title
        assert len(items) == 2  # both mocked TMDB candidates (Candidate Ten, Candidate Twenty) — not empty, not more

    def test_a_both_row_fills_each_library_to_its_own_size(self, ctx: EngineContext, mock_plextv):
        """A 'both' row delivers a movie collection AND a show collection, and each library fills to
        its own size. One shared budget split by what the curator picked left a mostly-TV watcher with
        a full show row and a one-item movie row."""
        movie_section = MagicMock()
        movie_section.type = "movie"
        movie_section.key = "1"
        movie_section.title = "Movies"
        show_section = MagicMock()
        show_section.type = "show"
        show_section.key = "2"
        show_section.title = "TV Shows"
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section, MediaType.SHOW: show_section}
        movies = {900: 999, **{i: 1000 + i for i in range(1, 40)}}
        shows = {5000: 5999, **{5000 + i: 6000 + i for i in range(1, 40)}}
        ctx.plex.build_library_index.side_effect = lambda sec: movies if sec is movie_section else shows

        def suggestions(tid, mt):  # returns (item, affinity) pairs
            # Plenty of BOTH movie and show candidates in the pool.
            base = 1 if mt is MediaType.MOVIE else 5000
            return _ranked(
                [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(1, 40)]
            )

        ctx.tmdb.suggestions.side_effect = suggestions
        # A watcher of one movie + one show, so both media types seed.
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),
            make_watched("Breaking Bad", days_ago=2, rating_key=5999, media_type=MediaType.SHOW),
        ]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=10, media="both")]
        ctx.config.min_history = 1  # 2 watches is enough here — exercise the real curate path, not cold start
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        movie_picks = [p for p in picks if p.media_type is MediaType.MOVIE]
        show_picks = [p for p in picks if p.media_type is MediaType.SHOW]
        assert len(movie_picks) == 10, f"movie row should fill to 10, got {len(movie_picks)}"
        assert len(show_picks) == 10, f"show row should fill to 10, got {len(show_picks)}"

    def test_a_row_builds_each_library_from_that_librarys_own_contents(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Two libraries of the SAME media type each get their OWN full row, built only from the
        titles that library holds — not one recommendation split between them. This is what makes a
        row 'per library': a server with a Movies and a 4K library fills both, from their own shelves.
        """
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        # Disjoint catalogues: Movies holds tmdb 10-15, 4K holds tmdb 50-55 (seed 900 in both).
        idx_std = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k
        # The candidate pool spans BOTH libraries' titles; each library must pick only its own.
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50  # keep the whole 12-title pool; don't truncate either library
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        # One build_picks call per library, each seeing ONLY that library's tmdb ids.
        seen = [{c.tmdb_id for c in call} for call in offered]
        assert {10, 11, 12, 13, 14, 15} in seen, f"Movies library should build from its own ids, saw {seen}"
        assert {50, 51, 52, 53, 54, 55} in seen, f"4K library should build from its own ids, saw {seen}"

    def test_run_records_a_breakdown_entry_per_library(self, ctx: EngineContext, mock_plextv):
        """The per-user report carries a per-(row, library) breakdown so the UI can show 'added X to
        Movies, Y to TV' with each library's own picks — not one merged list."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx_std = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        breakdown = report.users[0].breakdown
        by_library = {e["library_title"]: e for e in breakdown}
        assert set(by_library) == {"Movies", "4K Movies"}, f"one entry per library, got {list(by_library)}"
        for entry in breakdown:
            assert entry["row_slug"] == "picked"
            assert len(entry["picks"]) == 5, "each library's row has its own full set of picks"
            assert [p["rank"] for p in entry["picks"]] == [1, 2, 3, 4, 5], "picks ranked 1..k within the library"

    def _movie_row_ctx(self, ctx, freshness, run_day):
        """A single Movies library holding tmdb 10-19, one 'picked' movie row at the given freshness."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx = {900: 999, **{i: 1000 + i for i in range(10, 20)}}
        ctx.plex.build_library_index.return_value = idx
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie", freshness=freshness)]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        ctx.run_day = run_day  # a real day; 0 is the tests/direct "always refresh" sentinel

    def _prior_movies(self, tmdb_ids):
        return [
            Pick(
                tmdb_id=t,
                rating_key=0,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
            )
            for i, t in enumerate(tmdb_ids)
        ]

    def test_non_refresh_night_reuses_prior_picks_without_rebuilding(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Freshness 0 = a frozen row: after the first build it redelivers last run's picks unchanged
        and never rebuilds the row (no wasted work, and delivery's unchanged-skip avoids the Plex
        write too) — the fix for nightly churn."""
        self._movie_row_ctx(ctx, freshness=0.0, run_day=5)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built == []  # reused, not rebuilt
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [12, 13, 14, 15, 16]  # exactly last run's row, in order

    def test_refresh_night_keeps_the_strong_two_thirds_and_swaps_the_rest(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """On a refresh night the strongest ~two-thirds carry over and the rest are swapped for titles
        NOT already in the row, so a just-rotated-out pick can't immediately bounce back."""
        self._movie_row_ctx(ctx, freshness=1.0, run_day=5)  # 1.0 = refresh every night
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built  # a refresh night DOES rebuild the swapped-in slots
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert {12, 13, 14} <= set(ids), f"the strongest two-thirds of last run's row survive, got {ids}"
        assert {15, 16}.isdisjoint(ids), f"the weakest third is swapped out, got {ids}"
        assert not {15, 16} & set(ids), f"a just-rotated-out pick can't bounce straight back, got {ids}"

    def test_refresh_night_lets_a_newcomer_outrank_a_survivor(self, ctx: EngineContext, mock_plextv):
        """Survivors and newcomers are ranked TOGETHER against tonight's pool, so a better newcomer
        takes the head of the row. Concatenating `kept + new` instead pinned last run's top
        two-thirds to positions 1..keep_n for ever — on a 20-title row, 13 slots that never moved
        again however the candidates scored."""
        self._movie_row_ctx(ctx, freshness=1.0, run_day=5)
        # Last run held the pool's WEAKER half; 10 and 11 rank above all of them tonight.
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert ids == [10, 11, 12, 13, 14], f"row ordered by tonight's ranking, not last run's, got {ids}"
        assert ids[0] not in {12, 13, 14, 15, 16}, f"a newcomer can reach position 1, got {ids}"
        assert [p["rank"] for p in picks] == [1, 2, 3, 4, 5], "ranks renumbered to the delivered order"

    def _named_row_ctx(self, ctx, *, freshness: float, max_seeds: int = 1):
        """The `_movie_row_ctx` world, but with a row NAMED after the watch it is built from."""
        self._movie_row_ctx(ctx, freshness=freshness, run_day=5)
        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="Because you watched {top_seed}",
                size=5,
                media="movie",
                freshness=freshness,
                max_seeds=max_seeds,
            )
        ]

    def _prior_seeded_by(self, tmdb_ids, *, seed_tmdb_id: int, seed_title: str):
        return [replace(p, seed_tmdb_id=seed_tmdb_id, seed_title=seed_title) for p in self._prior_movies(tmdb_ids)]

    def test_a_named_row_rebuilds_when_the_seed_it_names_has_changed(self, ctx: EngineContext, mock_plextv):
        """A `{top_seed}` row's title renders from pick #1's seed, and the refresh branch always
        carries pick #1 forward — so without the seed check the row stays named after the FIRST watch
        that ever seeded it while its tail fills from newer ones. This person's only seed is Fargo."""
        self._named_row_ctx(ctx, freshness=1.0)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        titles = [strip_marker(t) for t in report.users[0].placement_titles]
        assert titles == ["Because you watched Fargo"]
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert {p["seed_title"] for p in picks} == {"Fargo"}, "every pick answers to the seed the row names"

    def test_a_named_row_carries_forward_while_its_seed_is_unchanged(self, ctx: EngineContext, mock_plextv):
        """The seed check must not turn every refresh into a full rebuild: while the row is still
        built from the seed it is named after, the normal keep-two-thirds carry-forward applies."""
        self._named_row_ctx(ctx, freshness=1.0)
        # 900 is what "Fargo" resolves to in this fixture, so the seed has NOT moved.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([12, 13, 14, 15, 16], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert {12, 13, 14} <= set(ids), f"unchanged seed keeps the normal carry-forward, got {ids}"
        titles = [strip_marker(t) for t in report.users[0].placement_titles]
        assert titles == ["Because you watched Fargo"]

    def test_a_named_row_rebuilds_when_RANKING_moves_the_seed_its_title_uses(self, ctx: EngineContext, mock_plextv):
        """The cell the single-seed tests could never reach: a `{top_seed}` row with MORE than one seed.

        `_seed_moved` asks whether the POOL still leads with the named seed. The title asks something
        subtly different — it renders from the best-matching DELIVERED pick — so re-ranking survivors
        against newcomers can put a differently-seeded newcomer first while the pool's top seed never
        moved. The row then renamed itself while still carrying the old seed's picks, which is the
        stale claim the whole mechanism exists to prevent.
        """
        self._two_seed_named_row_ctx(ctx, "best")
        # Last run's row is seeded by Fargo and carries Fargo's weaker (F1x) titles, so tonight's
        # ranking hands the lead to a Chernobyl-seeded newcomer.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([10, 11, 12, 13, 14], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        titles = [strip_marker(t) for t in report.users[0].placement_titles]
        assert titles == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"
        # Not "every pick shares that seed" — above one seed a `{top_seed}` row names its strongest
        # watch and legitimately holds others, which is the trade-off the seed-budget callout warns
        # about. The guarantee is narrower and is the one that was broken: the row never keeps
        # claiming a watch it is no longer led by.
        assert lead["seed_title"] in {p["seed_title"] for p in picks}
        assert titles != ["Because you watched Fargo"] or lead["seed_title"] == "Fargo", (
            f"the title cannot outlive the seed that earned it, got {titles} with lead {lead}"
        )

    def test_an_unnamed_row_ignores_the_seed_check(self, ctx: EngineContext, mock_plextv):
        """A row that names no seed keeps the cheap carry-forward however far its seeds have drifted —
        re-deriving a normal 30-seed row on any seed change would make every refresh a full rebuild."""
        self._movie_row_ctx(ctx, freshness=1.0, run_day=5)  # name_template="" — names no seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert {12, 13, 14} <= {p["tmdb_id"] for p in picks}, "seed drift alone does not rebuild an unnamed row"

    def test_a_named_row_follows_its_seed_even_when_stored_frozen(self, ctx: EngineContext, mock_plextv):
        """A `{top_seed}` row ignores a stored freshness — even 0.0, which freezes any other row.

        A row whose title names a watch is ABOUT recency, so a slow cadence makes it claim a watch the
        person moved on from days ago (issue #57: "it still says Because you watched Little Brother",
        reported twice). Forced rather than merely defaulted because the row editor HIDES the freshness
        control for these rows — honouring a slow value saved before that would strand the row with
        nothing in the UI to explain it or undo it.
        """
        self._named_row_ctx(ctx, freshness=0.0)  # 0.0 freezes any row that does NOT name its seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        titles = [strip_marker(t) for t in report.users[0].placement_titles]
        assert titles != ["Because you watched Chernobyl"], "a frozen cadence must not strand the title"
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        assert titles == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"

    def test_an_unnamed_row_still_freezes_at_zero_freshness(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The freshness override is scoped to rows that name a seed. Everywhere else 0.0 still means
        "never refresh once built" — the control is still offered for those rows, so it must still work."""
        self._movie_row_ctx(ctx, freshness=0.0, run_day=5)  # name_template="" — names no seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built == [], "a frozen row is redelivered, never rebuilt"
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [12, 13, 14, 15, 16]

    def _ordered_row_ctx(self, ctx, pick_order: str, *, run_day: int = 5):
        """`_movie_row_ctx`, but the pool carries DISTINCT ratings and years so an order is visible.

        tmdb 10..19 get descending ratings (10 is best) and ascending years (19 is newest), so
        "rating" and "newest" produce opposite orders and neither can be confused with the ranking.
        """
        self._movie_row_ctx(ctx, freshness=1.0, run_day=run_day)
        pool = [
            {
                "id": i,
                "title": f"T{i}",
                "genre_ids": [],
                "vote_average": 9.5 - (i - 10) * 0.5,
                "release_date": f"{2000 + i}-01-01",
            }
            for i in range(10, 20)
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie", pick_order=pick_order)]

    def _delivered_ids(self, report):
        """The row as DELIVERED, in the order it is written to Plex.

        `rank` is deliberately NOT the delivered position: it is stamped from the selection order and
        means "how good a match", which is what names a `{top_seed}` row and what carry-forward keeps
        the strongest two-thirds by. So every pick still carries a distinct 1..n rank, but for any
        order other than "best" those ranks are a permutation of the delivered order, not equal to it.
        """
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ranks = [p["rank"] for p in picks]
        assert sorted(ranks) == list(range(1, len(picks) + 1)), f"each pick keeps a distinct match rank, got {ranks}"
        return [p["tmdb_id"] for p in picks]

    def test_pick_order_best_leaves_the_ranking_alone(self, ctx: EngineContext, mock_plextv):
        """The default must be a genuine no-op — it is what every existing row is migrated to."""
        self._ordered_row_ctx(ctx, "best")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        assert self._delivered_ids(pipeline_mod.run(ctx, [sarah])) == [10, 11, 12, 13, 14]

    def test_pick_order_rating_puts_the_best_scored_first(self, ctx: EngineContext, mock_plextv):
        self._ordered_row_ctx(ctx, "rating")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"descending TMDB score, got {ids}"

    def test_pick_order_newest_puts_the_most_recent_release_first(self, ctx: EngineContext, mock_plextv):
        """Asserted as its own case, not just 'not the rating order': the two are deliberately
        opposite in this fixture, so a mix-up between them would otherwise pass one of the tests."""
        self._ordered_row_ctx(ctx, "newest")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        assert ids == sorted(ids, reverse=True), f"newest release first, got {ids}"

    def test_pick_order_shuffle_is_stable_within_a_day_and_moves_between_days(self, ctx: EngineContext, mock_plextv):
        """Shuffle is a hash of (row, user, day), never `random`: a re-run the same night must
        reproduce the same row (or every retry rewrites the collection), and the next day must not."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        day5 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        day5_again = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "shuffle", run_day=6)
        day6 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert day5 == day5_again, "a re-run on the same night reproduces the same order"
        assert day5 != day6, f"the order moves day to day, got {day5} both days"
        assert sorted(day5) == sorted(day6), "shuffling reorders the row, it never changes membership"

    def test_pick_order_shuffle_differs_between_two_users_on_the_same_day(self, ctx: EngineContext, mock_plextv):
        """Keyed on the user as well as the day, so two people's copies of one row don't shuffle in
        lockstep — otherwise the whole server shows the same 'random' order every night."""
        self._ordered_row_ctx(ctx, "shuffle")
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        by_user = {
            u.slug: [p["tmdb_id"] for e in u.breakdown if e["library_title"] == "Movies" for p in e["picks"]]
            for u in report.users
        }
        assert by_user["sarah"] != by_user["mike"], f"per-user shuffle, got {by_user}"

    def test_pick_order_shuffle_reorders_a_frozen_row_without_rebuilding_it(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Shuffle on a row that never refreshes — the combination that makes the feature worth
        having, and the one that exercises delivery's unchanged-membership write-skip. Ordering is
        applied on the carry-forward path too, so the row moves without a single curator call; the
        deferred order pass then carries the new order to Plex."""
        prior = self._prior_movies([12, 13, 14, 15, 16])
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        ctx.config.rows[0] = replace(ctx.config.rows[0], freshness=0.0)  # 0.0 = never refresh
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        built = spy_build_picks(monkeypatch)
        day5 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        self._ordered_row_ctx(ctx, "shuffle", run_day=6)
        ctx.config.rows[0] = replace(ctx.config.rows[0], freshness=0.0)
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        day6 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "a frozen row still never rebuilds — ordering is presentation, not selection"
        assert sorted(day5) == sorted([12, 13, 14, 15, 16]), f"membership is exactly last run's, got {day5}"
        assert day5 != day6, f"the frozen row's ORDER still moves day to day, got {day5} both days"

    def test_pick_order_new_first_leads_with_the_titles_that_arrived_this_run(self, ctx: EngineContext, mock_plextv):
        """Issue #63's first ask. The prior row holds the pool's STRONGEST five, so the survivors are
        exactly what `best` would put in front — if this passed with the newcomers already sorting
        first, the order would be indistinguishable from the ranking and the test would prove nothing.

        On a refresh night the branch keeps 3 of 5 survivors (10, 11, 12) and swaps in the next two
        candidates (15, 16); `new_first` has to invert that.
        """
        self._ordered_row_ctx(ctx, "new_first")
        # `_ordered_row_ctx` rebuilds the RowSpec without a freshness, so it inherits the config's
        # 0.0 — "never refresh". This case is about the refresh branch, so ask for one.
        ctx.config.rows[0] = replace(ctx.config.rows[0], freshness=1.0)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([10, 11, 12, 13, 14])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == [15, 16, 10, 11, 12], f"newcomers first, survivors after, each in rank order — got {ids}"

    def test_pick_order_new_first_is_a_no_op_when_nothing_arrived(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A carried-forward night has no newcomers, so the row must sit still rather than scramble.

        Without this, "new" defaulting to the whole row (or to none of it, sorted unstably) would
        reorder a row on nights nothing changed — the one thing `freshness` exists to avoid, and a
        Plex write for no reason.
        """
        self._ordered_row_ctx(ctx, "new_first", run_day=5)
        ctx.config.rows[0] = replace(ctx.config.rows[0], freshness=0.0)  # 0.0 = never refresh
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "a frozen row is redelivered, never rebuilt"
        assert ids == [12, 13, 14, 15, 16], f"nothing arrived, so nothing moves — got {ids}"

    def test_pick_order_rotate_advances_the_front_by_one_title_a_day(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Issue #63's second ask, and the property that makes it worth having: the front changes on a
        row that never rebuilds. Asserted against exact rotations, not just "day 5 != day 6", because
        the point is that the row stays in its ranking's relative order while the head advances — a
        shuffle would also pass an inequality check.

        Rotating rather than evicting is what keeps this in the display layer. Dropping the head
        instead would need a persisted position that `rank` (match quality) cannot carry without
        breaking `render_row_name` and `_seed_moved`.
        """
        prior = self._prior_movies([12, 13, 14, 15, 16])
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)
        seen = {}

        for day in (5, 6, 7):
            self._ordered_row_ctx(ctx, "rotate", run_day=day)
            ctx.config.rows[0] = replace(ctx.config.rows[0], freshness=0.0)
            ctx.previous_picks = {("sarah", "picked", "1"): prior}
            seen[day] = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "the front moves without a rebuild — ordering is presentation, not selection"
        assert seen[5] == [12, 13, 14, 15, 16], f"day 5 (5 % 5 = 0) starts at the top, got {seen[5]}"
        assert seen[6] == [13, 14, 15, 16, 12], f"day 6 advances the front by one, got {seen[6]}"
        assert seen[7] == [14, 15, 16, 12, 13], f"day 7 advances it again, got {seen[7]}"

    def test_pick_order_rotate_reproduces_the_same_order_within_a_day(self, ctx: EngineContext, mock_plextv):
        """Same guarantee `shuffle` needs: a retry the same night must not rewrite the collection."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "rotate", run_day=7)
        first = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "rotate", run_day=7)
        second = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert first == second, f"a re-run on the same night reproduces the row, got {first} then {second}"

    def _two_seed_named_row_ctx(self, ctx, pick_order: str, *, run_day: int = 5):
        """A `{top_seed}` row seeded by TWO watches, whose candidates sort differently by each order.

        One seed is required per distinct `seed_title` in the row — with a single seed every pick
        carries the same one and NO ordering could ever change the rendered title, which is exactly
        what made an earlier version of this test pass against the bug it was written to catch.

        Fargo's candidates are old and poorly rated; Chernobyl's are new and highly rated. So "rating"
        and "newest" both put a Chernobyl-seeded pick first, while the ranking does not.
        """
        self._movie_row_ctx(ctx, freshness=1.0, run_day=run_day)
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),
            make_watched("Chernobyl", days_ago=2, rating_key=998),
        ]
        ctx.plex.build_library_index.return_value = {900: 999, 555: 998, **{i: 1000 + i for i in range(10, 20)}}
        by_seed = {
            900: [
                {"id": i, "title": f"F{i}", "genre_ids": [], "vote_average": 5.0, "release_date": "1996-01-01"}
                for i in range(10, 15)
            ],
            555: [
                {"id": i, "title": f"C{i}", "genre_ids": [], "vote_average": 9.5, "release_date": "2024-01-01"}
                for i in range(15, 20)
            ],
        }
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(by_seed.get(tid, []))
        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="Because you watched {top_seed}",
                size=5,
                media="movie",
                freshness=1.0,
                pick_order=pick_order,
            )
        ]

    def test_a_named_rows_title_is_the_same_whatever_order_it_is_displayed_in(self, ctx: EngineContext, mock_plextv):
        """The cell where display order and match quality could be confused: a `{top_seed}` row that
        also chooses its own order.

        The title renders from the BEST-MATCHING pick, never from whichever pick sorted first. A row
        is named after the watch it was built from, and that does not change because the owner asked
        for the titles in a different sequence. Reading `picks[0]` instead, this row renamed itself
        whenever the order put another seed's pick on top — and a shuffled one did so most nights,
        rewriting its title on Plex each time.

        Asserted as "all four agree" rather than against a hardcoded name, so the test states the
        invariant that matters and cannot be satisfied by one order happening to match a literal.
        """
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        titles = {}
        for pick_order in ("best", "rating", "newest", "shuffle"):
            self._two_seed_named_row_ctx(ctx, pick_order)
            report = pipeline_mod.run(ctx, [sarah])
            titles[pick_order] = [strip_marker(t) for t in report.users[0].placement_titles]

        assert len({tuple(t) for t in titles.values()}) == 1, f"the order must not rename the row, got {titles}"
        # Tied back to the data rather than a literal name: whichever seed wins the ranking, the title
        # must be the one carried by the pick ranked #1 — that is what "named after its seed" means.
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        assert titles["best"] == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"

    def test_the_display_order_still_changes_which_pick_leads_the_row(self, ctx: EngineContext, mock_plextv):
        """The other half of the invariant above: the ORDER genuinely does change the delivered row,
        so 'the title never moves' is not passing merely because ordering did nothing here."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._two_seed_named_row_ctx(ctx, "best")
        best = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._two_seed_named_row_ctx(ctx, "rating")
        by_rating = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert best != by_rating, f"ordering changes the delivered row, got {best} vs {by_rating}"
        # The ranking interleaves seeds so each taste is represented; ordering by rating does not, so
        # the 9.5-rated (Chernobyl-seeded) picks group ahead of the 5.0-rated (Fargo-seeded) ones.
        assert by_rating[:3] == sorted(by_rating[:3]) and min(by_rating[:3]) >= 15, (
            f"the highly-rated picks lead as a block, got {by_rating}"
        )
        assert sorted(best) == sorted(by_rating), "ordering rearranges the row, it never changes membership"

    @pytest.mark.parametrize("pick_order", ["rating", "newest"])
    def test_rank_records_match_quality_not_the_delivered_position(self, pick_order, ctx: EngineContext, mock_plextv):
        """`rank` is stamped BEFORE the display order is applied, so for any order but "best" the
        delivered sequence and the ranks disagree.

        This is the guarantee the two `{top_seed}` bugs came from breaking. `rank` is what
        `render_row_name` names the row from and what `previous_picks` is ordered by — so if it were
        stamped after ordering, a shuffled row would rename itself nightly and `_seed_moved` would
        compare against an arbitrary pick and rebuild the row every refresh night.
        """
        self._two_seed_named_row_ctx(ctx, pick_order)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ranks = [p["rank"] for p in picks]
        assert sorted(ranks) == list(range(1, len(picks) + 1)), f"every pick keeps a distinct rank, got {ranks}"
        assert ranks != sorted(ranks), (
            f"{pick_order} reorders the row, so rank must NOT follow the delivered position — got {ranks}"
        )

    @pytest.mark.parametrize("pick_order", ["rating", "newest", "shuffle"])
    def test_a_named_row_carries_forward_whatever_order_it_is_displayed_in(
        self, pick_order, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """`_seed_moved` compares against the best-matching prior pick, which `previous_picks` returns
        first because it is ordered by the persisted rank column. Comparing against the DISPLAYED
        first pick instead made this row look reseeded every refresh night, so it rebuilt for ever and
        carry-forward silently stopped applying to every non-default order."""
        self._ordered_row_ctx(ctx, pick_order)
        ctx.config.rows[0] = replace(ctx.config.rows[0], name_template="Because you watched {top_seed}", max_seeds=1)
        # Seeded by 900 ("Fargo"), which is still this person's only seed — so the seed has NOT moved.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([12, 13, 14, 15, 16], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert {12, 13, 14} <= set(ids), f"{pick_order} row still carries its strongest two-thirds, got {ids}"

    def test_pick_order_sorts_picks_missing_the_value_last(self, ctx: EngineContext, mock_plextv):
        """Carried-forward picks delivered before 0056 have no rating or year. They must sort last
        and keep their order, so such a row degrades to its ranking for one cycle rather than
        scrambling — and the run must not raise on the None."""
        from shortlist.engine.rows import _apply_order

        picks = [
            Pick(tmdb_id=1, rating_key=0, title="no data A", rank=1, reason="", media_type=MediaType.MOVIE),
            Pick(tmdb_id=2, rating_key=0, title="rated", rank=2, reason="", media_type=MediaType.MOVIE, rating=8.0),
            Pick(tmdb_id=3, rating_key=0, title="no data B", rank=3, reason="", media_type=MediaType.MOVIE),
        ]

        by_rating = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5)
        by_year = _apply_order(picks, "newest", row_slug="r", user_slug="u", run_day=5)

        assert [p.tmdb_id for p in by_rating] == [2, 1, 3], "the rated pick leads; the rest keep their order"
        assert [p.tmdb_id for p in by_year] == [1, 2, 3], "no years at all leaves the order untouched"

    def test_a_shared_row_also_records_a_breakdown(self, ctx: EngineContext, mock_plextv):
        """A shared 'popular on this server' row records a per-library breakdown too, keyed by its own
        slug — so the run detail groups a public row the same way it groups a private one."""
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Both watch the same title, so it clears the 2-distinct-watchers floor for a public row.
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        assert shared_report.breakdown, "the shared row records a breakdown"
        assert all(e["row_slug"] == "popular" for e in shared_report.breakdown)

    def test_a_shared_row_honours_the_server_wide_block_list(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The shared path called `derive_seeds` with no `blocked` at all, so blocks simply did not
        apply to public rows.

        Asserted on the ARGUMENT rather than on the resulting picks: the picks depend on what TMDB
        returns for the surviving seeds, so a "no picks" assertion passes for a dozen reasons that
        have nothing to do with blocking (verified — it passed with the fix removed).
        """
        import shortlist.engine.rows as rows_mod

        captured: list[set[int] | None] = []
        real = rows_mod.derive_seeds

        def spy(*args, **kwargs):
            captured.append(kwargs.get("blocked"))
            return real(*args, **kwargs)

        monkeypatch.setattr(rows_mod, "derive_seeds", spy)

        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        ctx.config.blocked_shared_seeds = {4242}
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999, tmdb_id=4242)]

        pipeline_mod.run(ctx, [sarah, mike])

        assert {4242} in captured, "the shared row derived its seeds without the server-wide block list"

    def test_one_persons_block_does_not_reshape_the_shared_row(self, ctx: EngineContext, mock_plextv):
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        sarah.blocked_seeds = {4242}  # sarah's own preference
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999, tmdb_id=4242)]

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        assert shared_report.status != "skipped", "sarah's private block silently emptied a public row"

    def test_per_person_tokens_come_from_the_web_search_source_and_land_under_its_step(
        self, ctx: EngineContext, mock_plextv
    ):
        """The ONLY AI cost now is finding titles: the ``llm_web`` source. A run using it records that
        source's tokens into the user total AND under its own step bucket. Ranking/pick selection is
        code (picker.build_picks) with no LLM, so there is no 'curate' step and no per-row token spend."""

        class _WebCurator:
            supports_native_web_search = True
            last_tokens = 50  # the tokens the one web-search LLM call reports

            def recommend_web(self, profile, seeds, k):
                return [{"title": "Web Pick", "year": 2020, "media": "movie"}]

        ctx.curator = _WebCurator()
        ctx.config.candidate_sources = ["tmdb_similar", "llm_web"]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
                {"id": 20, "title": "Fresh Twenty", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        # The web source's proposed title resolves to a real TMDB id, so llm_web actually contributes.
        ctx.tmdb.search.side_effect = lambda title, mt, year=None: (
            {"id": 30, "title": "Web Pick", "genre_ids": [], "vote_average": 8.5} if title == "Web Pick" else None
        )
        ctx.plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020, 30: 1030}

        report = pipeline_mod.run(ctx, [sarah])

        u = report.users[0]
        assert u.llm_tokens == 50
        # Tokens are attributed to the SOURCE that spent them (llm_web), not a curate step.
        assert u.llm_tokens_by_step == {"llm_web": 50}
        assert u.exa_searches == 0  # native web search, no external Exa backend
        # No per-row LLM spend anymore: breakdown entries carry no token key.
        assert u.breakdown and all("llm_tokens" not in e for e in u.breakdown)

    def test_a_cancelled_run_skips_every_remaining_user(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A cancel signalled before delivery skips every user's gather/build/deliver — no pick work,
        no picks — and each is marked 'skipped'. An in-flight user isn't interrupted mid-work (the
        check is per-user), so this never leaves a half-applied user."""
        ctx.cancelled = lambda: True
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert [u.status for u in report.users] == ["skipped", "skipped"]
        assert not any(u.picks for u in report.users)
        assert built == []  # cancelled before any gather/build ran

    def test_a_partial_cancel_still_merges_filters_and_promotes_the_delivered_user(
        self, ctx: EngineContext, mock_plextv
    ):
        """Leak-safety under cancel: cancel firing AFTER the first user must still deliver that user,
        hide their row on every OTHER account, and promote it — while the rest are skipped. The
        merge covering a NON-delivered account is the exact guarantee that a cancel can't leave a
        delivered row visible to the wrong person."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Cancel becomes true after the first per-user check: sarah delivers, mike (and shared) skip.
        seen = {"n": 0}

        def cancelled() -> bool:
            seen["n"] += 1
            return seen["n"] > 1

        ctx.cancelled = cancelled

        created_by_label: dict[str, MagicMock] = {}

        def stored_label(collection, label):
            created_by_label[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = lambda section, title, items: MagicMock()
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [created_by_label[label.lower()]] if label.lower() in created_by_label else []
        )

        report = pipeline_mod.run(ctx, [sarah, mike])

        statuses = {u.slug: u.status for u in report.users}
        assert statuses["sarah"] == "ok" and statuses["mike"] == "skipped"
        assert ctx.plex.create_collection.call_count == 1  # only the delivered user built a row
        # Leak-safe: mike (NOT delivered this run) still had sarah's delivered row excluded from his
        # share — the privacy merge covered every account, not just the ones built.
        mike_filters = next(u for u in mock_plextv.users if u.id == 200).filters
        assert mike_filters["filterMovies"] == "label!=Shortlist_sarah"
        assert ctx.plex.promote.call_count == 1  # only the delivered user was promoted

    def test_default_watched_cap_excludes_finished_titles(self, ctx: EngineContext, mock_plextv):
        """watched_pct defaults to 0 (all fresh): a title the user has finished, even if it resurfaces
        as a candidate, is never recommended back. Guards the pool_key/pools_for `== 0` branch — an
        inversion there would recommend everyone their already-watched titles and pass every leaf test.
        """
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        # She finished movie 900 (the seed, ratingKey 999). It resurfaces as a candidate — must drop.
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 900, "title": "Already Finished", "genre_ids": [], "vote_average": 9.0},
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
                {"id": 20, "title": "Fresh Twenty", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        ctx.plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}

        report = pipeline_mod.run(ctx, [sarah])

        ids = {p.tmdb_id for p in report.users[0].picks}
        assert 900 not in ids, "a finished title must never be recommended at the 0% default"
        assert ids & {10, 20}, "fresh candidates still fill the row"

    def test_watched_pct_of_one_lets_finished_non_seed_titles_through(self, ctx: EngineContext, mock_plextv):
        """At 100% there is no filtering: a finished title (that isn't itself a seed) stays in the pool
        AND may be delivered. Guards the opposite inversion of the `== 0` branch. The seed is always
        excluded regardless — you don't re-recommend the exact thing just watched."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.config.max_seeds = 1  # only movie 900 becomes a seed; movie 50 stays a finished non-seed
        ctx.config.min_history = 1
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=10, media="both", watched_pct=1.0)]
        ctx.history_source.fetch.return_value = [
            make_watched("Seed Movie", days_ago=1, rating_key=999),  # tmdb 900 — the sole seed
            make_watched("Finished Extra", days_ago=9, rating_key=550),  # tmdb 50 — finished, not a seed
        ]
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 50, "title": "Finished Extra", "genre_ids": [], "vote_average": 9.0},  # finished, resurfaced
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
            ]
        )
        ctx.plex.build_library_index.return_value = {900: 999, 50: 550, 10: 1010}

        report = pipeline_mod.run(ctx, [sarah])

        ids = {p.tmdb_id for p in report.users[0].picks}
        assert 50 in ids, "at 100% a finished (non-seed) title may still be recommended"
        assert 900 not in ids, "the seed itself is always excluded"

    def test_muting_removes_an_already_delivered_row(self, ctx: EngineContext, mock_plextv):
        from shortlist.engine.delivery import render_row_name, row_marker

        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        # A collection already on the server for this row (title = display + the account's marker). The
        # default template renders {library_name} from the delivering library ("Movies" in this ctx).
        display = render_row_name(ctx.config.row_name_template, sarah, [], library_name="Movies")
        existing = MagicMock()
        existing.title = display + row_marker(100)
        ctx.plex.find_owned_collections.return_value = [existing]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert display in report.users[0].diff.deleted
        ctx.plex.create_collection.assert_not_called()  # muted -> nothing rebuilt

    def test_a_disabled_rows_collection_is_removed_from_its_owners_home(self, ctx: EngineContext, mock_plextv):
        """A row switched OFF in the UI still sat on its owner's Home (excluded from everyone else, so
        private — just not gone). The server hands disabled rows to the engine as retired_rows, which
        removes them like a mute — so 'off' means gone, not merely 'not refreshed'."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RowSpec

        # No enabled rows at all — the user's every row was disabled. Removal must still happen (it
        # sits before the "no rows -> return" check).
        ctx.config.rows = []
        ctx.config.rows_defined = True
        ctx.config.retired_rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5)]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        target = MagicMock()
        target.title = "Hidden Gems" + row_marker(100)
        # A DIFFERENT-titled collection under the same label must NOT be touched — removal matches by
        # title, so the guard has to be load-bearing.
        bystander = MagicMock()
        bystander.title = ctx.config.row_name_template + row_marker(100)
        ctx.plex.find_owned_collections.return_value = [target, bystander]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is target  # exactly the retired row
        assert "Hidden Gems" in report.users[0].diff.deleted
        ctx.plex.create_collection.assert_not_called()


class TestRequestsWiring:
    """The request pass only runs when enabled, and it sees the titles the library lacks."""

    def _suggest_a_missing_title(self, ctx: EngineContext) -> None:
        # Candidate 30 is NOT in the library index (which holds only 10 and 20), so it's requestable.
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 10, "title": "In Library", "genre_ids": [], "vote_average": 8.0, "vote_count": 900},
                {"id": 30, "title": "Missing Title", "genre_ids": [], "vote_average": 8.4, "vote_count": 800},
            ]
        )

    def test_disabled_by_default_never_calls_the_request_pass(self, ctx: EngineContext, mock_plextv, monkeypatch):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._suggest_a_missing_title(ctx)
        called = []
        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", lambda *a, **k: called.append(a))

        report = pipeline_mod.run(ctx, [sarah])

        assert called == []  # requests is None on the config -> no bookkeeping, no pass
        assert report.requests is None

    def test_enabled_run_feeds_missing_titles_to_the_request_pass(self, ctx: EngineContext, mock_plextv, monkeypatch):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport
        from shortlist.engine.models import MediaType as MT

        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._suggest_a_missing_title(ctx)
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )

        captured = {}
        sentinel = RequestReport(considered=1)

        def spy(cfg, tmdb, demand, *, dry_run, already_handled=None, **kw):
            captured["demand"] = demand
            captured["dry_run"] = dry_run
            captured["already_handled"] = already_handled
            return sentinel

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)

        report = pipeline_mod.run(ctx, [sarah])

        # The missing title reached the request pass; the in-library one did not.
        assert (30, MT.MOVIE) in captured["demand"]
        assert (10, MT.MOVIE) not in captured["demand"]
        assert captured["demand"][(30, MT.MOVIE)].demand == 1
        # Its provenance names the row per library: a missing MOVIE renders {library_name} as the
        # movie library ("Movies"), so the inbox shows the same name the row is actually called.
        why = captured["demand"][(30, MT.MOVIE)].why
        assert why and why[0].row == "✨ Movies Picked for You"
        assert report.requests is sentinel

    def test_per_row_pool_attributes_tags_to_the_row_that_surfaced_the_title(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport, RowSpec
        from shortlist.engine.models import MediaType as MT

        # Two rows for one user: a default one on tmdb_similar (all in-library, nothing missing) and
        # a "Hidden Gems" row on tmdb_discover that surfaces a MISSING title (id 30). The missing
        # title must carry only the discover row's tag (plus the user's), not the default row's.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5),  # inherits global -> tmdb_similar
            RowSpec(
                slug="gems",
                name_template="Hidden Gems",
                size=5,
                candidate_sources=["tmdb_discover"],
                request_tag="gems",
            ),
        ]
        sarah = make_profile("sarah", account_id=100, request_tag="sarah")
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 30, "title": "Missing Gem", "genre_ids": [], "vote_average": 8.4}
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )
        captured = {}
        monkeypatch.setattr(
            pipeline_mod.requests_mod,
            "request_missing",
            lambda cfg, tmdb, demand, **kw: captured.setdefault("demand", demand) or RequestReport(),
        )

        report = pipeline_mod.run(ctx, [sarah])

        missing = captured["demand"][(30, MT.MOVIE)]
        assert missing.tags == {"sarah", "gems"}  # user tag + the row whose pool surfaced it, not "picked"
        assert missing.demand == 1  # counted once for this user despite multiple rows/pools
        # Distinct-union candidate count spans both pools: {10,20} (similar) and {30} (discover).
        assert report.users[0].counts.candidates == 3


class TestPlacement:
    """Per-row placement (Home / Library / Both) and pin-to-top reach promote() with the right flags."""

    def _pick(self, slug: str):
        from shortlist.engine.models import MediaType, Pick

        return Pick(
            tmdb_id=1, rating_key=10, title="t1", rank=1, reason="", media_type=MediaType.MOVIE, collection_slug=slug
        )

    def test_library_placement_promotes_recommended_only(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="library"))
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

    def test_home_placement_with_pin_shows_on_home_and_pins(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="home", pin_top=True))
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": True,
            "recommended": False,
            "pin_top": True,
        }

    def test_recommended_is_chosen_per_collection_not_ored_across_audiences(self, ctx: EngineContext):
        """The Recommended flag comes from WHOSE row the collection is (issue #6).

        Everyone gets their OWN collection, so Plex's single `promotedToRecommended` is set per
        collection and the owner/friends split is real. The old code OR'd both placements into one
        flag, so "friends: Recommended on" silently dragged the owner's row onto the shelf too —
        and left the owner with no way to un-clutter their own shelf.
        """
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="home", placement_friends="both")

        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": False,
            "pin_top": False,
        }

        _promote_one(ctx, MagicMock(), spec, UserType.SHARED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

    def test_owner_keeps_the_shelf_while_friends_rows_stay_off_it(self, ctx: EngineContext):
        """The inverse split: the owner's row on their Recommended shelf, friends' rows only on
        Friends' Home. This is the config that keeps the owner's shelf to just their own row."""
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="both", placement_friends="home")

        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": True,
            "pin_top": False,
        }

        _promote_one(ctx, MagicMock(), spec, UserType.SHARED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": False,
            "pin_top": False,
        }

    def test_a_managed_user_collection_uses_the_friends_side(self, ctx: EngineContext):
        """A managed user takes the FRIENDS-side flags, never the owner's.

        Plex's docs are explicit: Home (``promotedToOwnHome``) "applies to the server owner", while
        Shared Users' Home (``promotedToSharedHome``) "applies to all shared users, including
        managed users" — https://support.plex.tv/articles/manage-recommendations/. Routing a managed
        user through the owner flag hides their row from them and puts it on the OWNER's Home.

        The owner side is deliberately "off" here, so reading the wrong side is unmissable.
        """
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="off", placement_friends="both")

        _promote_one(ctx, MagicMock(), spec, UserType.MANAGED)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

        # The owner, on the same spec, gets nothing — proving the two sides really are independent.
        _promote_one(ctx, MagicMock(), spec, UserType.OWNER)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": False,
            "pin_top": False,
        }

    def test_an_unmapped_managed_collection_never_lands_on_the_owners_home(self, ctx: EngineContext):
        """The no-spec fallback must respect the same split — it used to hand MANAGED `home=True`,
        which is the owner's shelf."""
        from shortlist.engine.models import UserType
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        _promote_one(ctx, collection, None, UserType.MANAGED)
        ctx.plex.promote.assert_called_with(collection, shared=True, home=False, recommended=False)

    def test_promotion_reaches_a_library_this_run_no_longer_targets(self, ctx: EngineContext):
        """`delivery_sections` is narrowed to libraries some row currently targets, so a row whose
        `library_keys` was narrowed left its old collection stranded in the dropped library — never
        re-promoted (out of scope) and never demoted either, keeping its surfaces indefinitely."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        movies = MagicMock(type="movie", key="1", title="Movies")
        dropped = MagicMock(type="movie", key="2", title="4K Movies")  # no row targets this any more
        ctx.plex.sections.return_value = [movies, dropped]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5, library_keys=["1"])]
        ctx.config.dry_run = False
        stranded = MagicMock(title="Hidden Gems (left behind)")
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stranded] if s is dropped else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once()  # reached despite living outside delivery_sections
        assert ctx.plex.promote.call_args.args[0] is stranded  # promoted the STRANDED collection, not a fallback

    def test_a_stranded_collection_resolves_to_its_row_rather_than_the_fallback(self, ctx: EngineContext):
        """Regression: widening promotion to every library made this WORSE before it made it better.

        The fallback title map used to be rendered only for the libraries a row targets NOW, so a
        collection left in a de-targeted library could be reached but never identified — and took the
        no-spec fallback on EVERY run, turning Friends' Home on for a row switched fully off. The map
        is now rendered across every library of the row's media type.
        """
        from datetime import UTC, datetime

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        movies = MagicMock(type="movie", key="1", title="Movies")
        dropped = MagicMock(type="movie", key="2", title="4K Movies")  # row no longer targets this
        ctx.plex.sections.return_value = [movies, dropped]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [
            RowSpec(
                slug="gems",
                name_template="{library_name} Gems",
                size=5,
                library_keys=["1"],
                placement="off",
                placement_friends="off",
            )
        ]
        ctx.config.dry_run = False
        stranded = MagicMock(title="4K Movies Gems" + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stranded] if s is dropped else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        # Its real spec is off/off, so it claims nothing — NOT the fallback's shared=True.
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": False,
            "pin_top": False,
        }

    def test_the_no_spec_fallback_never_forces_a_row_onto_the_recommended_shelf(self, ctx: EngineContext):
        """A row whose title can't be mapped back to its spec takes the fallback, which used to
        default `recommended=True`.

        That is the one surface where the OWNER sees every row — no share filter can hide it from
        them — so defaulting it on put rows the operator had switched fully off onto the owner's
        shelf. The Home flags stay on by design: this branch is common, not rare, and turning them
        off there made every row in the full-stack suite disappear.
        """
        from shortlist.engine.models import UserType
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        for user_type in (UserType.OWNER, UserType.MANAGED, UserType.SHARED, None):
            _promote_one(ctx, collection, None, user_type)
            assert ctx.plex.promote.call_args.kwargs.get("recommended") is False, user_type

        # And it still never puts someone else's row on the owner's Home.
        for user_type in (UserType.MANAGED, UserType.SHARED):
            _promote_one(ctx, collection, None, user_type)
            assert ctx.plex.promote.call_args.kwargs.get("home") is False, user_type

    def test_off_placement_claims_no_surface(self, ctx: EngineContext):
        """ "off" turns every surface off for that audience. The collection still exists and is still
        browse-hidden by promote()'s unconditional modeUpdate, so it lives in the Collections tab."""
        from shortlist.engine.models import RowSpec, UserType
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, placement="off", placement_friends="off")

        for user_type in (UserType.OWNER, UserType.MANAGED, UserType.SHARED):
            _promote_one(ctx, MagicMock(), spec, user_type)
            assert ctx.plex.promote.call_args.kwargs == {
                "shared": False,
                "home": False,
                "recommended": False,
                "pin_top": False,
            }, user_type

    def test_a_shared_row_unions_both_audiences(self, ctx: EngineContext):
        """A SHARED row is ONE public collection rather than one per person, so there is no "whose
        row is this" to split on — it takes both Home flags and either side's Recommended."""
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        spec = RowSpec(slug="x", name_template="", size=10, shared=True, placement="home", placement_friends="library")

        _promote_one(ctx, MagicMock(), spec)
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": True,
            "recommended": True,
            "pin_top": False,
        }

    def test_an_unmatched_collection_is_hidden_from_browse_and_claims_nothing_else(self, ctx: EngineContext):
        """A collection whose title we can't map to a row must still be browse-hidden — never left
        half-promoted and visible to everyone.

        It claims NO other surface: the fallback used to default `recommended=True`, which forced a
        row onto the Recommended shelf regardless of its placement, so a row the operator had
        switched fully off reappeared there. Under-showing a row for one run is recoverable;
        silently overriding "off" is not.
        """
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        _promote_one(ctx, collection, None)
        ctx.plex.promote.assert_called_once_with(collection, shared=True, recommended=False)

    def test_undelivered_static_library_only_row_keeps_its_placement(self, ctx: EngineContext):
        """INT-3: a STATIC-titled 'Library only' row that exists but got no picks this run keeps its
        library-only placement — it must NOT fall to the everywhere-visible default and pop onto Home
        for that one run (the promote-phase fallback maps it to its spec by its stable title)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library")]
        ctx.config.dry_run = False
        section = MagicMock(type="movie", key="1", title="Movies")
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))  # exists, no picks
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

    def test_undelivered_library_name_row_maps_each_library_to_its_spec(self, ctx: EngineContext):
        """The default {library_name} row renders a DIFFERENT title per library, so an undelivered but
        still-lingering copy must map to its spec in EACH library — not fall to the everywhere-visible
        default in the libraries the fallback didn't render. Both keep the row's library-only placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        tpl = "✨ {library_name} Picked for You"
        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="picked", name_template=tpl, size=10, placement="library")]
        ctx.config.dry_run = False
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.delivery_sections = [movies, shows]
        ctx.plex.sections.return_value = [movies, shows]
        colls = {
            movies: MagicMock(title=render_row_name(tpl, user, [], library_name="Movies") + row_marker(100)),
            shows: MagicMock(title=render_row_name(tpl, user, [], library_name="TV Shows") + row_marker(100)),
        }
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [colls[s]] if s in colls else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_count == 2  # each library's lingering row mapped to its spec
        for call in ctx.plex.promote.call_args_list:
            # placement="library" -> hidden from Home, shown only in the library's Recommended shelf.
            assert call.kwargs == {"shared": False, "home": False, "recommended": True, "pin_top": False}

    def test_an_undelivered_dynamic_titled_row_keeps_the_safe_fallback(self, ctx: EngineContext):
        """A {top_seed} row's title can't be predicted without picks, so an un-delivered one has no
        entry in the title map and takes the no-spec fallback.

        That is the right outcome, not a gap: the fallback browse-hides, gives each audience its own
        Home flag, and — crucially — does NOT claim the Recommended shelf, the one surface the owner
        cannot filter. Resolving it by label instead would mis-map a DISABLED row's leftover
        collection onto whichever row the user still has enabled.
        """
        from datetime import UTC, datetime

        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="dyn", name_template="Because you watched {top_seed}", size=10, placement="library")
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title="Because you watched Dune (from a prior run)")
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(coll, shared=True, home=False, recommended=False)

    def test_an_unmanaged_rows_config_still_resolves_its_default_row(self, ctx: EngineContext):
        """With no rows configured the engine synthesizes a legacy default spec, and every other
        phase builds from it. Promotion used to read the RAW `config.rows` instead, so the title map
        was empty, every lookup missed, and placement was silently ignored for the whole server."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = []
        ctx.config.rows_defined = False  # unmanaged -> default_row_spec() is synthesized
        ctx.config.dry_run = False
        section = MagicMock()
        section.title = "Movies"
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        default_spec = ctx.config.per_person_rows()[0]
        title = render_row_name(
            resolve_row_template(default_spec, user, ctx.config), user, [], library_name="Movies"
        ) + row_marker(user.plex_account_id)
        coll = MagicMock(title=title)
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        # Resolved to the real spec (default placement "both") — NOT the no-spec fallback, which
        # would have withheld the Recommended shelf.
        assert ctx.plex.promote.call_args.kwargs["recommended"] is True

    def test_fallback_skips_a_row_this_user_is_not_in_the_audience_for(self, ctx: EngineContext):
        """Audience is honoured by the no-picks fallback: a per-person row this user is excluded from
        must never be handed a Home/Library placement for them. It stays on the unmapped safe fallback
        (per-person rows share the same marker, so this audience skip is the ONLY thing protecting it)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library", audience={999})
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(
            coll, shared=True, home=False, recommended=False
        )  # excluded → NOT mapped; friend → no home

    def test_fallback_leaves_shared_rows_to_the_shared_promote_loop(self, ctx: EngineContext):
        """A shared row must never be picked up by the PER-PERSON fallback (it promotes in the separate
        shared loop). Even if a collection under this user's label matched the title the fallback would
        compute, the `spec.shared` skip keeps it on the unmapped safe fallback, not the shared spec's
        Home placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="all", name_template="Everyone's Picks", size=10, placement="home", shared=True)
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        ctx.plex.sections.return_value = [section]
        coll = MagicMock(title=render_row_name("Everyone's Picks", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(
            coll, shared=True, home=False, recommended=False
        )  # shared spec skipped → NOT mapped; friend → no home

    def test_a_top_seed_row_records_a_placement_title_per_library(self, ctx: EngineContext, mock_plextv):
        """A {top_seed} row spanning two libraries writes a DIFFERENT title in each (each curated from
        its own contents), so promotion must know both — not just the first. The recorded titles must
        match what the collections are actually delivered as, or every library but the first would fall
        back to the legacy everywhere-visible placement."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        # Two seeds; each library holds candidates from a DIFFERENT seed, so its {top_seed} differs:
        # Movies is fed by Fargo (ids 10-15), 4K by Heat (ids 50-55).
        idx_std = {900: 999, 800: 888, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, 800: 888, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k

        def suggestions(tid, mt):  # returns (item, affinity) pairs
            base = 10 if tid == 900 else 50  # Fargo -> Movies ids, Heat -> 4K ids
            return _ranked(
                [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(6)]
            )

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),  # tmdb 900
            make_watched("Heat", days_ago=2, rating_key=888),  # tmdb 800
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Because you watched {top_seed}", size=5, media="movie")
        ]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        # Capture the titles delivery actually writes so we can compare to what was recorded.
        delivered_titles: list[str] = []
        ctx.plex.create_collection.side_effect = lambda section, title, items: (
            delivered_titles.append(title) or MagicMock()
        )
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        recorded = set(report.users[0].placement_titles)
        # Two libraries with different top seeds -> two distinct titles; the pre-fix code recorded ONE
        # (union) and left the 4K collection unmatched. Every delivered title must be recorded.
        assert len(recorded) == 2, f"expected a distinct title per library, got {recorded}"
        assert set(delivered_titles) == recorded, "recorded titles must match what delivery wrote"
        assert all(slug == "picked" for slug in report.users[0].placement_titles.values())


class _DictCache:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestLibraryScoping:
    """Only the libraries a row targets are read — an unselected/off-type library is never scanned."""

    def test_reads_only_libraries_a_row_targets(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec

        movies = MagicMock(type="movie", key="1", title="Movies")
        sports = MagicMock(type="movie", key="2", title="Sports")  # unselected by the row below
        shows = MagicMock(type="show", key="3", title="TV Shows")  # wrong media for a movie row
        ctx.config.rows = [RowSpec(slug="m", name_template="Movies", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.plex.section_signature.return_value = None  # force a scan (no cache)
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, sports, shows])

        assert scanned == ["1"]  # Movies only — Sports and TV Shows never read
        assert [str(s.key) for s in ctx.delivery_sections] == ["1"]

    def test_unconfigured_run_still_reads_every_library(self, ctx: EngineContext):
        """No rows configured -> the synthesized default row targets everything, so all libraries read."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.config.rows = []
        ctx.config.rows_defined = False
        ctx.plex.section_signature.return_value = None
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, shows])

        assert sorted(scanned) == ["1", "2"]

    def test_muted_row_cleanup_scans_a_library_the_run_scoped_out(self, ctx: EngineContext):
        """A muted row whose stale copy lives in a de-targeted library is still removed — cleanup scans
        EVERY library, not the run's (targeting-scoped) delivery_sections."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import CollectionDiff, RowOverride, RowSpec, UserRunReport
        from shortlist.engine.rows import _remove_muted_and_retired

        movies = MagicMock(type="movie", key="1", title="Movies")
        old_lib = MagicMock(type="movie", key="2", title="4K Movies")  # row no longer targets this
        # sections() deliberately WIDER than delivery_sections: the point is that cleanup reaches a
        # library this run no longer targets.
        ctx.plex.sections.return_value = [movies, old_lib]
        ctx.delivery_sections = [movies]
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.config.dry_run = False
        sarah = make_profile("sarah", account_id=100, row_overrides={"gems": RowOverride(muted=True)})
        stale = MagicMock(title="Hidden Gems" + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stale] if s is old_lib else []

        report = UserRunReport(username="sarah", slug="sarah", diff=CollectionDiff())
        _remove_muted_and_retired(ctx, sarah, ctx.config, report)

        ctx.plex.delete_owned_collection.assert_called_once()  # removed from 4K Movies despite the scope
        # ...and the ledger entry for the collection just deleted is marked for forgetting, or the
        # dead ratingKey would be re-presented on every later run.
        assert report.removed_deliveries == [{"row_slug": "gems", "library_key": "2"}]


class TestLibraryIndexCache:
    """The cross-run tmdb_id -> ratingKey index cache in _library_index."""

    def _ctx(self, cache):
        ctx = MagicMock()
        ctx.index_cache = cache
        ctx.progress = None  # _emit only logs
        ctx.plex.section_signature.return_value = "100:200"
        ctx.plex.build_library_index.return_value = {42: 1}
        return ctx

    def test_unchanged_library_serves_the_cached_index_without_re_scanning(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        first = pipeline_mod._library_index(ctx, section)
        second = pipeline_mod._library_index(ctx, section)
        assert first == second == {42: 1}
        assert ctx.plex.build_library_index.call_count == 1  # second run served from cache

    def test_a_changed_signature_re_scans(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        ctx.plex.section_signature.return_value = "101:200"  # a title was added/removed/edited
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_nullcache_always_scans(self):
        ctx = self._ctx(NullCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_a_missing_signature_disables_the_cache(self):
        ctx = self._ctx(_DictCache())
        ctx.plex.section_signature.return_value = None  # neither totalSize nor updatedAt available
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2


class TestParallelRuns:
    """Stage 3: users processed concurrently, but every Plex write serialized by ctx.write_lock."""

    def _users(self, mock_plextv, names=("sarah", "mike", "canary")):
        users = [make_profile(n, account_id=(i + 1) * 100) for i, n in enumerate(names)]
        mock_plextv.users = [plextv_user((i + 1) * 100, n) for i, n in enumerate(names)]
        return users

    def test_writes_never_run_concurrently_under_the_lock(self, ctx: EngineContext, mock_plextv):
        import threading
        import time

        users = self._users(mock_plextv)
        ctx.concurrency = 3

        created: dict[str, object] = {}

        def stored_label(collection, label):
            created[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.find_owned_collections.side_effect = lambda s, label: (
            [created[label.lower()]] if label.lower() in created else []
        )

        counter = {"now": 0, "max": 0}
        guard = threading.Lock()

        def guarded_create(section, title, items):
            with guard:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            time.sleep(0.02)  # widen the window a race would slip through
            with guard:
                counter["now"] -= 1
            return MagicMock()

        ctx.plex.create_collection.side_effect = guarded_create

        report = pipeline_mod.run(ctx, users)

        assert all(u.status == "ok" for u in report.users)
        assert ctx.plex.create_collection.call_count == 3  # every user delivered
        assert counter["max"] == 1, "deliver writes ran concurrently — the write_lock is not holding"

    def test_concurrency_preserves_user_order_and_excludes(self, ctx: EngineContext, mock_plextv):
        users = self._users(mock_plextv)
        ctx.concurrency = 3
        created: dict[str, object] = {}

        def stored_label(collection, label):
            created[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = lambda section, title, items: MagicMock()
        ctx.plex.find_owned_collections.side_effect = lambda s, label: (
            [created[label.lower()]] if label.lower() in created else []
        )

        report = pipeline_mod.run(ctx, users)

        assert [u.slug for u in report.users] == ["sarah", "mike", "canary"]  # input order preserved
        # Each user's share filter excludes the OTHER two users' rows — same privacy result as serial.
        sarah_filters = next(u for u in mock_plextv.users if u.id == 100).filters
        assert "Shortlist_mike" in sarah_filters["filterMovies"]
        assert "Shortlist_canary" in sarah_filters["filterMovies"]
        assert "Shortlist_sarah" not in sarah_filters["filterMovies"]


class TestEffectiveRowSources:
    """candidate_sources (or the global default) is the single source of truth for every row —
    llm_web included, per-person or shared (a head-to-head showed it adds strong taste matches)."""

    def _spec(self, *, shared: bool, sources=None) -> RowSpec:
        return RowSpec(slug="r", name_template="", size=10, shared=shared, candidate_sources=sources or [])

    def test_llm_web_is_kept_for_a_per_person_row(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=False), ["tmdb_similar", "llm_web", "llm_library"])
        assert set(srcs) == {"tmdb_similar", "llm_web", "llm_library"}

    def test_llm_web_is_kept_for_a_shared_row(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=True), ["tmdb_similar", "llm_web"])
        assert "llm_web" in srcs

    def test_a_rows_own_sources_win_over_the_default(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=False, sources=["tmdb_discover"]), ["tmdb_similar", "llm_web"])
        assert srcs == ("tmdb_discover",)


class TestPerDeliveryTimeoutRetry:
    """A PMS timeout retries JUST the idempotent delivery write, NOT the whole user — so a Plex hiccup
    never re-runs the expensive gather + pick selection (the amplifier that made SFLIX run 3
    catastrophic). A delivery that keeps timing out still fails only that user (rule 6 resume-safety)."""

    def _full_movie_pool(self, ctx: EngineContext) -> None:
        """Five in-library candidates for a size-5 row, so ``build_picks`` fires ONCE per section
        (no short-row padding second call) and its call count cleanly reflects the pick work."""
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ids = [10, 11, 12, 13, 14]
        ctx.tmdb.suggestions.return_value = _ranked(
            [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in ids]
        )
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 1000 + i for i in ids}}

    def test_a_transient_delivery_timeout_retries_only_the_write_not_pick_selection(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)  # no real backoff waits
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._full_movie_pool(ctx)
        built = spy_build_picks(monkeypatch)

        # Inject the timeout at the actual PMS WRITE (create_collection), NOT at our deliver_rows
        # helper — so real deliver_rows (and its idempotent re-read) runs on BOTH attempts.
        create_calls = {"n": 0}

        def flaky_create(section, title, items):
            create_calls["n"] += 1
            if create_calls["n"] == 1:
                raise requests.exceptions.ReadTimeout("busy PMS on the write")
            return MagicMock()

        ctx.plex.create_collection.side_effect = flaky_create

        report = pipeline_mod.run(ctx, [sarah])

        assert create_calls["n"] == 2  # the write was retried once, against real deliver_rows
        # Pick selection ran ONCE — the retry did not re-run the gather+build (the point of the change).
        assert len(built) == 1
        user = next(u for u in report.users if u.slug == "sarah")
        assert user.status != "error"
        # The retry did not double-count the per-library audit breakdown (idempotent report state).
        assert len(user.breakdown) == 1

    def test_a_persistent_delivery_timeout_fails_only_that_user(self, ctx: EngineContext, mock_plextv, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._full_movie_pool(ctx)
        built = spy_build_picks(monkeypatch)
        ctx.plex.create_collection.side_effect = requests.exceptions.ReadTimeout("down")

        report = pipeline_mod.run(ctx, [sarah])

        assert next(u for u in report.users if u.slug == "sarah").status == "error"
        assert len(built) == 1  # pick selection ran once, was not re-run on the failures
        ctx.plex.promote.assert_not_called()  # nothing delivered -> nothing promoted


class TestCollectionOrderPhase:
    """The deferred, post-promote item-ordering pass: best-effort, never fatal to an already-delivered run."""

    def test_orders_every_collection_with_its_keys_and_survives_a_failure(self, ctx: EngineContext):
        from unittest.mock import MagicMock as MM
        from unittest.mock import call

        from shortlist.engine.pipeline import _collection_order_phase

        c1, c2, c3 = MM(ratingKey=1), MM(ratingKey=2), MM(ratingKey=3)
        # Middle collection's ordering blows up (slow PMS) — the pass must keep going, not raise.
        ctx.plex.order_collection.side_effect = [4, RuntimeError("PMS timed out"), 2]
        _collection_order_phase(ctx, [(c1, [1, 2]), (c2, [3, 4]), (c3, [5, 6])])
        # Each collection ordered with ITS OWN ranked keys, in order (asserts the unpack, not just count).
        ctx.plex.order_collection.assert_has_calls([call(c1, [1, 2]), call(c2, [3, 4]), call(c3, [5, 6])])

    def test_duplicate_collection_from_a_retry_is_ordered_once(self, ctx: EngineContext):
        from unittest.mock import MagicMock as MM

        from shortlist.engine.pipeline import _collection_order_phase

        coll = MM(ratingKey=7)  # a retried user appended the same collection twice
        _collection_order_phase(ctx, [(coll, [1, 2]), (coll, [1, 2])])
        assert ctx.plex.order_collection.call_count == 1  # de-duped by ratingKey

    def test_dry_run_orders_nothing(self, ctx: EngineContext):
        from dataclasses import replace as dc_replace
        from unittest.mock import MagicMock as MM

        from shortlist.engine.pipeline import _collection_order_phase

        ctx.config = dc_replace(ctx.config, dry_run=True)
        _collection_order_phase(ctx, [(MM(ratingKey=1), [1, 2])])
        ctx.plex.order_collection.assert_not_called()

    def test_no_order_work_is_a_noop(self, ctx: EngineContext):
        from shortlist.engine.pipeline import _collection_order_phase

        _collection_order_phase(ctx, [])
        ctx.plex.order_collection.assert_not_called()

    def test_shelf_ordering_off_skips_all_reordering(self, ctx: EngineContext):
        """The agregarr/Kometa coexistence toggle: with manage_shelf_order=False the order phase must
        never touch the Recommended shelf, even when anchors are configured."""
        from types import SimpleNamespace

        from shortlist.engine.models import HubAnchor
        from shortlist.engine.pipeline import _order_phase

        ctx.config.hub_anchors = {"1": HubAnchor(anchor_title="Recently Added Movies", before=False)}
        ctx.config.manage_shelf_order = False
        report = SimpleNamespace(hub_orderings=[])

        _order_phase(ctx, report)

        ctx.plex.order_owned_hubs.assert_not_called()
        assert report.hub_orderings == []


class TestConverge:
    """The converge phase: rows the promote phase never reaches must still come off the owner's Home.

    Promotion is write-only and only visits users in tonight's run, so anyone paused, disabled,
    deselected, errored or promoted by an older build keeps stale flags for ever. This is the pass
    that catches them — and `promotedToOwnHome` is the one surface no share filter can hide.
    """

    def _collection(self, rating_key: int, label: str, *, on_owner_home: bool = True):
        collection = MagicMock()
        collection.ratingKey = rating_key
        collection.title = f"row-{rating_key}"
        collection.labels = [MagicMock(tag=label)]
        hub = collection.visibility.return_value
        hub.promotedToOwnHome = on_owner_home
        hub.promotedToRecommended = True
        hub.promotedToSharedHome = True
        return collection

    def _run(
        self,
        ctx: EngineContext,
        collections: list,
        promoted: set[int],
        owner_slug: str = "steve",
        paused: set[str] | None = None,
    ):
        from shortlist.engine.models import RunReport
        from shortlist.engine.pipeline import _converge_phase

        ctx.owner_slug = owner_slug
        ctx.paused_slugs = paused or set()
        ctx.plex.sections.return_value[0].collections.return_value = collections
        ctx.plex.demote_all.side_effect = lambda c, **kw: PlexClient.demote_all(ctx.plex, c, **kw)
        ctx.plex.claims_any_surface.side_effect = lambda c: PlexClient.claims_any_surface(ctx.plex, c)
        # Exercise the REAL demote, so the test covers the read-then-write contract, not a stub.
        ctx.plex.demote_own_home.side_effect = lambda c: PlexClient.demote_own_home(ctx.plex, c)
        ctx.plex.reads_as_on_owner_home.side_effect = lambda c: PlexClient.reads_as_on_owner_home(ctx.plex, c)
        report = RunReport(started_at=datetime.now(UTC))
        _converge_phase(ctx, promoted, report)
        return report

    def test_a_stranded_row_is_taken_off_the_owners_home(self, ctx: EngineContext):
        """The SFLIX case: a shared user's row left on the owner's Home by an older build, whose
        owner is not in tonight's run so promote never revisits it."""
        stranded = self._collection(1, "Shortlist_gemnath")
        report = self._run(ctx, [stranded], promoted=set())

        stranded.visibility.return_value.updateVisibility.assert_called_once_with(
            recommended=True, home=False, shared=True
        )
        assert report.converged == ["Shortlist_gemnath"]

    def test_the_owners_own_row_is_left_alone(self, ctx: EngineContext):
        """The owner's row belongs on the owner's Home — converge must not strip it."""
        owned = self._collection(1, "Shortlist_steve")
        report = self._run(ctx, [owned], promoted=set())

        owned.visibility.return_value.updateVisibility.assert_not_called()
        assert report.converged == []

    def test_a_row_promoted_this_run_is_skipped(self, ctx: EngineContext):
        """Promote already set this one correctly; re-reading it would be pure churn."""
        fresh = self._collection(7, "Shortlist_sarah")
        report = self._run(ctx, [fresh], promoted={7})

        fresh.visibility.assert_not_called()
        assert report.converged == []

    def test_a_foreign_collection_is_never_touched(self, ctx: EngineContext):
        """Kometa and friends share these libraries — rule 4."""
        foreign = self._collection(1, "Kometa_Marvel")
        self._run(ctx, [foreign], promoted=set())

        foreign.visibility.return_value.updateVisibility.assert_not_called()

    def test_an_already_correct_row_is_not_rewritten(self, ctx: EngineContext):
        """Idempotence: a nightly converge over hundreds of rows must cost reads, not writes."""
        settled = self._collection(1, "Shortlist_sarah", on_owner_home=False)
        report = self._run(ctx, [settled], promoted=set())

        settled.visibility.return_value.updateVisibility.assert_not_called()
        assert report.converged == []

    def test_nothing_happens_when_the_owner_is_unknown(self, ctx: EngineContext):
        """Without an owner slug every label looks foreign, including the owner's own row. Guessing
        would strip the owner's row off their own Home, so converge must decline instead."""
        anything = self._collection(1, "Shortlist_sarah")
        report = self._run(ctx, [anything], promoted=set(), owner_slug="")

        anything.visibility.assert_not_called()
        assert report.converged == []

    def test_dry_run_writes_nothing_but_still_reports_the_real_list(self, ctx: EngineContext):
        """The preview an operator reads before authorising the live pass must be the ACTUAL list.

        Reporting nothing (or every candidate considered) makes the preview useless: a dry-run
        sync check would answer "corrected 0" forever, whatever the server actually holds.
        """
        ctx.config.dry_run = True
        stranded = self._collection(1, "Shortlist_gemnath")
        settled = self._collection(2, "Shortlist_sarah", on_owner_home=False)

        report = self._run(ctx, [stranded, settled], promoted=set())

        stranded.visibility.return_value.updateVisibility.assert_not_called()
        settled.visibility.return_value.updateVisibility.assert_not_called()
        assert report.converged == ["Shortlist_gemnath"]  # only the one actually stranded

    def test_a_shared_row_is_left_on_the_owners_home(self, ctx: EngineContext):
        """A SHARED row is ONE public collection labelled `shortlist__shared_<row>`, and it belongs on
        the owner's Home whenever its placement asks for it. Matching only the owner's own label
        demoted every shared row on every pass that did not rebuild it — a no-user run, a scoped cron
        run, a cancelled run, a sync check. That is most passes.
        """
        from shortlist.engine.models import RowSpec

        ctx.config.rows = [RowSpec(slug="trending", name_template="Trending", size=10, shared=True, placement="both")]
        shared = self._collection(1, "Shortlist__shared_trending")

        report = self._run(ctx, [shared], promoted=set())

        shared.visibility.return_value.updateVisibility.assert_not_called()
        assert report.converged == []

    def test_a_shared_row_that_does_not_want_home_is_still_converged(self, ctx: EngineContext):
        """The allowance is per-row, not "any shared label" — a shared row set to Library-only has no
        business on the owner's Home either."""
        from shortlist.engine.models import RowSpec

        ctx.config.rows = [
            RowSpec(slug="trending", name_template="Trending", size=10, shared=True, placement="library")
        ]
        shared = self._collection(1, "Shortlist__shared_trending")

        report = self._run(ctx, [shared], promoted=set())

        assert report.converged == ["Shortlist__shared_trending"]

    def test_a_paused_users_row_comes_off_every_surface(self, ctx: EngineContext):
        """Pause means "stop showing it". A paused person is absent from every run by definition, so
        converge is the only pass that can act on them — without this their row stays up for ever."""
        paused = self._collection(1, "Shortlist_sarah")
        report = self._run(ctx, [paused], promoted=set(), paused={"sarah"})

        paused.visibility.return_value.updateVisibility.assert_called_once_with(
            recommended=False, home=False, shared=False
        )
        assert report.converged == ["Shortlist_sarah"]

    def test_an_active_users_row_is_not_stripped_by_the_pause_path(self, ctx: EngineContext):
        """Only the paused person's own label. Stripping an active user's row off every surface would
        make their row vanish for no reason."""
        active = self._collection(1, "Shortlist_mike")
        self._run(ctx, [active], promoted=set(), paused={"sarah"})

        # Not the all-surfaces call — at most the own-home demote, since it is not the owner's label.
        assert active.visibility.return_value.updateVisibility.call_args.kwargs != {
            "recommended": False,
            "home": False,
            "shared": False,
        }

    def test_a_paused_row_already_hidden_is_not_rewritten(self, ctx: EngineContext):
        """Idempotence: converge runs every night over every collection."""
        settled = self._collection(1, "Shortlist_sarah", on_owner_home=False)
        settled.visibility.return_value.promotedToRecommended = False
        settled.visibility.return_value.promotedToSharedHome = False
        report = self._run(ctx, [settled], promoted=set(), paused={"sarah"})

        settled.visibility.return_value.updateVisibility.assert_not_called()
        assert report.converged == []

    def test_a_switched_off_shared_row_is_retired(self, ctx: EngineContext):
        """`retired_rows` only covers PER-PERSON rows (rows.py filters `not s.shared`), so switching a
        shared row off left its collection claiming Friends' Home and the Recommended shelf for ever.
        Non-owners stop seeing it — their filter excludes any label the config no longer declares
        shared — but the OWNER has no filter, so it sat on their server unchanged."""
        from shortlist.engine.models import RowSpec

        ctx.config.rows = [RowSpec(slug="live", name_template="Live", size=10, shared=True, placement="both")]
        gone = self._collection(1, "Shortlist__shared_retired")

        report = self._run(ctx, [gone], promoted=set())

        gone.visibility.return_value.updateVisibility.assert_called_once_with(
            recommended=False, home=False, shared=False
        )
        assert report.converged == ["Shortlist__shared_retired"]

    def test_a_dry_run_does_not_offer_to_fix_a_paused_row_already_down(self, ctx: EngineContext):
        """Caught on the live server: the preview said 2 and the live pass corrected 0, because the
        paused branch reported every candidate without reading whether it claimed anything. The Tools
        button then offered to "fix" rows that were already hidden."""
        ctx.config.dry_run = True
        settled = self._collection(1, "Shortlist_sarah", on_owner_home=False)
        settled.visibility.return_value.promotedToRecommended = False
        settled.visibility.return_value.promotedToSharedHome = False

        report = self._run(ctx, [settled], promoted=set(), paused={"sarah"})

        assert report.converged == []

    def test_a_pms_failure_never_fails_the_run(self, ctx: EngineContext):
        """Converge runs after the real work and only ever removes visibility — a wobble here must
        not sink a run that already delivered everyone's rows. Next run retries."""
        exploding = self._collection(1, "Shortlist_gemnath")
        exploding.visibility.side_effect = RuntimeError("PMS timeout")

        report = self._run(ctx, [exploding], promoted=set())  # must not raise
        assert report.converged == []


class TestOrphanDeletion:
    """Converge may DELETE a collection whose user Shortlist no longer knows — the one irreversible
    action it takes, so it is gated on having a complete picture.

    Demoting an orphan leaves it in the Collections tab; deleting is what clears it from there.

    Neither clears the `label!=` exclude — `privacy.prune` removes only shared labels and a person's
    own label from their own filter, so a private-row exclude survives either way. This docstring used
    to claim deleting was the only way to clear the filters, which was the stated justification for
    choosing the irreversible option.
    """

    def _collection(self, rating_key: int, label: str):
        collection = MagicMock()
        collection.ratingKey = rating_key
        collection.title = f"row-{rating_key}"
        collection.labels = [MagicMock(tag=label)]
        hub = collection.visibility.return_value
        hub.promotedToOwnHome = True
        hub.promotedToRecommended = True
        hub.promotedToSharedHome = True
        return collection

    def _run(self, ctx, collections, *, known: dict, may_delete: bool, dry_run: bool = False):
        from shortlist.engine.models import RunReport
        from shortlist.engine.pipeline import _converge_phase

        ctx.owner_slug = "steve"
        ctx.known_slugs = known
        ctx.may_delete_orphans = may_delete
        ctx.config.dry_run = dry_run
        ctx.plex.sections.return_value[0].collections.return_value = collections
        ctx.plex.claims_any_surface.return_value = True
        ctx.plex.demote_all.return_value = True
        report = RunReport(started_at=datetime.now(UTC))
        _converge_phase(ctx, set(), report)
        return report

    def test_a_user_less_run_never_deletes_however_complete_the_picture(self, ctx: EngineContext):
        """`engine_run(ctx, [])` is the privacy-sync shape, and it fires from routine mutations —
        disabling one person, narrowing a shared row's audience. It documents itself as creating and
        deleting nothing, but it inherited delete authority from the CONTEXT and quietly had it: the
        audit row said "share filters merged" while a collection was destroyed.
        """
        from shortlist.engine.models import RunReport
        from shortlist.engine.pipeline import _converge_phase

        orphan = self._collection(1, "Shortlist_ghost")
        ctx.owner_slug = "steve"
        ctx.known_slugs = {100: "steve", 200: "sarah"}
        ctx.may_delete_orphans = True  # the picture IS complete — that is not the question
        ctx.config.dry_run = False
        ctx.plex.sections.return_value[0].collections.return_value = [orphan]
        ctx.plex.claims_any_surface.return_value = True
        ctx.plex.demote_all.return_value = True

        report = RunReport(started_at=datetime.now(UTC))
        _converge_phase(ctx, set(), report, may_delete=False)

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphans_removed == [], "a pass with no users must not destroy anyone's row"
        # Still demoted — monotonically private, which is what such a pass IS for.
        assert report.converged == ["Shortlist_ghost"]

    def test_a_collection_whose_user_is_gone_is_deleted(self, ctx: EngineContext):
        orphan = self._collection(1, "Shortlist_ghost")
        report = self._run(ctx, [orphan], known={100: "steve", 200: "sarah"}, may_delete=True)

        ctx.plex.delete_owned_collection.assert_called_once()
        assert report.orphans_removed == ["Shortlist_ghost"]

    def test_a_known_users_collection_is_never_deleted(self, ctx: EngineContext):
        live = self._collection(1, "Shortlist_sarah")
        report = self._run(ctx, [live], known={100: "steve", 200: "sarah"}, may_delete=True)

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphans_removed == []

    def test_an_incomplete_picture_demotes_instead_of_deleting(self, ctx: EngineContext):
        """ "I could not read the users" and "this user does not exist" look identical from here.
        Deleting on the first would wipe live rows, so it only ever hides."""
        orphan = self._collection(1, "Shortlist_ghost")
        report = self._run(ctx, [orphan], known={100: "steve"}, may_delete=False)

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphans_removed == []
        assert report.converged == ["Shortlist_ghost"]

    def test_an_empty_roster_never_deletes_anything(self, ctx: EngineContext):
        """An empty `known_slugs` means the picture is missing, not that everyone left."""
        orphan = self._collection(1, "Shortlist_ghost")
        report = self._run(ctx, [orphan], known={}, may_delete=True)

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphans_removed == []

    def test_dry_run_reports_the_deletion_without_making_it(self, ctx: EngineContext):
        orphan = self._collection(1, "Shortlist_ghost")
        report = self._run(ctx, [orphan], known={100: "steve"}, may_delete=True, dry_run=True)

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphans_removed == ["Shortlist_ghost"]


class TestRatingSource:
    """Ordering by "Highest rated" when the owner picked a non-TMDB service (IMDb, Trakt, …).

    The score comes from MDBList, which the request gate already uses. Only rows actually ordered by
    rating pay for it, and only for the picks that survived into the row.
    """

    def _rating_ctx(self, ctx, source: str, mdblist):
        from tests.unit.test_pipeline import TestPerRowOverrides as T

        T()._ordered_row_ctx(ctx, "rating")
        ctx.config.rating_source = source
        ctx.mdblist = mdblist

    def _ids(self, report):
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        return [p["tmdb_id"] for p in picks]

    def test_a_rating_row_sorts_on_the_configured_service_not_tmdb(self, ctx: EngineContext, mock_plextv):
        """The whole point: TMDB rates 10 highest and 19 lowest in this fixture, so an IMDb order that
        reverses them cannot be TMDB's numbers by coincidence."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)})  # IMDb: 19 best, 10 worst
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, reverse=True), f"sorted by the IMDb score, got {ids}"
        assert mdblist.calls == len(ids), f"one lookup per delivered pick, not per candidate, got {mdblist.calls}"

    def test_the_default_source_costs_no_lookups_at_all(self, ctx: EngineContext, mock_plextv):
        """TMDB is already on every candidate, so the default must not touch MDBList — otherwise every
        rating-ordered row on the server would spend quota for a number it already had."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({})
        self._rating_ctx(ctx, "tmdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert mdblist.calls == 0, "the TMDB default is a no-op"
        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"still ordered by TMDB score, got {ids}"

    def test_a_spent_quota_falls_the_whole_row_back_to_tmdb(self, ctx: EngineContext, mock_plextv):
        """A row must never be sorted on two services' scales at once. Falling back only for the
        titles AFTER the 429 would interleave IMDb scores with TMDB ones, which is worse than either.
        """
        from tests.unit.test_requests import FakeMdbList

        # Reversed vs TMDB, so a partial application would be obvious in the delivered order.
        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)}, rate_limit_after=2)
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"whole row on TMDB scores, got {ids}"

    def test_a_title_the_service_cannot_score_sorts_last(self, ctx: EngineContext, mock_plextv):
        """An unrated title is not an error — IMDb simply has no score for some titles. It goes to the
        end of the row rather than dropping out of it or failing the run."""
        from tests.unit.test_requests import FakeMdbList

        ratings: dict[int, tuple[float, int] | None] = {tid: (float(tid), 5000) for tid in range(10, 20)}
        # 12 is one of the five titles that actually reach this row (selection takes the top 5 by
        # ranking), and would otherwise sit mid-row on the IMDb scale — so "sorts last" is a real move.
        ratings[12] = None
        mdblist = FakeMdbList(ratings)
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids[-1] == 12, f"the unscored title sorts last, got {ids}"
        assert sorted(ids) == [10, 11, 12, 13, 14], f"and is still delivered, not dropped, got {ids}"

    def test_no_mdblist_key_configured_leaves_the_row_on_tmdb(self, ctx: EngineContext, mock_plextv):
        """`ctx.mdblist` is None when no key is set. Choosing IMDb without one must degrade to TMDB,
        which is what the setting documents — not raise on every rating-ordered row."""
        self._rating_ctx(ctx, "imdb", None)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"fell back to TMDB, got {ids}"

    def test_the_service_score_never_overwrites_the_persisted_tmdb_rating(self):
        """`Pick.rating` is TMDB's, is persisted as such, and comes back on every carried-forward pick.

        The service score is returned as a separate override map instead of being written onto the
        pick. Writing it made a fallback impossible to honour: a refresh night mixes carried picks
        (holding last run's MDBList score) with newcomers (holding TMDB's), and once both sit in the
        same field nothing can tell them apart — so a quota-spent night sorted one row on two
        services' scales, which is worse than sorting it on either.
        """
        from shortlist.engine.rows import _apply_order, _rated_by_source
        from tests.unit.test_requests import FakeMdbList

        picks = [
            Pick(tmdb_id=1, rating_key=1, title="A", rank=1, reason="", media_type=MediaType.MOVIE, rating=9.0),
            Pick(tmdb_id=2, rating_key=2, title="B", rank=2, reason="", media_type=MediaType.MOVIE, rating=1.0),
        ]
        ctx = MagicMock()
        ctx.config.rating_source = "imdb"
        ctx.mdblist_rate_limited = False
        ctx.mdblist = FakeMdbList({1: (2.0, 500), 2: (8.0, 500)})  # IMDb reverses TMDB's order

        overrides = _rated_by_source(picks, ctx)
        ordered = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5, ratings=overrides)

        assert [p.rating for p in picks] == [9.0, 1.0], "the picks themselves still carry TMDB's score"
        assert [p.tmdb_id for p in ordered] == [2, 1], "but the row is ordered on the IMDb score"
        # And with no map (the quota-spent / no-key fallback) the SAME picks order on TMDB alone.
        fallback = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5, ratings=None)
        assert [p.tmdb_id for p in fallback] == [1, 2], "the fallback is one consistent TMDB scale"

    def test_a_spent_quota_stops_being_retried_for_the_rest_of_the_run(self, ctx: EngineContext, mock_plextv):
        """Without a latch, every rating-ordered row for every user re-attempts after the first 429 —
        and each attempt is retried three times honouring Retry-After (up to 60s). On a 40-user server
        that is minutes of stall for results that are thrown away."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)}, rate_limit_after=0)
        self._rating_ctx(ctx, "imdb", mdblist)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        assert ctx.mdblist_rate_limited, "the run latched the spent quota"
        assert mdblist.calls == 1, f"one failed call for the whole run, not one per row per user, got {mdblist.calls}"


class TestSeedCycling:
    """`RowPolicy`'s side of seed cycling: what forces a nightly cadence, and what may share a
    derivation. The rotation itself is covered in test_history.py."""

    def _policy(self, ctx: EngineContext, user) -> object:
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver

        return RowPolicy(
            ctx=ctx,
            user=user,
            cfg=ctx.config,
            specs=[],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )

    @pytest.mark.parametrize(
        ("name_template", "seed_window", "stored", "expected", "why"),
        [
            ("Because you watched {top_seed}", 1, 0.0, 1.0, "a named row overrides even a frozen value"),
            ("Because you watched {top_seed}", 1, None, 1.0, "and the inherited global"),
            # The arm the row editor originally failed to mirror: an UNNAMED row that cycles is run
            # nightly too, so a freshness control on it would state a cadence the row never uses.
            ("Tonight's pick", 3, 0.0, 1.0, "a cycling row is nightly whether or not it names a seed"),
            ("Tonight's pick", 1, 0.0, 0.0, "a row that follows no watch still freezes at 0"),
            ("Tonight's pick", 1, None, 0.5, "and still inherits the global otherwise"),
        ],
    )
    def test_only_a_row_following_a_watch_is_forced_nightly(
        self, ctx: EngineContext, name_template, seed_window, stored, expected, why
    ):
        ctx.config = replace(ctx.config, freshness=0.5)
        policy = self._policy(ctx, make_profile("sarah", account_id=100))
        spec = RowSpec(slug="r", name_template=name_template, size=5, seed_window=seed_window, freshness=stored)

        assert policy.effective_freshness(spec) == expected, why

    def test_the_DEFAULT_row_is_forced_nightly_from_the_global_template(self, ctx: EngineContext):
        """The row-identity cell the matrix above cannot reach, and the one that matters most.

        `context_builder` blanks the default row's `name_template` on purpose — its title comes from
        the global `row.name_template`, which is what the wizard and Settings edit. Asking the SPEC
        whether it names a seed therefore answered "no" for the one row every new install starts
        with, and the wizard offers "Because you watched {top_seed}" for exactly that row: it was
        neither forced nightly nor rebuilt when its seed moved, while the editor hid the freshness
        control and promised "every night".
        """
        ctx.config = replace(ctx.config, freshness=0.5, row_name_template="Because you watched {top_seed}")
        policy = self._policy(ctx, make_profile("sarah", account_id=100))
        default_row = RowSpec(slug="picked", name_template="", size=5)

        assert policy.effective_freshness(default_row) == 1.0

    def test_a_per_user_template_that_names_a_seed_also_forces_nightly(self, ctx: EngineContext):
        """Same precedence, middle rung: `resolve_row_template` is row -> user -> global, so a
        per-user override naming a seed has to count as much as the row's own template."""
        ctx.config = replace(ctx.config, freshness=0.5, row_name_template="Picked for You")
        user = make_profile("sarah", account_id=100)
        user.row_name_template = "Because you watched {top_seed}"
        policy = self._policy(ctx, user)

        assert policy.effective_freshness(RowSpec(slug="picked", name_template="", size=5)) == 1.0

    def test_two_cycling_rows_do_not_share_one_derivation(self, ctx: EngineContext):
        """The seed cache keys on (media, libraries, max_seeds) — which two cycling rows can match on
        exactly. Without the row's own offset in the key they share one entry and land on the SAME
        watch, which is the opposite of what turning cycling on asks for."""
        user = make_profile("sarah", account_id=100)
        user.history = [make_watched(f"Movie {i}", days_ago=i, tmdb_id=900 + i * 7) for i in range(5)]
        ctx.run_day = 5
        policy = self._policy(ctx, user)
        # Identical in every keyed dimension except the slug the offset is derived from.
        common = {"name_template": "", "size": 5, "media": "movie", "max_seeds": 1, "seed_window": 3}
        leads = {slug: policy.seeds_for(RowSpec(slug=slug, **common))[0].title for slug in ("alpha", "beta", "gamma")}

        assert len(set(leads.values())) > 1, f"every cycling row picked the same watch: {leads}"

    def test_the_cycle_offset_survives_a_restart(self):
        """A per-process-salted `hash` would re-phase every restart, so a row would re-pick its seed
        and rebuild on every run — the same reason `_is_refresh_night` uses crc32."""
        import zlib

        from shortlist.engine.rows import seed_cycle_offset

        assert seed_cycle_offset("picked", "sarah", 5) == 5 + zlib.crc32(b"picked|sarah")
        # And two people's rows sit at different points in the cycle, so a server does not re-derive
        # every cycling row on the same night.
        assert seed_cycle_offset("picked", "sarah", 5) != seed_cycle_offset("picked", "mike", 5)
