"""Leak-safe ordering: first-row hiding, convergence, orphan deletion and the privacy loop."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import MagicMock
from unittest.mock import call as plex_call

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import rows as rows_mod
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import FilterWriteRefused
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    row_marker,
)
from shortlist.engine.models import (
    OwnedRow,
    RowSpec,
    UserType,
)
from tests.conftest import make_profile, plextv_user
from tests.unit.pipeline_support import ctx  # noqa: F401


class TestAFirstRowIsHiddenAsSoonAsItsPersonIsDelivered:
    """A person's first row has no `label!=` exclude in anyone's share filter until a merge writes one.

    Measured on a real server (tests/fixtures/pms_collections_tab_filter_visibility.json): collection mode "hide" does
    nothing for the library's Collections tab — the browse-hidden, unexcluded shared rows were listed
    there, 2 of 2 — while every row that account's filter excluded was not, 0 of 180. So a first row
    created early in a run sat in every shared account's Collections tab, title and titles, until the
    merge at the END of delivery: about an hour on a large library. People whose rows already exist
    are unaffected — their exclude is already on every share.
    """

    def _mark_end_of_run_merge(self, events: list[tuple], monkeypatch) -> None:
        end_of_run_merge = pipeline_mod._privacy_sync_phase

        def marked(*args, **kwargs):
            events.append(("end-of-run merge",))
            return end_of_run_merge(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod, "_privacy_sync_phase", marked)

    def _record_writes(self, ctx: EngineContext, mock_plextv) -> list[tuple]:
        events: list[tuple] = []

        def create(section, title, items):
            events.append(("create", title))
            return MagicMock()

        def put(account_id, fields):
            events.append(("filter", account_id, dict(fields)))
            for u in mock_plextv.users:
                if u.id == account_id:
                    u.filters.update(fields)

        ctx.plex.create_collection.side_effect = create
        mock_plextv.update_user_filters.side_effect = put
        return events

    def test_each_new_persons_row_is_excluded_everywhere_before_the_next_person_is_delivered(
        self, ctx: EngineContext, mock_plextv
    ):
        # mike and jess are new; sarah already has a row. Two new people, so the first of them must not
        # wait for the second.
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [
            plextv_user(100, "sarah"),
            plextv_user(200, "mike"),
            plextv_user(300, "jess"),
            plextv_user(400, "dan"),
        ]
        events = self._record_writes(ctx, mock_plextv)
        order = [
            make_profile("mike", account_id=200),
            make_profile("sarah", account_id=100),
            make_profile("jess", account_id=300),
        ]

        report = pipeline_mod.run(ctx, order)

        def create_of(account_id: int) -> int:
            return next(i for i, e in enumerate(events) if e[0] == "create" and e[1].endswith(row_marker(account_id)))

        def first_hide_of(label: str) -> int:
            return next(i for i, e in enumerate(events) if e[0] == "filter" and label in e[2].get("filterMovies", ""))

        assert create_of(200) < first_hide_of("Shortlist_mike") < create_of(100), events
        assert create_of(300) < first_hide_of("Shortlist_jess"), events
        hidden_from = {e[1] for e in events if e[0] == "filter" and "Shortlist_mike" in e[2].get("filterMovies", "")}
        assert {100, 300, 400} <= hidden_from, "every other account, not just tonight's people"
        assert 200 not in hidden_from, "a person must never be hidden from their own row"
        assert [u.slug for u in report.users] == ["mike", "sarah", "jess"]

    def test_at_concurrency_no_other_row_is_written_between_a_first_row_and_its_exclude(
        self, ctx: EngineContext, mock_plextv
    ):
        """Production runs 4 people at a time. The exclude has to go on inside the same hold of the write lock
        that wrote the first row, or rows other people have queued land in between."""
        ctx.concurrency = 3
        mock_plextv.users = [plextv_user(a, n) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"), (400, "dan"))]
        events = self._record_writes(ctx, mock_plextv)
        put = mock_plextv.update_user_filters.side_effect
        create = ctx.plex.create_collection.side_effect

        def slow_create(section, title, items):
            time.sleep(0.02)  # give the other workers time to queue on the lock
            return create(section, title, items)

        def put_checking_the_lock(account_id, fields):
            assert ctx.write_lock.locked(), "a share filter was written without the write lock"
            put(account_id, fields)

        ctx.plex.create_collection.side_effect = slow_create
        mock_plextv.update_user_filters.side_effect = put_checking_the_lock
        people = [make_profile(n, account_id=a) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"))]
        result: dict = {}
        worker = threading.Thread(target=lambda: result.setdefault("report", pipeline_mod.run(ctx, people)))
        worker.start()
        worker.join(timeout=30)

        assert not worker.is_alive(), "the run deadlocked"
        assert all(u.status == "ok" for u in result["report"].users)
        for person in people:
            label = f"Shortlist_{person.slug}"
            created = next(
                i
                for i, e in enumerate(events)
                if e[0] == "create" and e[1].endswith(row_marker(person.plex_account_id))
            )
            hidden = next(i for i, e in enumerate(events) if e[0] == "filter" and label in e[2].get("filterMovies", ""))
            between = [e for e in events[created + 1 : hidden] if e[0] == "create"]
            assert not between, f"{person.slug}: {between} was written between their row and its exclude"

    def test_a_person_whose_delivery_fails_after_their_row_exists_is_still_hidden_early(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        events = self._record_writes(ctx, mock_plextv)
        deliver = rows_mod.deliver_rows

        def deliver_then_fail(plex, profile, *args, **kwargs):
            outcome = deliver(plex, profile, *args, **kwargs)
            if profile.slug == "mike":
                raise ValueError("a failure after the row was written")
            return outcome

        monkeypatch.setattr(rows_mod, "deliver_rows", deliver_then_fail)

        report = pipeline_mod.run(ctx, [make_profile("mike", account_id=200), make_profile("sarah", account_id=100)])

        assert next(u for u in report.users if u.slug == "mike").status == "error"
        mike_hidden = next(
            i for i, e in enumerate(events) if e[0] == "filter" and "Shortlist_mike" in e[2].get("filterMovies", "")
        )
        sarah_create = next(i for i, e in enumerate(events) if e[0] == "create" and e[1].endswith(row_marker(100)))
        assert mike_hidden < sarah_create, events

    def test_plex_tv_failing_filter_writes_stops_early_merges_for_the_rest_of_the_run(
        self, ctx: EngineContext, mock_plextv
    ):
        """Each failing write retries with backoff for a minute or more, under the write lock. Repeating that for
        every new person would stall every delivery for hours; the end-of-run pass covers what was skipped."""
        mock_plextv.users = [plextv_user(a, n) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"), (400, "dan"))]
        self._record_writes(ctx, mock_plextv)
        attempts_during_delivery: list[int] = []
        delivering = {"done": False}

        def failing_put(account_id, fields):
            if not delivering["done"]:
                attempts_during_delivery.append(account_id)
            raise ConnectionError("plex.tv unreachable")

        mock_plextv.update_user_filters.side_effect = failing_put
        ctx.progress = lambda slug, stage, counts, reason=None: (
            delivering.__setitem__("done", True) if stage == "users_done" else None
        )
        people = [make_profile(n, account_id=a) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"))]

        report = pipeline_mod.run(ctx, people)

        assert len(attempts_during_delivery) == 1, attempts_during_delivery
        assert not report.ok, "the end-of-run pass must still try, fail, and block promotion"

    def test_accounts_hidden_early_are_still_spot_checked_for_enforcement(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """The enforcement spot-check skips an account written SECONDS ago (Plex takes ~25s to apply a
        filter). One written at the start of delivery, long before the end, is not that — skipping it would
        leave every account unmeasured on any night someone new got a row."""
        clock = iter(range(0, 10_000, 100))  # every reading 100s after the last
        monkeypatch.setattr(pipeline_mod.time, "monotonic", lambda: next(clock))
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike"), plextv_user(300, "jess")]
        self._record_writes(ctx, mock_plextv)
        ctx.token_for_user = MagicMock(return_value="server-token")
        ctx.plex.user_hubs.return_value = []

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert ctx.token_for_user.called, "every account hidden early was skipped by the enforcement check"

    def test_an_early_write_plex_tv_did_not_keep_is_written_again_at_the_end(self, ctx: EngineContext, mock_plextv):
        """The early merge has no read-back of its own. The end-of-run pass reads every filter fresh, so an
        early write that never stuck looks like a missing exclude there, and is written — and verified."""
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        dropped: set[int] = set()

        def put_dropping_the_first_write(account_id, fields):
            if account_id not in dropped:
                dropped.add(account_id)  # answered 200, stored nothing
                return
            next(u for u in mock_plextv.users if u.id == account_id).filters.update(fields)

        mock_plextv.update_user_filters.side_effect = put_dropping_the_first_write

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert 100 in dropped, "the early merge never wrote sarah's filter, so this proved nothing"
        assert "Shortlist_mike" in next(u for u in mock_plextv.users if u.id == 100).filters["filterMovies"]
        assert report.ok

    def test_an_account_written_seconds_ago_is_not_spot_checked(self, ctx: EngineContext, mock_plextv):
        """A run for one new person: the end-of-run pass starts right after the early merge, well inside the
        ~25s Plex takes to apply it, so reading that account's Home would measure the OLD filter."""
        mock_plextv.users = [plextv_user(200, "mike"), plextv_user(300, "jess")]
        self._record_writes(ctx, mock_plextv)
        ctx.plex.owned_collections.return_value = {"mike": OwnedRow(label="Shortlist_mike", rating_keys=[502])}
        ctx.token_for_user = MagicMock(return_value="server-token")
        ctx.plex.user_hubs.return_value = []
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))
        jess = make_profile("jess", account_id=300)
        pipeline_mod._record_filter_write(report, jess, {"filterMovies": ("", "label!=Shortlist_mike")})
        roster = {300: plextv_user(300, "jess", filters={"filterMovies": "label!=Shortlist_mike"})}

        pipeline_mod._verify_filters_enforced(ctx, [jess], roster, ctx.plex.owned_collections(), True, report)

        ctx.token_for_user.assert_not_called()

    def test_a_restriction_the_early_merge_repairs_is_still_reported_as_working_again(
        self, ctx: EngineContext, mock_plextv
    ):
        """#116: an owner restriction switched off by an old `|` join is switched back on by whichever pass
        writes that account first. On a night someone new gets a row that is the early merge, and the end
        pass then has nothing to write — the notice must not depend on which pass did it."""
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [
            plextv_user(100, "sarah"),
            plextv_user(200, "mike"),
            plextv_user(300, "jess", filters={"filterMovies": "contentRating!=R|label!=Shortlist_sarah"}),
        ]
        self._record_writes(ctx, mock_plextv)

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert next(u for u in mock_plextv.users if u.id == 300).filters["filterMovies"].startswith("contentRating!=R&")
        assert report.restrictions_restored == {300: "jess"}

    def test_a_repair_the_early_merge_reported_but_plex_tv_did_not_keep_is_withdrawn(
        self, ctx: EngineContext, mock_plextv
    ):
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        broken = "contentRating!=R|label!=Shortlist_sarah"
        mock_plextv.users = [
            plextv_user(100, "sarah"),
            plextv_user(200, "mike"),
            plextv_user(300, "jess", filters={"filterMovies": broken}),
        ]
        self._record_writes(ctx, mock_plextv)
        put = mock_plextv.update_user_filters.side_effect

        jess_writes = {"n": 0}

        def never_keep_jess(account_id, fields):
            if account_id != 300:
                put(account_id, fields)
                return
            jess_writes["n"] += 1
            if jess_writes["n"] > 1:  # the end-of-run rewrite fails outright, so only the fresh read can tell
                raise RuntimeError("(500) plex.tv")

        mock_plextv.update_user_filters.side_effect = never_keep_jess

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert 300 not in report.restrictions_restored

    def test_no_extra_merge_when_nobody_is_new(self, ctx: EngineContext, mock_plextv):
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[502]),
        }
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        events = self._record_writes(ctx, mock_plextv)

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        last_create = max(i for i, e in enumerate(events) if e[0] == "create")
        assert all(i > last_create for i, e in enumerate(events) if e[0] == "filter"), events

    def test_the_early_merge_itself_honours_dry_run(self, ctx: EngineContext, mock_plextv):
        # A dry run never stores a label, so the pipeline never reaches this; the function still must not write.
        ctx.config.dry_run = True
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))

        pipeline_mod._exclude_first_rows(
            ctx, [make_profile("mike", account_id=200)], {"mike": "Shortlist_mike"}, report, who="mike"
        )

        mock_plextv.update_user_filters.assert_not_called()
        assert report.filter_writes[100]["fields"]["filterMovies"][1] == "label!=Shortlist_mike"

    def test_an_account_left_alone_gets_no_early_exclude(self, ctx: EngineContext, mock_plextv):
        """ "Leave this account's Plex sharing alone" (#92): the end-of-run pass REMOVES our excludes from it,
        so writing one early would flip its filter twice a night."""
        ctx.unmanaged_account_ids = {100}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(300, "jess")]
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))

        pipeline_mod._exclude_first_rows(
            ctx, [make_profile("mike", account_id=200)], {"mike": "Shortlist_mike"}, report, who="mike"
        )

        assert [c.args[0] for c in mock_plextv.update_user_filters.call_args_list] == [300]

    def test_one_account_refusing_the_early_write_does_not_stop_the_others(self, ctx: EngineContext, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(300, "jess")]
        self._record_writes(ctx, mock_plextv)
        put = mock_plextv.update_user_filters.side_effect

        def refuse_sarah(account_id, fields):
            if account_id == 100:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 100: HTTP 422")
            put(account_id, fields)

        mock_plextv.update_user_filters.side_effect = refuse_sarah
        report = pipeline_mod.RunReport(started_at=datetime.now(UTC))

        pipeline_mod._exclude_first_rows(
            ctx, [make_profile("mike", account_id=200)], {"mike": "Shortlist_mike"}, report, who="mike"
        )

        assert "Shortlist_mike" in next(u for u in mock_plextv.users if u.id == 300).filters["filterMovies"]
        assert list(report.filter_writes) == [300]

    def test_an_early_merge_that_fails_leaves_the_end_of_run_merge_to_hide_the_row(
        self, ctx: EngineContext, mock_plextv
    ):
        ctx.plex.owned_collections.return_value = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501])}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        self._record_writes(ctx, mock_plextv)
        roster_reads = {"n": 0}

        def list_users():
            roster_reads["n"] += 1
            if roster_reads["n"] == 1:
                raise RuntimeError("plex.tv 503")
            return mock_plextv.users

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        assert all(u.status == "ok" for u in report.users)
        sarah = next(u for u in mock_plextv.users if u.id == 100)
        assert "Shortlist_mike" in sarah.filters["filterMovies"]

    # A public row nobody is disabled from is the third cell: it is excluded from nobody, so there is no
    # exclude to write early or late — covered by the shared-row audience tests in test_privacy.py.
    @pytest.mark.parametrize(
        ("audience", "disabled", "outsiders"),
        [
            pytest.param({100, 200}, set(), {300, 400}, id="subset-audience"),
            pytest.param(None, {300}, {300}, id="public-row-disabled-account"),
        ],
    )
    def test_a_new_shared_row_is_excluded_from_outsiders_before_the_next_row_is_written(
        self, ctx: EngineContext, mock_plextv, monkeypatch, audience, disabled, outsiders
    ):
        """Shared rows are delivered after every person's row, so every early merge above has already run by
        the time a brand-new shared row exists. It sits in an outsider's Collections tab exactly like a
        person's first row, so it is hidden the same way: before the next row is written, not at run's end."""
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[502]),
        }
        ctx.config.rows = [
            RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2, audience=audience),
            RowSpec(slug="gems", name_template="Gems", size=5, shared=True, min_watchers=2, audience=audience),
        ]
        ctx.disabled_account_ids = disabled
        mock_plextv.users = [plextv_user(a, n) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"), (400, "dan"))]
        events = self._record_writes(ctx, mock_plextv)
        self._mark_end_of_run_merge(events, monkeypatch)

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        def create_of(title: str) -> int:
            return next(i for i, e in enumerate(events) if e[0] == "create" and e[1].startswith(title))

        def hides_of(label: str) -> list[tuple[int, int]]:
            return [
                (i, e[1])
                for i, e in enumerate(events)
                if e[0] == "filter" and label in e[2].get("filterMovies", "").lower()
            ]

        end = events.index(("end-of-run merge",))
        popular, gems = hides_of("shortlist__shared_popular"), hides_of("shortlist__shared_gems")
        assert popular and gems, events
        assert create_of("Popular") < popular[0][0] < create_of("Gems"), events
        assert create_of("Gems") < gems[0][0] < end, events
        assert {account for _, account in popular} == outsiders, "every outsider, and nobody in the audience"

    def test_a_new_shared_row_is_hidden_before_its_second_library_is_written(self, ctx: EngineContext, mock_plextv):
        movies, classics = (
            MagicMock(type="movie", key=key, title=title) for key, title in (("1", "Movies"), ("2", "Classics"))
        )
        for section in (movies, classics):
            section.collections.return_value = []
        ctx.plex.sections.return_value = [movies, classics]
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[502]),
        }
        ctx.config.rows = [
            RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2, audience={100, 200})
        ]
        mock_plextv.users = [plextv_user(a, n) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"))]
        events = self._record_writes(ctx, mock_plextv)

        def create(section, title, items):
            events.append(("create", title, section.key))
            return MagicMock()

        ctx.plex.create_collection.side_effect = create

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        creates = [i for i, e in enumerate(events) if e[0] == "create"]
        hides = [
            i
            for i, e in enumerate(events)
            if e[0] == "filter" and "shortlist__shared_popular" in e[2].get("filterMovies", "").lower()
        ]
        assert len(creates) == 2 and hides, events
        assert creates[0] < hides[0] < creates[1], events

    def test_no_early_merge_for_a_shared_row_already_on_the_server(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # The PMS read files it under `_shared_popular` and Plex title-cases its label; matching on either as-is
        # would treat every shared row as new and walk every account for it, every night.
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[501]),
            "mike": OwnedRow(label="Shortlist_mike", rating_keys=[502]),
            "_shared_popular": OwnedRow(label="Shortlist__shared_popular", rating_keys=[503]),
        }
        ctx.config.rows = [
            RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2, audience={100, 200})
        ]
        mock_plextv.users = [plextv_user(a, n) for a, n in ((100, "sarah"), (200, "mike"), (300, "jess"))]
        events = self._record_writes(ctx, mock_plextv)
        self._mark_end_of_run_merge(events, monkeypatch)

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        end = events.index(("end-of-run merge",))
        assert any(e[0] == "create" and e[1].startswith("Popular") for e in events[:end]), events
        assert not [e for e in events[:end] if e[0] == "filter"], events


