"""Pipeline orchestration: per-user isolation, cold start, dry-run, parallel runs, cancel and cost accounting."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import rows as rows_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    row_marker,
)
from shortlist.engine.models import (
    MediaType,
    OwnedRow,
    Pick,
    RowOverride,
    RowSpec,
    UserRunReport,
    UserType,
)
from tests.conftest import NOW, fake_media_item, make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import (
    _ranked,
    ctx,  # noqa: F401
    spy_build_picks,
)


def _run_two_row_user(ctx: EngineContext, mock_plextv) -> UserRunReport:
    """Two per-person rows sharing one pool (same media/sources/seeds), with enough watch history
    to skip a cold start. Runs the real pipeline — which calls `_run_user` per person — and returns
    this user's report."""
    ctx.config.rows = [
        RowSpec(slug="picked-for-you", name_template="Picked for You", size=5),
        RowSpec(slug="because-you-watched", name_template="Because You Watched", size=5),
    ]

    def slow_fetch(*_args, **_kwargs) -> list:
        # Keeps setup_s deterministically non-zero — round(x, 3) in _run_user collapses a sub-ms
        # span to exactly 0.0, which would make `report.setup_s > 0` fail by rounding accident.
        time.sleep(0.01)
        return [make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)]

    ctx.history_source.fetch.side_effect = slow_fetch
    mock_plextv.users = [plextv_user(100, "sarah")]

    report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
    return report.users[0]


def _run_cold_user(ctx: EngineContext, mock_plextv) -> UserRunReport:
    """Fewer watches than `min_history` — the cold-start path, which never builds a candidate pool."""
    ctx.plex.top_rated.side_effect = lambda section, n: [
        (100 + i, MagicMock(ratingKey=9000 + i, title=f"Top{i}")) for i in range(n)
    ]
    ctx.config.rows = [RowSpec(slug="picked-for-you", name_template="Picked for You", size=5)]
    ctx.history_source.fetch.return_value = [make_watched("Solo Watch", days_ago=1, rating_key=999)]
    mock_plextv.users = [plextv_user(100, "sarah")]

    report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
    return report.users[0]


class TestRun:
    def test_happy_path_delivers_syncs_then_promotes(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

        # A row does not exist until created (delivery takes the create path); capture each created
        # collection by the label it is stored under, so promotion — which enumerates a user's rows
        # by label — finds it.
        created_by_label: dict[str, MagicMock] = {}

        def stored_label(collection, label, *, extra=None):
            created_by_label[label.lower()] = collection
            if extra is not None:
                # Recorded too: the constant label goes on in the SAME write now, so a fake that
                # dropped it would show fewer labelled rows than the real client produces.
                created_by_label[extra.lower()] = collection
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

    def test_a_readback_holding_our_excludes_where_plex_ignores_them_blocks_promotion(
        self, ctx: EngineContext, mock_plextv
    ):
        """Present is not enforced (#116). plex.tv hands back whatever it stored, so a write that lands
        as `X|label!=...` passes a presence check while Plex shows the account every row. The read-back
        must ask whether Plex APPLIES the exclude, or it waves the leak through to promotion."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Both already have rows, so no early merge runs: the roster is read for the sync, then its read-back.
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[1]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[2]),
        }

        def put_behind_an_or(account_id, fields):
            user = next(u for u in mock_plextv.users if u.id == account_id)
            user.filters.update({name: f"contentRating!=R|{value}" for name, value in fields.items()})

        mock_plextv.update_user_filters.side_effect = put_behind_an_or
        existing = MagicMock()
        existing.title = "✨ Picked for You"
        existing.items.return_value = []
        ctx.plex.find_owned_collections.return_value = [existing]

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        assert "read-back missing" in (report.error or "") or any(
            "read-back missing" in (u.error or "") for u in report.users
        )
        ctx.plex.promote.assert_not_called()

    def test_an_account_whose_filter_plex_cannot_read_is_reported_and_does_not_block_everyone(
        self, ctx: EngineContext, mock_plextv
    ):
        """Owner decision 2026-09-13: report it, don't block the server. A literal `&` in one account's label
        makes Plex fail that account's Home outright (measured), so no hide rule written into it can be
        verified. Blocking promotion would take every other person's rows off Home, nightly, for one label
        name — the #14 shape. That account is named instead, with the label to rename."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "label=Kids & Family"}),
            plextv_user(200, "mike"),
        ]
        existing = MagicMock()
        existing.title = "✨ Picked for You"
        existing.items.return_value = []
        ctx.plex.find_owned_collections.return_value = [existing]
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[1]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[2]),
        }

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert list(report.unreadable_filters) == ["sarah"]
        assert "Kids & Family" in report.unreadable_filters["sarah"]
        assert not report.promotion_blockers
        ctx.plex.promote.assert_called()
        # Refused PER FIELD (review 2026-09-13): the unreadable Movies filter is left alone, but Sarah's TV
        # filter can be enforced and is — a bad label in one field must not leave the other unhidden.
        sarah_writes = [call.args[1] for call in mock_plextv.update_user_filters.call_args_list if call.args[0] == 100]
        assert sarah_writes == [{"filterTelevision": "label!=Shortlist_mike"}]
        assert any(call.args[0] == 200 for call in mock_plextv.update_user_filters.call_args_list)

    def test_a_dry_run_restores_nothing_it_did_not_write(self, ctx: EngineContext, mock_plextv):
        """Safe mode computes the repair diff but writes nothing, so claiming a restriction "applies again"
        would repeat a false fact every night (architecture review 2026-09-13)."""
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R|label!=Shortlist_mike"}),
            plextv_user(200, "mike"),
        ]
        ctx.plex.owned_collections.return_value = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[2])}

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.filter_writes, "the dry run still previews the repair"
        assert report.restrictions_restored == {}

    def test_a_left_alone_account_whose_restriction_comes_back_is_reported(self, ctx: EngineContext, mock_plextv):
        user = make_profile("kid", account_id=500)
        remote = plextv_user(500, "kid", filters={"filterMovies": "contentRating!=R|label!=Shortlist__shared_x"})
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))

        pipeline_mod._leave_sharing_alone(ctx, user, remote, report)

        assert report.restrictions_restored == {500: "kid"}

    def test_the_enforcement_spot_check_skips_an_account_this_run_just_wrote(self, ctx: EngineContext):
        """Plex takes ~25s to apply a filter change (pms_share_filter_boolean_semantics.json). Reading an
        account's Home straight after writing its filter measures the OLD filter — on the first run after
        upgrade that is the #116 repair itself, and it would raise an undismissable "Plex is ignoring the
        privacy filter" alert beside the notice saying the repair worked. Written accounts are not sampled;
        with no other account of that kind, the run does not claim to have measured."""
        sarah = make_profile("sarah", account_id=100)
        remote = plextv_user(100, "sarah", filters={"filterMovies": "label!=Shortlist_mike"})
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))
        report.filter_writes[100] = {"username": "sarah", "fields": {}}
        ctx.token_for_user = MagicMock(return_value="server-100")
        owned = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[2])}

        pipeline_mod._verify_filters_enforced(ctx, [sarah], {100: remote}, owned, True, report)

        ctx.token_for_user.assert_not_called()
        assert report.filters_enforcement_measured is False

    def test_a_type_skipped_because_it_was_written_is_not_claimed_as_measured(self, ctx: EngineContext):
        """Round-6 audit: skipping a just-written managed account while a shared one was read published an
        empty finding for BOTH types, clearing a live managed-type alert. A type that produced no reading
        must not ride on another type's."""
        sarah = make_profile("sarah", account_id=100)
        dan = make_profile("dan", user_type=UserType.MANAGED, account_id=300)
        roster = {
            100: plextv_user(100, "sarah", filters={"filterMovies": "label!=Shortlist_mike"}),
            300: plextv_user(300, "dan", filters={"filterMovies": "label!=Shortlist_mike"}),
        }
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))
        report.filter_writes[300] = {"username": "dan", "fields": {}}
        ctx.token_for_user = MagicMock(return_value="server-100")
        ctx.plex.user_hubs.return_value = []
        owned = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[2])}

        pipeline_mod._verify_filters_enforced(ctx, [sarah, dan], roster, owned, True, report)

        assert report.filters_enforcement_measured is False

    def test_a_repair_plex_tv_did_not_keep_is_not_reported_as_restored(self, ctx: EngineContext, mock_plextv):
        """Round-7 audit: the restore is recorded when plex.tv ACCEPTS the write, and jobs audit it before
        they raise — so a write that answered 200 but never stuck told the owner "working again"."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R|label!=Shortlist_mike"}),
            plextv_user(200, "mike"),
        ]
        mock_plextv.update_user_filters.side_effect = lambda *a: None  # accepted, not stored
        ctx.plex.owned_collections.return_value = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[2])}

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.promotion_blockers or not report.ok
        assert report.restrictions_restored == {}

    def test_verification_roster_read_raising_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """If the single post-write roster read (used to verify persistence) itself fails, we cannot
        confirm any exclude stuck -> fail safe, nothing promoted. list_users is called twice per run:
        once to build the roster, once to verify; only the second (verify) read raises here."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Both already have rows, so no early merge runs: the roster is read for the sync, then its read-back.
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[1]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[2]),
        }
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

    def test_a_restore_is_not_reported_when_the_read_back_itself_fails(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R|label!=Shortlist_mike"}),
            plextv_user(200, "mike"),
        ]
        # sarah already has a row too, so no early merge runs: the roster is read for the sync, then its read-back.
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[1]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[2]),
        }
        calls = {"n": 0}

        def list_users():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("plex.tv roster read failed")
            return mock_plextv.users

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert "could not verify filters" in (report.error or "")
        assert report.restrictions_restored == {}

    def test_a_restore_is_verified_even_when_another_accounts_write_already_failed(
        self, ctx: EngineContext, mock_plextv
    ):
        """Round-8 audit: the read-back was skipped once anything had failed, so a repair plex.tv accepted
        and did not keep was still announced as "working again". The read-back is read-only; run it."""
        sarah, dan = make_profile("sarah", account_id=100), make_profile("dan", account_id=300)
        mock_plextv.users = [
            plextv_user(100, "sarah", filters={"filterMovies": "contentRating!=R|label!=Shortlist_mike"}),
            plextv_user(300, "dan"),
        ]
        ctx.plex.owned_collections.return_value = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[2])}

        def put(account_id, fields):
            if account_id == 300:
                raise RuntimeError("plex.tv 503")
            # sarah's write is accepted and not stored

        mock_plextv.update_user_filters.side_effect = put

        report = pipeline_mod.run(ctx, [sarah, dan])

        assert report.promotion_blockers
        assert report.restrictions_restored == {}

    def test_account_absent_from_verification_roster_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """A write happens, but the verification roster read-back no longer lists that account (its
        share vanished mid-run) -> its just-merged exclude cannot be confirmed -> fail safe, nothing
        promoted. Reproduces the `remote2 is None -> got=''` branch of the batched verify."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        full = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.users = full
        # Both already have rows, so no early merge runs: the roster is read for the sync, then its read-back.
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[1]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[2]),
        }
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


class TestTheRunSaysItsUnmatchedWatchedSummaryOnce:
    """The per-library tally of watched titles dropped for want of a tmdb:// guid is the history
    source's; the run is what knows when every read is over, so it asks for it — once, afterwards."""

    def test_the_summary_is_asked_for_once_after_every_read(self, ctx: EngineContext, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        order: list[str] = []
        history = ctx.history_source.fetch.return_value

        def fetch(user, **kwargs):
            order.append("read")
            return history

        ctx.history_source.fetch.side_effect = fetch
        ctx.history_source.log_unmatched_summary.side_effect = lambda: order.append("summary")

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert order.count("read") == 2
        assert order[-1] == "summary" and order.count("summary") == 1
        ctx.history_source.log_unmatched_summary.assert_called_once_with()


class TestTheRunSummaryLine:
    """`run complete in …` counted `report.users`, which also holds each shared row's report (filed
    under `shared_<slug>`), so a night of 46 people and one shared row read "47 ok"."""

    def test_people_and_shared_rows_are_counted_apart(self, ctx: EngineContext, mock_plextv):
        from loguru import logger

        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Picked for You", size=5),
            RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2),
        ]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        lines: list[str] = []
        handler = logger.add(
            lines.append, level="INFO", format="{message}", filter=lambda r: r["message"].startswith("run complete")
        )
        try:
            report = pipeline_mod.run(
                ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)]
            )
        finally:
            logger.remove(handler)

        assert [u.status for u in report.users if u.slug == "shared_popular"] == ["ok"], "no shared row to count"
        assert [line.split(": ", 1)[1].strip() for line in lines] == [
            "people 2 ok, 0 failed, 0 skipped; shared rows 1 ok, 0 failed, 0 skipped (dry_run=False)"
        ]


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

        def stored_label(collection, label, *, extra=None):
            created[label.lower()] = collection
            if extra is not None:
                created[extra.lower()] = collection  # same write, on a newly created row
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

    def test_a_worker_thread_logs_under_its_callers_context(self, ctx: EngineContext, mock_plextv):
        """The server scopes a run's activity log to the run with a loguru contextvar. A pool thread
        starts with an EMPTY context, so without carrying the caller's across, every warning a person's
        work raised on the pool lost its run and never reached that run's log."""
        from loguru import logger

        users = self._users(mock_plextv)
        ctx.concurrency = 3
        history = ctx.history_source.fetch.return_value
        threads: set[int] = set()
        tagged: list[object] = []

        def fetch(user, **kwargs):
            threads.add(threading.get_ident())
            logger.warning("probe from {}", user.slug)
            return history

        ctx.history_source.fetch.side_effect = fetch
        handler = logger.add(
            lambda message: tagged.append(message.record["extra"].get("probe_run")),
            level="WARNING",
            filter=lambda record: record["message"].startswith("probe from"),
        )
        try:
            with logger.contextualize(probe_run=7):
                pipeline_mod.run(ctx, users)
        finally:
            logger.remove(handler)

        assert threads and threading.get_ident() not in threads, "the reads never ran on the pool"
        assert tagged and set(tagged) == {7}

    def test_concurrency_preserves_user_order_and_excludes(self, ctx: EngineContext, mock_plextv):
        users = self._users(mock_plextv)
        ctx.concurrency = 3
        created: dict[str, object] = {}

        def stored_label(collection, label, *, extra=None):
            created[label.lower()] = collection
            if extra is not None:
                created[extra.lower()] = collection  # same write, on a newly created row
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