class TestCollectionOrderPhase:
    """The deferred, post-promote item-ordering pass: best-effort, never fatal to an already-delivered run."""

    def test_the_ordering_pass_counts_itself_out_too(self, ctx: EngineContext):
        """`ordering` announced itself once and then went silent for the whole pass.

        It is one PMS round-trip per MOVED ITEM, so a cold rollout spends minutes in here — and the
        header sat on "Finishing up · ordering rows" with no number for the duration, which is the
        wedged look the tail narration exists to remove (owner, run #10, 2026-08-17). `filters` and
        `promoting` have counted themselves out since the last time this bug appeared; this one had
        been missed.
        """
        emitted: list[dict] = []
        ctx.progress = lambda slug, stage, counts, reason=None: emitted.append(counts) if stage == "ordering" else None
        ctx.plex.order_collection.return_value = 0
        order_work = [(MagicMock(ratingKey=key), [key]) for key in (11, 22, 33)]

        pipeline_mod._collection_order_phase(ctx, order_work)

        assert [(c["done"], c["total"]) for c in emitted] == [(1, 3), (2, 3), (3, 3)]

    def test_the_ordering_count_promises_a_total_it_can_reach(self, ctx: EngineContext):
        """The pass de-dupes by ratingKey — a delivery retried after a mid-run timeout appends the
        same collection twice — so counting `order_work` would stall the header one short of a total
        it was never going to reach."""
        emitted: list[dict] = []
        ctx.progress = lambda slug, stage, counts, reason=None: emitted.append(counts) if stage == "ordering" else None
        ctx.plex.order_collection.return_value = 0
        repeated = MagicMock(ratingKey=11)
        order_work = [(repeated, [11]), (repeated, [11]), (MagicMock(ratingKey=22), [22])]

        pipeline_mod._collection_order_phase(ctx, order_work)

        assert [(c["done"], c["total"]) for c in emitted] == [(1, 2), (2, 2)]
        assert emitted[-1]["done"] == emitted[-1]["total"], "the count never reaches its total"

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

        ctx.plex.place_rows.assert_not_called()
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
        """The a large production server case: a shared user's row left on the owner's Home by an older build, whose
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

    def test_the_constant_label_does_not_turn_a_live_row_into_an_orphan(self, ctx: EngineContext):
        """Every row now carries a constant `Shortlist` label beside its `Shortlist_<user>` one.

        Orphan detection chops the owner's slug off the front of the label. It matches on
        `shortlist_` WITH the underscore, so the constant label is skipped and `Shortlist_sarah` is
        found — but if that prefix were ever loosened to `shortlist`, the constant label would match
        first and yield an EMPTY slug. Empty is in nobody's roster, so every row on the server would
        classify as an orphan, and orphans are the one thing this phase DELETES.

        This is the blast radius of a one-character edit, so it is pinned against the real function
        rather than against the string prefix alone.
        """
        collection = self._collection(1, "Shortlist")
        collection.labels = [MagicMock(tag="Shortlist"), MagicMock(tag="Shortlist_sarah")]

        report = self._run(ctx, [collection], known={100: "sarah"}, may_delete=True)

        assert report.orphans_removed == [], "a row whose owner is known must never be deleted"
        collection.delete.assert_not_called()

    def test_a_row_carrying_ONLY_the_constant_label_is_left_alone(self, ctx: EngineContext):
        """Belt and braces: a collection with the constant label and no owner label is not something
        this app creates — delivery applies the owner label first and deletes the row if it fails. It
        must not be read as an orphan on the strength of a label that names nobody."""
        collection = self._collection(1, "Shortlist")

        report = self._run(ctx, [collection], known={100: "sarah"}, may_delete=True)

        assert report.orphans_removed == []
        collection.delete.assert_not_called()

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


ALL_SURFACES_OFF = plex_call(recommended=False, home=False, shared=False)


class TestConvergeRecordsEachRowItTouched:
    """`report.converged` and `report.orphans_removed` hold bare labels — no collection, no library, no reason —
    so no exact audit event could be built from them, and a converge demotion or orphan delete reached Plex with
    no event at all (plex-safety rule 10). Each branch now records an entry beside them.

    Every cell also pins the exact Plex calls its branch makes: recording what happened must not change what
    happens. The four demotion branches are four cells because each decides on a different reason and three of
    them make a different write.
    """

    _collection = TestConverge._collection

    # reason -> (the row's label, how the pass is set up, the Plex calls a live pass makes, the hub write)
    DEMOTIONS: ClassVar[dict[str, tuple]] = {
        "paused": (
            "Shortlist_sarah",
            {"paused": {"sarah"}},
            lambda c: [plex_call.claims_any_surface(c), plex_call.demote_all(c, reason="paused")],
            ALL_SURFACES_OFF,
        ),
        "shared_row_switched_off": (
            "Shortlist__shared_retired",
            {},
            lambda c: [plex_call.claims_any_surface(c), plex_call.demote_all(c, reason="row switched off")],
            ALL_SURFACES_OFF,
        ),
        "unknown_owner": (
            "Shortlist_ghost",
            {"known": {100: "steve"}},
            lambda c: [plex_call.claims_any_surface(c), plex_call.demote_all(c, reason="unknown owner")],
            ALL_SURFACES_OFF,
        ),
        "on_owner_home": (
            "Shortlist_mike",
            {},
            lambda c: [plex_call.reads_as_on_owner_home(c), plex_call.demote_own_home(c)],
            plex_call(recommended=True, home=False, shared=True),
        ),
    }

    def _run(self, ctx, collection, *, paused=frozenset(), known=None, may_delete=False, dry_run=False):
        from shortlist.engine.models import RunReport
        from shortlist.engine.pipeline import _converge_phase

        section = ctx.plex.sections.return_value[0]
        section.key = 1
        section.collections.return_value = [collection]
        ctx.owner_slug = "steve"
        ctx.paused_slugs = set(paused)
        ctx.known_slugs = known or {}
        ctx.may_delete_orphans = may_delete
        ctx.config.dry_run = dry_run
        ctx.config.rows = []
        # The REAL reads and demotes, so a cell covers the read-then-write contract, not a stub's answer.
        ctx.plex.claims_any_surface.side_effect = lambda c: PlexClient.claims_any_surface(ctx.plex, c)
        ctx.plex.demote_all.side_effect = lambda c, **kw: PlexClient.demote_all(ctx.plex, c, **kw)
        ctx.plex.reads_as_on_owner_home.side_effect = lambda c: PlexClient.reads_as_on_owner_home(ctx.plex, c)
        ctx.plex.demote_own_home.side_effect = lambda c: PlexClient.demote_own_home(ctx.plex, c)
        report = RunReport(started_at=datetime.now(UTC))
        _converge_phase(ctx, set(), report)
        return report

    @staticmethod
    def _entry(label: str, **extra) -> dict:
        return {"label": label, "title": "row-1", "rating_key": 1, "library_key": "1", "library": "Movies", **extra}

    @pytest.mark.parametrize("reason", list(DEMOTIONS))
    def test_a_demotion_is_recorded_with_its_collection_library_and_reason(self, ctx: EngineContext, reason):
        label, setup, _, _ = self.DEMOTIONS[reason]

        report = self._run(ctx, self._collection(1, label), **setup)

        assert report.converge_demotions == [self._entry(label, reason=reason)]
        assert report.orphan_deletions == []

    @pytest.mark.parametrize("reason", list(DEMOTIONS))
    def test_a_dry_run_records_the_demotion_it_would_make(self, ctx: EngineContext, reason):
        """The same rows `converged` lists in a preview — which is the actual list, not every candidate."""
        label, setup, _, _ = self.DEMOTIONS[reason]

        report = self._run(ctx, self._collection(1, label), dry_run=True, **setup)

        assert report.converge_demotions == [self._entry(label, reason=reason)]

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry_run"])
    @pytest.mark.parametrize("reason", list(DEMOTIONS))
    def test_each_demotion_branch_makes_the_same_plex_calls(self, ctx: EngineContext, reason, dry_run):
        """Regression pin: the reads, the write and its kwargs, in order — and in a dry run the reads alone."""
        label, setup, plex_calls, hub_write = self.DEMOTIONS[reason]
        row = self._collection(1, label)

        report = self._run(ctx, row, dry_run=dry_run, **setup)

        expected = plex_calls(row)[:1] if dry_run else plex_calls(row)
        assert ctx.plex.method_calls == [plex_call.sections(), *expected]
        assert row.visibility.return_value.updateVisibility.call_args_list == ([] if dry_run else [hub_write])
        assert report.converged == [label]
        assert report.orphans_removed == []

    def test_a_demote_plex_did_not_make_is_not_recorded(self, ctx: EngineContext):
        """`demote_all` reads the hub again and writes nothing when it no longer claims a surface — a row that
        came down between the check and the write. An entry says Plex changed, so it follows `converged`."""
        row = self._collection(1, "Shortlist_sarah")
        settled = MagicMock(promotedToRecommended=False, promotedToOwnHome=False, promotedToSharedHome=False)
        row.visibility.side_effect = [row.visibility.return_value, settled]

        report = self._run(ctx, row, paused={"sarah"})

        settled.updateVisibility.assert_not_called()
        assert report.converged == []
        assert report.converge_demotions == []

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_an_orphan_deletion_is_recorded_with_its_collection_and_library(self, ctx: EngineContext, dry_run):
        row = self._collection(1, "Shortlist_ghost")

        report = self._run(ctx, row, known={100: "steve"}, may_delete=True, dry_run=dry_run)

        assert report.orphan_deletions == [self._entry("Shortlist_ghost")]
        assert report.converge_demotions == []

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry_run"])
    def test_an_orphan_deletion_makes_the_same_plex_calls(self, ctx: EngineContext, dry_run):
        """Regression pin: one delete, with Shortlist's label prefix as the ownership proof, and nothing else."""
        row = self._collection(1, "Shortlist_ghost")

        report = self._run(ctx, row, known={100: "steve"}, may_delete=True, dry_run=dry_run)

        deletes = [] if dry_run else [plex_call.delete_owned_collection(row, "shortlist")]
        assert ctx.plex.method_calls == [plex_call.sections(), *deletes]
        row.visibility.assert_not_called()
        assert report.orphans_removed == ["Shortlist_ghost"]
        assert report.converged == []

    def test_a_pass_with_no_users_records_no_deletion_however_complete_the_roster(self, ctx: EngineContext):
        """Why the three run-less jobs audit demotions and not deletions: `engine_run(ctx, [])` hands converge no
        delete authority, so an orphan is DEMOTED (and recorded as one) even when the roster is complete and the
        context would allow it. Through the real `run`, not `_converge_phase`, so the authority it passes is
        what is pinned."""
        orphan = self._collection(1, "Shortlist_ghost")
        section = ctx.plex.sections.return_value[0]
        section.key = 1
        section.collections.return_value = [orphan]
        ctx.owner_slug = "steve"
        ctx.known_slugs = {100: "steve", 200: "sarah"}
        ctx.may_delete_orphans = True
        ctx.plex.claims_any_surface.return_value = True
        ctx.plex.demote_all.return_value = True

        report = pipeline_mod.run(ctx, [])

        ctx.plex.delete_owned_collection.assert_not_called()
        assert report.orphan_deletions == []
        assert report.converge_demotions == [self._entry("Shortlist_ghost", reason="unknown_owner")]


def _profiled_remote(profile: str, account_id: int = 500, username: str = "kid"):
    """A Plex Home account with a parental Restriction Profile — the kind Plex refuses a hide-list for."""
    from shortlist.engine.clients.plextv import PlexTvUser

    return PlexTvUser(
        id=account_id,
        username=username,
        user_type=UserType.MANAGED,
        home=True,
        restricted=True,
        protected=False,
        restriction_profile=profile,
        filters=dict.fromkeys(("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos"), ""),
    )


class TestAccountsThePrivacyLoopCannotVouchFor:
    """An account the privacy loop could not vouch for is RECORDED, never left silent.

    The run page counts every account it is not told about as "hides every row". So a profiled account
    `_record_unhideable` could not look through (no token, no usable collections read, a read that
    raised), an account whose filter write failed, and an account the owner left alone all read as
    hiding — a green "2 of 2" over a run whose own log says it "reports nothing rather than a false
    all-clear". Reporting only: none of these changes what is written or promoted.
    """

    OWNED: ClassVar[dict] = {"sarah": OwnedRow(label="Shortlist_sarah", rating_keys=[11])}

    @staticmethod
    def _report():
        return pipeline_mod.RunReport(started_at=datetime.now(UTC))

    def test_a_profiled_account_no_token_could_be_minted_for_is_recorded_as_unchecked(self, ctx: EngineContext):
        """The archetype: `home_user_server_token` refuses a PIN-protected Home user."""
        ctx.pms_for_user = lambda profile: None
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == ["kid"]
        assert report.unhideable_rows == {}

    def test_a_profiled_account_is_recorded_as_unchecked_when_the_collections_read_failed(self, ctx: EngineContext):
        ctx.pms_for_user = MagicMock()
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), {}, False, report
        )

        assert report.privacy_unchecked == ["kid"]
        ctx.pms_for_user.assert_not_called()

    def test_a_profiled_account_is_recorded_as_unchecked_when_the_collections_read_came_back_empty(
        self, ctx: EngineContext
    ):
        """An empty read cannot prove no row exists (plex-safety rule 4), so it vouches for nobody."""
        ctx.pms_for_user = MagicMock()
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), {}, True, report
        )

        assert report.privacy_unchecked == ["kid"]

    def test_a_profiled_account_is_recorded_as_unchecked_when_looking_through_it_raises(self, ctx: EngineContext):
        ctx.pms_for_user = MagicMock(side_effect=RuntimeError("PMS timed out"))
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == ["kid"]
        assert report.unhideable_rows == {}

    def test_a_profiled_account_is_recorded_as_unchecked_when_the_engine_has_no_per_account_client(
        self, ctx: EngineContext
    ):
        ctx.pms_for_user = None
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("little_kid"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == ["kid"]

    def test_a_profiled_account_that_was_looked_through_is_not_recorded_as_unchecked(
        self, ctx: EngineContext, monkeypatch
    ):
        ctx.pms_for_user = MagicMock()
        monkeypatch.setattr(pipeline_mod, "unhidden_rows_visible_to", lambda as_them, owned, slug: [])
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == []
        assert report.unhideable_rows == {}

    def test_a_profiled_account_seen_to_expose_rows_is_a_finding_not_unchecked(self, ctx: EngineContext, monkeypatch):
        ctx.pms_for_user = MagicMock()
        monkeypatch.setattr(pipeline_mod, "unhidden_rows_visible_to", lambda as_them, owned, slug: [11])
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("kid", account_id=500), _profiled_remote("older_kid"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == []
        assert report.unhideable_rows == {"kid": [11]}

    def test_an_account_with_no_profile_is_not_this_checks_business(self, ctx: EngineContext):
        """Its hide-list was written and read back by the loop itself; this check is for profiled ones."""
        ctx.pms_for_user = lambda profile: None
        report = self._report()

        pipeline_mod._record_unhideable(
            ctx, make_profile("sarah", account_id=100), plextv_user(100, "sarah"), self.OWNED, True, report
        )

        assert report.privacy_unchecked == []

    def test_the_run_records_a_profiled_account_it_could_not_look_through(self, ctx: EngineContext, mock_plextv):
        """End to end through the loop: the `little_kid` account is never written to, so the loop hands it to
        `_record_unhideable`, which cannot mint a token for it."""
        ctx.pms_for_user = lambda profile: None
        ctx.plex.owned_collections.return_value = dict(self.OWNED)
        mock_plextv.users = [plextv_user(100, "sarah"), _profiled_remote("little_kid")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("kid", account_id=500)])

        assert report.unhideable_measured is True
        assert report.privacy_unchecked == ["kid"]
        assert report.privacy_write_failed == []
        assert report.privacy_left_alone == []

    def test_a_refused_write_on_an_account_whose_profile_could_not_be_read_is_unchecked(
        self, ctx: EngineContext, mock_plextv
    ):
        """The profile-unknown arm skips the account as expected — no exclude written, nothing looked
        through, so the run cannot vouch for it either."""
        mock_plextv.users = [plextv_user(100, "sarah"), _profiled_remote("")]
        mock_plextv.home_profile_known.return_value = False

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("kid", account_id=500)])

        assert not report.promotion_blockers
        assert report.privacy_unchecked == ["kid"]

    def test_an_account_whose_filter_write_plex_refused_is_recorded(self, ctx: EngineContext, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah"), _profiled_remote("")]

        def refuse_the_kid(account_id, fields):
            if account_id == 500:
                raise FilterWriteRefused("plex.tv rejected the share-filter update for account 500: HTTP 422")

        mock_plextv.update_user_filters.side_effect = refuse_the_kid

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("kid", account_id=500)])

        assert report.promotion_blockers
        assert report.privacy_write_failed == ["kid"]
        assert report.privacy_unchecked == []

    def test_an_account_whose_filter_write_raised_is_recorded(self, ctx: EngineContext, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(300, "jess")]

        def fail_jess(account_id, fields):
            if account_id == 300:
                raise RuntimeError("connection reset")

        mock_plextv.update_user_filters.side_effect = fail_jess

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("jess", account_id=300)])

        assert report.promotion_blockers
        assert report.privacy_write_failed == ["jess"]

    def test_an_account_left_alone_is_recorded(self, ctx: EngineContext, mock_plextv):
        """`manage_sharing=0`: it keeps none of our excludes, so it sees every row — by the owner's choice."""
        ctx.unmanaged_account_ids = {300}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(300, "jess")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert report.privacy_left_alone == ["jess"]
        assert report.privacy_unchecked == []
        assert report.privacy_write_failed == []