class TestPerDeliveryTimeoutRetry:
    """A PMS timeout retries JUST the idempotent delivery write, NOT the whole user — so a Plex hiccup
    never re-runs the expensive gather + pick selection (the amplifier that made a large production server run 3
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


class TestIdleHoldInARun:
    """A row whose owner has watched nothing since it was last built waits out its refresh night.

    The cadence asks "is it this row's night?"; the hold asks "is there anything new to say?".
    Only when both agree does the row re-pick — and the hold has a ceiling, so an inactive person
    never ends up with a permanently frozen row (issue #109).
    """

    RUN_DAY = date(2026, 6, 15).toordinal()
    KEY = ("sarah", "picked", "1")

    def _ctx(self, ctx: EngineContext, *, hold_days: int, built_days_ago: int, watched_days_ago: int) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=watched_days_ago, rating_key=999)]
        # refresh_days=1 so it is ALWAYS the row's refresh night — every difference below is the hold.
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=3, media="movie", refresh_days=1)]
        ctx.config.min_history = 1
        ctx.config.idle_hold_days = hold_days
        ctx.run_day = self.RUN_DAY
        # The clock `make_watched(days_ago=…)` counts back from, so "watched 30 days ago" and "built
        # 5 days ago" are on one timeline. Real `now` would put every fixture watch decades in the past.
        ctx.run_at = NOW
        ctx.previous_picks = {self.KEY: self._prior(NOW - timedelta(days=built_days_ago))}
        ctx.previous_recipes = {}  # unknown recipe never forces a rebuild

    def _prior(self, built_at):
        return [
            Pick(
                tmdb_id=t,
                rating_key=2000 + t,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                built_at=built_at,
            )
            for i, t in enumerate([17, 18, 19])
        ]

    def _run(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        entries = report.users[0].trace.get("selection") or []
        assert entries, "the trace recorded no selection at all"
        return report, entries[0]

    def test_an_idle_person_holds_the_row_on_its_refresh_night(self, ctx: EngineContext, mock_plextv):
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)

        report, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] == "held_idle"
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [17, 18, 19], "the row is redelivered exactly as it was"

    def test_the_person_is_told_why_their_row_looks_identical(self, ctx: EngineContext, mock_plextv):
        """The run page shows a held person their picks and nothing else.

        Without this the second run of a night reads as a run that silently did nothing, which is
        exactly how the owner read it. The trace explains it per row; the run page needs the same
        answer at the level it asks the question.
        """
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)

        report, _entry = self._run(ctx, mock_plextv)

        assert report.users[0].reason == (
            "Their rows were due to rebuild tonight, but they haven't watched anything since those "
            "rows were built — so last run's titles were redelivered unchanged."
        )

    def test_the_sentence_covers_every_cell_of_its_own_matrix(self):
        """Five outcomes, and only two were reachable from a full pipeline run.

        The all-`carried_forward` sentence is the one nearly every install sees — it is what "it
        wasn't their night" says — and it was pinned only by a hardcoded fixture in a vitest file,
        so changing the wording here would have failed a web test that does not own the string.

        The counts are per ROW, not per trace entry: `selection` carries one entry per
        (row, library), so a single row living in a movie and a TV library must not be reported as
        two rows.
        """
        why = rows_mod._why_nothing_rebuilt

        assert why([]) is None
        assert why([{"row": "picked", "decision": "refreshed"}]) is None
        assert why([{"row": "picked", "decision": "carried_forward"}]) == (
            "It wasn't any of their rows' night to rebuild, so last run's titles were redelivered unchanged."
        )
        # One row, two libraries, both held: one row, not two.
        assert why(
            [
                {"row": "picked", "library": "Movies", "decision": "held_idle"},
                {"row": "picked", "library": "TV Shows", "decision": "held_idle"},
            ]
        ) == (
            "Their rows were due to rebuild tonight, but they haven't watched anything since those rows "
            "were built — so last run's titles were redelivered unchanged."
        )
        assert why(
            [
                {"row": "picked", "decision": "carried_forward"},
                {"row": "gems", "decision": "held_idle"},
            ]
        ) == (
            "Nothing was re-picked for them tonight: 1 row was not due to rebuild, and 1 was held "
            "because they haven't watched anything since."
        )
        assert why(
            [
                {"row": "picked", "decision": "carried_forward"},
                {"row": "gems", "decision": "carried_forward"},
                {"row": "cosy", "decision": "held_idle"},
                {"row": "loud", "decision": "held_idle"},
            ]
        ) == (
            "Nothing was re-picked for them tonight: 2 rows were not due to rebuild, and 2 were held "
            "because they haven't watched anything since."
        )

    def test_nothing_is_explained_away_when_a_row_actually_rebuilt(self, ctx: EngineContext, mock_plextv):
        """A rebuilt row puts a change on the page, and a sentence about rows that did not move
        would then be noise on top of it."""
        self._ctx(ctx, hold_days=0, built_days_ago=5, watched_days_ago=30)

        report, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] != "held_idle"
        assert report.users[0].reason is None

    def test_a_held_row_is_never_re_picked(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The saving this feature exists for: no re-selection, so delivery's unchanged-skip then
        avoids the Plex membership write too."""
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)
        built = spy_build_picks(monkeypatch)

        self._run(ctx, mock_plextv)

        assert built == []

    def test_the_trace_says_it_was_due_and_was_held_anyway(self, ctx: EngineContext, mock_plextv):
        """`refresh_night` records the CADENCE's answer, not the hold's. "It was due tonight and we
        held it" is the fact the owner needs; collapsing it to False would be indistinguishable from
        a row that simply was not due."""
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)

        _, entry = self._run(ctx, mock_plextv)

        assert entry["refresh_night"] is True
        assert entry["idle_hold_days"] == 28

    def test_a_watch_since_the_build_refreshes_as_normal(self, ctx: EngineContext, mock_plextv):
        """The whole point: they watched something, so there IS something new to say."""
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=1)

        _, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] == "refreshed"

    def test_the_hold_expires_at_the_ceiling(self, ctx: EngineContext, mock_plextv):
        """A hold, not a freeze. Past the ceiling the row rebuilds however idle they are — the row
        nobody watches is the one that most needs to look different next time they open Plex."""
        self._ctx(ctx, hold_days=28, built_days_ago=40, watched_days_ago=90)

        _, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] == "refreshed"

    def test_off_by_default(self, ctx: EngineContext, mock_plextv):
        """Every existing install: the setting is 0, so an idle person refreshes exactly as before."""
        self._ctx(ctx, hold_days=0, built_days_ago=5, watched_days_ago=30)

        _, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] == "refreshed"

    def test_blocking_a_seed_still_rebuilds_a_held_row(self, ctx: EngineContext, mock_plextv):
        """A blocked seed is an owner edit like any other, and today it lands on the very next run —
        a `{top_seed}` row is forced to a nightly cadence. With a hold on, the row would go on being
        built from (and named after) the blocked watch until the person watched something or the
        ceiling expired, which on a 60-day ceiling is two months of a row they explicitly rejected.

        `blocked_seeds` lives on the user, not in the row's settings, so `row_recipe` did not cover
        it — the clause that lets a deliberate edit outrank the hold never fired.
        """
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)
        mock_plextv.users = [plextv_user(100, "sarah")]

        # Night one: the row is built, and stamps the recipe it was built under — exactly what the
        # adapter reads back into `previous_recipes` on the next run.
        first = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        ctx.previous_recipes = {self.KEY: first.users[0].picks[0].recipe}

        # Night two: same idle person, but the owner has blocked the seed in between.
        blocked = make_profile("sarah", account_id=100)
        blocked.blocked_seeds = {999}
        second = pipeline_mod.run(ctx, [blocked])
        entry = (second.users[0].trace.get("selection") or [{}])[0]

        assert entry.get("decision") == "settings_changed"

    def test_the_recipe_stays_inside_its_column_however_many_seeds_are_blocked(self, ctx: EngineContext, mock_plextv):
        """`picks.recipe` is `String(128)`. Listing blocked seeds inline blows through that at about
        nine of them (30 blocked seeds measured 279 chars). SQLite does not truncate, so this is
        harmless today — but a truncating backend would collide two different block sets into one
        fingerprint, which is a settings change that silently never rebuilds. Hashing keeps the
        component fixed-width whatever is in the set."""
        from shortlist.server.db.models import PickRow

        limit = PickRow.__table__.c.recipe.type.length
        self._ctx(ctx, hold_days=0, built_days_ago=5, watched_days_ago=1)
        heavy = make_profile("sarah", account_id=100)
        heavy.blocked_seeds = set(range(900000, 900060))
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [heavy])

        recipe = report.users[0].picks[0].recipe
        assert len(recipe) <= limit, f"recipe is {len(recipe)} chars, column holds {limit}"

    def test_a_row_whose_seed_moved_is_never_held(self, ctx: EngineContext, mock_plextv):
        """The one thing that CAN move a `{top_seed}` row's seed while nobody is watching: an
        un-watch. `last_watch_at` then moves BACKWARDS onto an older watch, which is `<=` the build,
        so the hold engages — and `_seed_moved`, which would have caught the stale title, is only
        consulted on the refresh branch. The row goes on claiming "Because you watched X" about a
        title the owner has un-watched, for up to the ceiling. Without the hold this is impossible,
        because a `{top_seed}` row is forced to a nightly cadence.
        """
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Because you watched {top_seed}", size=3, media="movie")
        ]
        # Last run's picks were built from a seed that is no longer what the pool leads with.
        ctx.previous_picks = {
            self.KEY: [
                Pick(
                    tmdb_id=t,
                    rating_key=2000 + t,
                    title=f"T{t}",
                    rank=i + 1,
                    reason="kept",
                    media_type=MediaType.MOVIE,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    built_at=NOW - timedelta(days=5),
                    seed_tmdb_id=424242,
                    seed_title="A Film They Un-watched",
                )
                for i, t in enumerate([17, 18, 19])
            ]
        }

        _, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] != "held_idle", "a row whose title no longer matches its seed must rebuild"
        assert entry["decision"] == "seed_moved"

    def test_a_settings_change_still_rebuilds_a_held_row(self, ctx: EngineContext, mock_plextv):
        """The owner's deliberate edit outranks the hold. Making them wait up to the ceiling for an
        edit to show reads as the setting being broken — the same reason `recipe_changed` already
        beats the cadence."""
        self._ctx(ctx, hold_days=28, built_days_ago=5, watched_days_ago=30)
        ctx.previous_recipes = {self.KEY: "a-different-recipe"}

        _, entry = self._run(ctx, mock_plextv)

        assert entry["decision"] == "settings_changed"


class TestCancelStopsWritingPromptly:
    """Cancel must actually stop, not finish everything already in flight.

    Measured on a live server: with `run.concurrency` at 8, eight people are mid-delivery when
    Cancel is pressed, and each one finishing ALL of its rows on a PMS answering in ~17s left the
    run writing for minutes after the operator asked it to stop.
    """

    def test_a_person_mid_delivery_stops_before_their_next_row(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A ROW is the boundary: delivered whole, so stopping between rows leaves nothing
        half-written. Within a row is where walking away would be unsafe, and that is untouched."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, 10: 2010, 20: 2020}
        ctx.tmdb.suggestions.return_value = [
            ({"id": 10, "title": "A", "genre_ids": [], "vote_average": 8.0, "release_date": "2020-01-01"}, 1.0),
            ({"id": 20, "title": "B", "genre_ids": [], "vote_average": 7.0, "release_date": "2021-01-01"}, 0.9),
        ]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        # Two rows for one person: the first is written, then Cancel lands, so the second must not be.
        ctx.config.rows = [
            RowSpec(slug="one", name_template="One", size=2, media="movie"),
            RowSpec(slug="two", name_template="Two", size=2, media="movie"),
        ]
        ctx.config.min_history = 1
        mock_plextv.users = [plextv_user(100, "sarah")]

        # Cancel lands the moment the FIRST row reaches Plex. Tied to the write itself rather than to
        # a count of `cancelled()` calls: the count is an implementation detail that changes whenever
        # a new check is added, and this test is about the boundary, not about how often we look.
        import shortlist.engine.rows as rows_mod

        cancelled = {"yes": False}
        real_deliver = rows_mod.deliver_rows

        def deliver_then_cancel(*args, **kwargs):
            result = real_deliver(*args, **kwargs)
            cancelled["yes"] = True
            return result

        monkeypatch.setattr(rows_mod, "deliver_rows", deliver_then_cancel)
        ctx.cancelled = lambda: cancelled["yes"]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        rows_built = {e["row_slug"] for u in report.users for e in u.breakdown}
        assert rows_built == {"one"}, "the second row must not be written after a cancel"

    def test_a_row_parked_on_the_write_lock_writes_nothing_after_a_cancel(self, ctx: EngineContext, monkeypatch):
        """The boundary a cancel actually needs, and the one the two checks above cannot reach.

        Every person's Plex writes serialize on ONE `ctx.write_lock`, so at concurrency 8 seven
        people are parked INSIDE `_deliver_row` when Cancel is pressed — already past every check
        that precedes the lock. Each resuming to write a full row on a PMS answering in ~17s is the
        minutes of "Stopping…" the per-row check could not explain.
        """
        import threading

        import shortlist.engine.rows as rows_mod
        from shortlist.engine.models import UserRunReport
        from shortlist.engine.rows import RowPolicy

        wrote: list[str] = []
        monkeypatch.setattr(rows_mod, "deliver_rows", lambda *a, **k: wrote.append(a[4].slug))

        cancelled = {"yes": False}
        real_lock = threading.Lock()

        class ParkedThenCancelled:
            """Cancel arrives while this row waits its turn — what the other seven threads are doing."""

            def __enter__(self):
                real_lock.acquire()
                cancelled["yes"] = True
                return self

            def __exit__(self, *exc):
                real_lock.release()
                return False

        ctx.write_lock = ParkedThenCancelled()
        ctx.cancelled = lambda: cancelled["yes"]

        spec = RowSpec(slug="two", name_template="Two", size=2, media="movie")
        user = make_profile("sarah", account_id=100)
        report = UserRunReport(username="sarah", slug="sarah")
        policy = RowPolicy(
            ctx=ctx,
            user=user,
            cfg=ctx.config,
            specs=[spec],
            library_index={},
            report=report,
            resolve=lambda item: None,
        )
        pick = Pick(tmdb_id=10, rating_key=2010, title="A", rank=1, reason="because", media_type=MediaType.MOVIE)

        delivered = rows_mod._deliver_row(
            policy, spec, [pick], {"1": [pick]}, sole_row=True, stored_labels={}, order_work=None
        )

        assert delivered is False, "the caller must be told to stop this person, not carry on to their next row"
        assert wrote == [], "a row must not be written to Plex once the run has been cancelled"

    def test_a_cancel_during_the_retry_backoff_keeps_the_audit_for_what_was_already_written(
        self, ctx: EngineContext, monkeypatch
    ):
        """A cancel must never erase the record of a write that reached Plex (plex-safety rule 10).

        Delivery retries per row, and each attempt truncates the row's breakdown so a re-run does not
        double-count it. That truncation is only safe because the attempt re-appends — so it has to
        happen AFTER the cancel check, or a cancel landing during the backoff returns having deleted
        the audit entry for a library the first attempt really did write. Operators cancel precisely
        when a run is stalling on retries, so this is the likely case, not the exotic one.
        """
        import requests

        import shortlist.engine.rows as rows_mod
        from shortlist.engine.models import UserRunReport
        from shortlist.engine.rows import RowPolicy

        cancelled = {"yes": False}
        ctx.cancelled = lambda: cancelled["yes"]
        report = UserRunReport(username="sarah", slug="sarah")

        attempts = {"n": 0}

        def flaky_deliver(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                # Library A written and audited, library B times out — the partial state a retry exists
                # for. Cancel lands while the backoff sleeps.
                kwargs["breakdown"].append({"row_slug": "two", "library_key": "1", "rating_key": 4242})
                cancelled["yes"] = True
                raise requests.exceptions.ReadTimeout("PMS timed out on the second library")
            raise AssertionError("a cancelled row must not be re-attempted")

        monkeypatch.setattr(rows_mod, "deliver_rows", flaky_deliver)

        spec = RowSpec(slug="two", name_template="Two", size=2, media="movie")
        policy = RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100),
            cfg=ctx.config,
            specs=[spec],
            library_index={},
            report=report,
            resolve=lambda item: None,
        )
        pick = Pick(tmdb_id=10, rating_key=2010, title="A", rank=1, reason="because", media_type=MediaType.MOVIE)

        delivered = rows_mod._deliver_row(
            policy, spec, [pick], {"1": [pick]}, sole_row=True, stored_labels={}, order_work=None
        )

        assert delivered is False
        assert [e["rating_key"] for e in report.breakdown] == [4242], (
            "the collection the first attempt wrote to Plex must keep its audit entry — without it "
            "the delivery ledger loses the ratingKey and the next run builds a second collection "
            "beside the orphan"
        )


class TestRunUserCost:
    def test_setup_and_every_entered_row_are_timed(self, ctx: EngineContext, mock_plextv):
        """Every row the loop ENTERS gets a `row_timing` entry — including one whose only source is
        down, which `pools_for` turns into `None` and the row loop `continue`s past. Without an
        entry for that row the UI cannot tell 'finished with nothing to show' from 'never recorded'.

        The two rows are given DIFFERENT sources on purpose: identical rows share one pool key (see
        `TestPoolCosts`), so both would always succeed or fail together and neither could `continue`
        without the other. Only `tmdb_discover` is made to fail, so `picked-for-you` (the default
        `tmdb_similar`) still delivers — the real property under test is that a row recorded via
        `_row_timer` but never delivered is distinguishable from one that was: it's in `row_timing`
        but absent from `breakdown`.
        """
        ctx.config.rows = [
            RowSpec(slug="picked-for-you", name_template="Picked for You", size=5),
            RowSpec(
                slug="because-you-watched",
                name_template="Because You Watched",
                size=5,
                candidate_sources=["tmdb_discover"],
            ),
        ]
        ctx.tmdb.discover.side_effect = RuntimeError("tmdb_discover down")

        def slow_fetch(*_args, **_kwargs) -> list:
            # Keeps setup_s deterministically non-zero — round(x, 3) in _run_user collapses a sub-ms
            # span to exactly 0.0, which would make `report.setup_s > 0` fail by rounding accident.
            time.sleep(0.01)
            return [make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)]

        ctx.history_source.fetch.side_effect = slow_fetch
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

        assert report.setup_s > 0
        assert set(report.row_timing) == {"picked-for-you", "because-you-watched"}, (
            "the row that continue'd past a dead source must still be timed, not silently dropped"
        )
        assert {b["row_slug"] for b in report.breakdown} == {"picked-for-you"}, (
            "the dead-source row delivered nothing and must not appear in the delivery breakdown"
        )


class TestPoolCosts:
    def test_two_rows_sharing_a_pool_record_one_entry_naming_both(self, ctx: EngineContext, mock_plextv):
        """The whole point of the honest split: one gather, one token figure, both rows named.
        A cache HIT must still attribute its row, or the pool reads as belonging to one row."""
        report = _run_two_row_user(ctx, mock_plextv)
        assert len(report.pool_costs) == 1
        entry = report.pool_costs[0]
        assert sorted(entry["rows"]) == ["because-you-watched", "picked-for-you"]
        assert entry["tokens"] == report.llm_tokens
        assert entry["label"]

    def test_cold_start_user_records_no_pools(self, ctx: EngineContext, mock_plextv):
        """Cold start never builds a pool. `[]` is the true answer, not missing data."""
        report = _run_cold_user(ctx, mock_plextv)
        assert report.pool_costs == []


class _FakeClock:
    """A `time` stand-in for `rows_mod`: `monotonic()` moves only when the test says so."""

    def __init__(self) -> None:
        self.now = 100.0
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRowTiming:
    @pytest.fixture
    def clock(self, monkeypatch) -> _FakeClock:
        # Swap the module's `time` name, not `time.monotonic` itself: that is process-global.
        clock = _FakeClock()
        monkeypatch.setattr(rows_mod, "time", clock)
        return clock

    def test_row_timer_records_duration_when_body_completes(self, clock):
        report = UserRunReport(username="alex", slug="alex")
        with rows_mod._row_timer(report, "picked-for-you"):
            clock.advance(0.25)
        assert report.row_timing["picked-for-you"]["duration_s"] == 0.25
        assert report.row_timing["picked-for-you"]["blocked_s"] == 0.0
        assert report.lock_bucket is None

    def test_row_timer_records_duration_when_body_breaks_early(self, clock):
        """The delivery loop `break`s on cancel — an interrupted row still cost the time it spent."""
        report = UserRunReport(username="alex", slug="alex")
        for _ in range(1):
            with rows_mod._row_timer(report, "because-you-watched"):
                clock.advance(0.25)
                break
        assert report.row_timing["because-you-watched"]["duration_s"] == 0.25

    def test_row_timer_records_duration_when_body_raises(self, clock):
        report = UserRunReport(username="alex", slug="alex")
        try:
            with rows_mod._row_timer(report, "picked-for-you"):
                clock.advance(0.25)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert report.row_timing["picked-for-you"]["duration_s"] == 0.25
        assert report.lock_bucket is None

    def test_timed_lock_charges_wait_to_the_current_row(self, clock):
        from shortlist.engine.context import EngineContext

        ctx = EngineContext.__new__(EngineContext)
        ctx.write_lock = threading.Lock()
        report = UserRunReport(username="alex", slug="alex")

        holder_has_lock = threading.Event()

        def hold() -> None:
            with ctx.write_lock:
                holder_has_lock.set()
                # The row timer reads the clock once and the lock's wait timer once, before it blocks:
                # the second read means the requester is about to wait, so the "wait" is exactly this advance.
                deadline = time.monotonic() + 5
                while clock.reads < 2 and time.monotonic() < deadline:
                    time.sleep(0.001)
                clock.advance(0.5)

        t = threading.Thread(target=hold)
        t.start()
        holder_has_lock.wait(timeout=2)
        with rows_mod._row_timer(report, "picked-for-you"), rows_mod._timed_lock(ctx, report):
            pass
        t.join(timeout=2)

        assert report.row_timing["picked-for-you"]["blocked_s"] == 0.5

    def test_timed_lock_charges_nothing_during_setup(self):
        """lock_bucket is None before the row loop — that wait belongs to setup_s, not to a row."""
        from shortlist.engine.context import EngineContext

        ctx = EngineContext.__new__(EngineContext)
        ctx.write_lock = threading.Lock()
        report = UserRunReport(username="alex", slug="alex")
        with rows_mod._timed_lock(ctx, report):
            pass
        assert report.row_timing == {}
