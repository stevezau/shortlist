from datetime import UTC, datetime
from typing import ClassVar

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import UserRunReport
from shortlist.server.db.models import Base
from shortlist.server.services.run_persistence import _cost_blob


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


class TestCostBlob:
    def test_seconds_become_integer_milliseconds(self):
        report = UserRunReport(username="alex", slug="alex")
        report.setup_s = 421.0
        report.row_timing = {"picked-for-you": {"duration_s": 12.04, "blocked_s": 0.31}}
        report.pool_costs = [
            {
                "label": "movie · tmdb, llm_web",
                "tokens": 15917,
                "exa_searches": 3,
                "duration_s": 398.0,
                "rows": ["picked-for-you"],
            }
        ]
        blob = _cost_blob(report)
        assert blob["setup_ms"] == 421000
        assert blob["rows"]["picked-for-you"] == {"duration_ms": 12040, "blocked_ms": 310}
        assert blob["pools"][0]["duration_ms"] == 398000
        assert blob["pools"][0]["tokens"] == 15917
        assert "duration_s" not in blob["pools"][0]

    def test_a_report_that_measured_nothing_persists_null(self):
        """Not `{}`: an empty blob would render as a real measurement of zero. A user who never
        reached the gather (no rows due) genuinely has nothing recorded."""
        assert _cost_blob(UserRunReport(username="alex", slug="alex")) is None


class TestARealFailureOutlivesAThresholdReason:
    """A queued title now always carries a reason, so the merge had to learn which one matters.

    Before: `row.detail = m.detail or row.detail` — so a title whose send genuinely errored
    ("Sonarr returned HTTP 503") had that overwritten the next night by "max_per_run (3) already
    filled", and the only record that Sonarr was broken was gone from the inbox and the trace.
    """

    def test_every_reason_the_engine_can_queue_is_classified_as_not_a_failure(self):
        """Drives the real engine through each blocking branch, so rewording a reason cannot silently
        reclassify it. Asserting substrings (the older tests do) would not catch that: "below
        auto_min_demand" still contains "auto_min_demand" while no longer matching the prefix."""
        from shortlist.engine.requests import QUEUE_REASON_PREFIXES
        from shortlist.server.services.run_persistence import _is_failure_detail

        for prefix in QUEUE_REASON_PREFIXES:
            assert _is_failure_detail(prefix) is False, prefix
            assert _is_failure_detail(f"{prefix} (3)") is False, prefix

    def test_a_threshold_reason_does_not_erase_a_recorded_failure(self):
        from shortlist.server.services.run_persistence import _is_failure_detail

        assert _is_failure_detail("Sonarr GET /lookup returned HTTP 503") is True
        assert _is_failure_detail("max_per_run (3) already filled") is False
        assert _is_failure_detail("rating below auto_min_rating (7.5)") is False
        assert _is_failure_detail("auto-send is off") is False
        assert _is_failure_detail("on an Arr exclusion list") is False
        assert _is_failure_detail("") is False
        assert _is_failure_detail(None) is False


class TestTheShelfEventsANightlyRunEmits:
    """`_emit_hub_ordering_events` — the RUN path, which `jobs._audit_hub_orderings` mirrors for the
    on-demand handlers.

    Tested separately from the jobs path because they are separate emitters with separate scope
    names, and `docs/guides.md` tells owners to read THIS one back after a nightly run
    (`/api/events/log?scope=run.hub_unplaced` — the change log has no screen yet). The jobs-path
    tests in `test_jobs.py` cannot see a regression here.
    """

    @staticmethod
    def _emit(entries: list[dict], *, dry_run: bool = False) -> list[tuple]:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        report = SimpleNamespace(hub_orderings=entries, dry_run=dry_run)
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_hub_ordering_events(None, 7, report)
        return seen

    def test_a_placement_that_could_not_be_applied_gets_its_own_scope_and_no_verified(self):
        """`verified` answers "we asked Plex and it stuck". Nothing was asked here, so answering it
        would be a fabrication — and the separate scope is what keeps `_shelf_contention`'s bounded
        window holding only the repeated moves it counts."""
        seen = self._emit([{"library": "Movies", "placed": False, "moved": [], "reason": "anchor not found"}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "warning")]
        fields = seen[0][2]
        assert fields["reason"] == "anchor not found" and fields["verified"] is None
        assert fields["library"] == "Movies" and fields["run_id"] == 7

    def test_a_move_still_uses_the_ordinary_scope(self):
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": True}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "info")]

    def test_an_unverified_move_is_a_warning(self):
        """A shelf we asked for and did not get — the SFLIX case the whole audit was rebuilt around."""
        seen = self._emit([{"library": "Movies", "moved": ["Picked for You"], "verified": False}])

        assert [(a[0], a[1]) for a in seen] == [("run.hub_order", "warning")]

    def test_a_dry_run_is_never_a_warning_on_either_scope(self):
        """A preview asked Plex for nothing, so neither kind is an alarm."""
        seen = self._emit(
            [
                {"library": "Movies", "placed": False, "moved": [], "reason": "anchor not found"},
                {"library": "TV", "moved": ["row"], "verified": False},
            ],
            dry_run=True,
        )

        assert [(a[0], a[1]) for a in seen] == [("run.hub_unplaced", "info"), ("run.hub_order", "info")]


class TestARunAuditsEveryShareFilterWrite:
    """`run.privacy_sync` from the RUN path — the record the runless jobs now share (`test_jobs.py`
    `TestRunlessPrivacyPassesAuditFilterWrites`). Pinned so sharing the emitter cannot change what a run
    has always written: rows already in the table and in support bundles read this exact shape."""

    def test_a_persisted_run_records_each_accounts_before_and_after(self, sessions):
        from shortlist.engine.models import RunReport
        from shortlist.server.db.models import Event, Run
        from shortlist.server.services.run_persistence import persist_report

        with sessions() as session:
            run = Run(trigger="manual", status="running", dry_run=False, stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        report = RunReport(started_at=datetime.now(UTC), dry_run=False)
        report.filter_writes = {
            300: {"username": "dave", "fields": {"filterMovies": ("", "label!=shortlist_sarah")}, "at": 12.5}
        }

        persist_report(sessions, run_id, report)

        with sessions() as session:
            events = session.query(Event).filter_by(scope="run.privacy_sync").all()
        assert [(e.level, e.message) for e in events] == [
            (
                "info",
                {
                    "run_id": run_id,
                    "dry_run": False,
                    "plex_account_id": 300,
                    "username": "dave",
                    "fields": {"filterMovies": {"before": "", "after": "label!=shortlist_sarah"}},
                },
            )
        ]
        assert list(events[0].message) == ["run_id", "dry_run", "plex_account_id", "username", "fields"]


SWEEP_REASON = (
    "row was broken beyond repair-in-place — no share filter could hide it (wrong type for its library, or no "
    "shortlist label at all — an orphan from an interrupted run), or it shared a collection tag with other users' "
    "rows and held their picks. Keys starting 'freed-name helper:' are not rows: a helper a stopped run left "
    "behind while freeing a row's name"
)


class TestARunAuditsItsSweep:
    """`run.sweep` from the RUN path — the record the runless jobs now share (`test_jobs.py`
    `TestRunlessPrivacyPassesAuditTheirSweep`). Pinned so sharing the emitter cannot change what a run has
    always written: rows already in the table and in support bundles read this exact shape."""

    SWEPT: ClassVar[dict[str, list[str]]] = {
        "sarah": ["Picked for Sarah"],
        "freed-name helper:mike": ["Picked for Mike ~freeing"],
    }

    def _persist(self, sessions, *, dry_run: bool, swept_rows: dict) -> tuple[int, list]:
        from shortlist.engine.models import RunReport
        from shortlist.server.db.models import Event, Run
        from shortlist.server.services.run_persistence import persist_report

        with sessions() as session:
            run = Run(trigger="manual", status="running", dry_run=dry_run, stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        report = RunReport(started_at=datetime.now(UTC), dry_run=dry_run)
        report.swept_rows = swept_rows

        persist_report(sessions, run_id, report)

        with sessions() as session:
            return run_id, session.query(Event).filter_by(scope="run.sweep").all()

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_persisted_run_records_every_row_and_helper_it_deleted(self, sessions, dry_run):
        run_id, events = self._persist(sessions, dry_run=dry_run, swept_rows=dict(self.SWEPT))

        assert [(e.level, e.message) for e in events] == [
            (
                "warning",
                {"run_id": run_id, "dry_run": dry_run, "reason": SWEEP_REASON, "deleted": self.SWEPT},
            )
        ]
        assert list(events[0].message) == ["run_id", "dry_run", "reason", "deleted"]

    def test_a_run_that_swept_nothing_adds_no_event(self, sessions):
        _, events = self._persist(sessions, dry_run=False, swept_rows={})

        assert events == []


def _persist_converge(sessions, *, dry_run: bool, scope: str, **lists) -> tuple[int, list]:
    """Persist a run whose converge phase recorded `lists`, and read back every event of `scope`."""
    from shortlist.engine.models import RunReport
    from shortlist.server.db.models import Event, Run
    from shortlist.server.services.run_persistence import persist_report

    with sessions() as session:
        run = Run(trigger="manual", status="running", dry_run=dry_run, stats={})
        session.add(run)
        session.commit()
        run_id = run.id
    report = RunReport(started_at=datetime.now(UTC), dry_run=dry_run, **lists)

    persist_report(sessions, run_id, report)

    with sessions() as session:
        return run_id, session.query(Event).filter_by(scope=scope).all()


class TestARunAuditsItsConvergeDemotions:
    """`run.demote` — converge takes rows off Home (a paused person, a shared row switched off, an unknown owner
    it may not delete, a row on the owner's Home that should not be). It wrote only a sorted list of labels to
    the run log, so nothing said which collection, in which library, or why (plex-safety rule 10)."""

    DEMOTED: ClassVar[list[dict]] = [
        {
            "label": "Shortlist_sarah",
            "title": "Picked for Sarah",
            "rating_key": 4101,
            "library_key": "1",
            "library": "Movies",
            "reason": "paused",
        },
        {
            "label": "Shortlist_mike",
            "title": "Picked for Mike",
            "rating_key": 4102,
            "library_key": "2",
            "library": "TV Shows",
            "reason": "on_owner_home",
        },
    ]

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_persisted_run_records_every_row_it_took_off_home(self, sessions, dry_run):
        """Info, not warning: each is the intended effect of a setting (a pause, a switched-off row) or a
        correction that only ever hides more — nothing the owner has to act on."""
        demoted = [dict(entry) for entry in self.DEMOTED]
        run_id, events = _persist_converge(sessions, dry_run=dry_run, scope="run.demote", converge_demotions=demoted)

        assert [(e.level, e.message) for e in events] == [
            ("info", {"run_id": run_id, "dry_run": dry_run, "demoted": self.DEMOTED})
        ]
        assert list(events[0].message) == ["run_id", "dry_run", "demoted"]

    def test_a_run_that_demoted_nothing_adds_no_event(self, sessions):
        _, events = _persist_converge(sessions, dry_run=False, scope="run.demote", converge_demotions=[])

        assert events == []


ORPHAN_REASON = (
    "its label names no one Shortlist knows — the person was removed from the server or from Shortlist — and the "
    "roster read was complete, so the collection was deleted rather than hidden"
)


class TestARunAuditsItsOrphanDeletes:
    """`run.orphan_delete` — converge DELETES a collection whose label names nobody on the roster, the one
    irreversible thing it does. It recorded only `orphans_removed`, a list of labels no event carried
    (plex-safety rule 10)."""

    DELETED: ClassVar[list[dict]] = [
        {
            "label": "Shortlist_ghost",
            "title": "Picked for Ghost",
            "rating_key": 4103,
            "library_key": "1",
            "library": "Movies",
        }
    ]

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_persisted_run_records_every_collection_it_deleted(self, sessions, dry_run):
        deleted = [dict(entry) for entry in self.DELETED]
        run_id, events = _persist_converge(
            sessions, dry_run=dry_run, scope="run.orphan_delete", orphan_deletions=deleted
        )

        assert [(e.level, e.message) for e in events] == [
            ("warning", {"run_id": run_id, "dry_run": dry_run, "reason": ORPHAN_REASON, "deleted": self.DELETED})
        ]
        assert list(events[0].message) == ["run_id", "dry_run", "reason", "deleted"]

    def test_a_run_that_deleted_nothing_adds_no_event(self, sessions):
        _, events = _persist_converge(sessions, dry_run=False, scope="run.orphan_delete", orphan_deletions=[])

        assert events == []

    def test_a_demotion_is_never_filed_as_a_delete_or_the_reverse(self, sessions):
        """Two scopes so "what was destroyed at 03:31" is answerable on its own, as `orphans_removed` promised."""
        demoted = [dict(entry) for entry in TestARunAuditsItsConvergeDemotions.DEMOTED]
        _, deletes = _persist_converge(sessions, dry_run=False, scope="run.orphan_delete", converge_demotions=demoted)

        assert deletes == []


class TestTheZeroRequestedEventSaysWhetherItWasReachable:
    """`_emit_request_events` — "0 requested" has two shapes and only one is about the owner's
    settings. `min_demand` counts DISTINCT wanters, so a run covering fewer people than the floor
    could never have filled the pool, whatever the settings were. Six such events on the
    maintainer's server (2026-09-03, every one a one-user manual run) raised "Nothing is being
    requested — loosen your floors" while the nightly 46-user run was requesting normally."""

    @staticmethod
    def _emit(*, users: int, demand_floor: int) -> dict:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.engine.models import RequestReport
        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        requests = RequestReport(wanted=650, pool_size=0, demand_floor=demand_floor)
        report = SimpleNamespace(
            requests=requests,
            dry_run=False,
            users=[UserRunReport(username=f"u{i}", slug=f"u{i}") for i in range(users)],
        )
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_request_events(None, 7, report)
        return next(f | {"_level": level} for scope, level, f in seen if scope == "requests.none_qualified")

    def test_a_run_smaller_than_its_own_demand_floor_is_info_and_flagged(self):
        fields = self._emit(users=1, demand_floor=2)

        assert fields["_level"] == "info", "arithmetically guaranteed, so not an alarm"
        assert fields["demand_unreachable"] is True
        assert (fields["users"], fields["demand_floor"]) == (1, 2)

    def test_a_full_roster_that_cleared_nothing_is_still_a_warning(self):
        """The shape the alert exists for: plenty of people, plenty missing, floors too tight."""
        fields = self._emit(users=46, demand_floor=2)

        assert fields["_level"] == "warning"
        assert fields["demand_unreachable"] is False

    def test_a_floor_of_one_is_never_unreachable(self):
        """The default. One person wanting a title is one wanter, so a single-user run clears it."""
        assert self._emit(users=1, demand_floor=1)["demand_unreachable"] is False


class TestTheRequestsEventIsWrittenWheneverThePassDidAnything:
    """`run.requests` used to be written only when something was auto-SENT, and `outcomes` holds only
    sends — so a night that queued 51 titles for the owner's approval left no audit event at all."""

    @staticmethod
    def _emit(requests) -> list[tuple[str, dict]]:
        from types import SimpleNamespace
        from unittest.mock import patch

        from shortlist.server.services import run_persistence as rp

        seen: list[tuple] = []
        report = SimpleNamespace(requests=requests, dry_run=False, users=[])
        with patch.object(rp, "add_audit", lambda session, scope, level, **f: seen.append((scope, level, f))):
            rp._emit_request_events(None, 7, report)
        return [(level, fields) for scope, level, fields in seen if scope == "run.requests"]

    @staticmethod
    def _title(tmdb_id: int):
        from shortlist.engine.models import MediaType, MissingTitle

        return MissingTitle(
            tmdb_id=tmdb_id, title=f"T{tmdb_id}", media_type=MediaType.MOVIE, year=2020, rating=7.5, vote_count=900
        )

    def test_a_pass_that_only_queued_still_leaves_the_event_with_its_counts(self):
        from shortlist.engine.models import RequestReport

        requests = RequestReport(considered=5, wanted=40, pool_size=12, queued=[self._title(i) for i in range(3)])

        events = self._emit(requests)

        assert len(events) == 1, "a queued-only night left no audit event"
        level, fields = events[0]
        assert level == "info"
        assert (fields["considered"], fields["queued"], fields["sent"]) == (5, 3, 0)
        assert fields["outcomes"] == []
        assert fields["run_id"] == 7

    def test_a_pass_that_sent_carries_the_sent_count_beside_its_outcomes(self):
        from shortlist.engine.models import MediaType, RequestOutcome, RequestReport

        sent = self._title(1)
        requests = RequestReport(
            considered=2,
            wanted=10,
            pool_size=4,
            sent=[sent],
            queued=[self._title(2)],
            outcomes=[RequestOutcome(tmdb_id=1, title="T1", media_type=MediaType.MOVIE, status="requested")],
        )

        (_, fields), *rest = self._emit(requests)

        assert rest == []
        assert (fields["considered"], fields["queued"], fields["sent"]) == (2, 1, 1)
        assert [o["tmdb_id"] for o in fields["outcomes"]] == [1]

    def test_a_pass_that_neither_queued_nor_sent_writes_no_event(self):
        from shortlist.engine.models import RequestReport

        assert self._emit(RequestReport(wanted=40, pool_size=12, considered=0)) == []
        assert self._emit(None) == []


class TestPicksCarryTheBuiltAtStamp:
    """`built_at` has to survive the write as well as the read.

    The read back has a test (`test_previous_picks_carries_the_built_at_stamp`), but nothing
    exercised the WRITE: drop `built_at=pick.built_at` from `_persist_user_report` and every stamp is
    silently NULL, every carried row reads as "unknown", and the idle hold is inert on a real server
    with the whole suite green.
    """

    def test_a_persisted_pick_keeps_the_stamp_the_engine_put_on_it(self, sessions):
        from shortlist.engine.models import MediaType, Pick, UserRunReport
        from shortlist.server.db.models import PickRow, Run, User
        from shortlist.server.services.run_persistence import _persist_user_report

        built = datetime(2026, 8, 20, 3, 30, tzinfo=UTC)
        with sessions() as session:
            user = User(plex_account_id=1, username="sarah", slug="sarah", enabled=True)
            run = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add_all([user, run])
            session.commit()
            report = UserRunReport(username="sarah", slug="sarah", status="ok")
            report.picks = [
                Pick(
                    tmdb_id=100,
                    rating_key=1,
                    title="T100",
                    rank=1,
                    reason="",
                    media_type=MediaType.MOVIE,
                    collection_slug="picked",
                    section_key="1",
                    built_at=built,
                )
            ]

            _persist_user_report(session, run.id, user, report, dry_run=False)
            session.commit()

            stored = session.query(PickRow).one()
            assert stored.built_at is not None, "the stamp was dropped on the way into the database"
            assert stored.built_at.replace(tzinfo=stored.built_at.tzinfo or UTC) == built


class TestPicksCarryTheLeadSeed:
    """The watch a `{top_seed}` row was built from has to survive the write (issue #133). Dropped here, every
    carried row reads as "unknown", and a row named without a seeded pick never notices its watch moving on:
    it carries two-thirds of the old watch's row forward under the new watch's name."""

    def test_a_persisted_pick_keeps_the_watch_its_row_was_built_from(self, sessions):
        from shortlist.engine.models import MediaType, Pick, UserRunReport
        from shortlist.server.db.models import PickRow, Run, User
        from shortlist.server.services.run_persistence import _persist_user_report

        with sessions() as session:
            user = User(plex_account_id=1, username="sarah", slug="sarah", enabled=True)
            run = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add_all([user, run])
            session.commit()
            report = UserRunReport(username="sarah", slug="sarah", status="ok")
            report.picks = [
                Pick(
                    tmdb_id=100,
                    rating_key=1,
                    title="T100",
                    rank=1,
                    reason="",
                    media_type=MediaType.MOVIE,
                    collection_slug="because",
                    section_key="1",
                    lead_seed_tmdb_id=900,
                    lead_seed_title="Fargo",
                )
            ]

            _persist_user_report(session, run.id, user, report, dry_run=False)
            session.commit()

            stored = session.query(PickRow).one()
            assert (stored.lead_seed_tmdb_id, stored.lead_seed_title) == (900, "Fargo")


class TestTheLedgerRecordsWhatWasWrittenToASummaryAndSortTitle:
    """Issue #120. The ledger's record is what lets clearing a row's field hand back ONLY what Shortlist
    wrote — so the persist must forget a record the run cleared, and must keep one a run never reached."""

    def _entry(self, **details) -> dict:
        return {"row_slug": "gems", "library_key": "1", "rating_key": 42, "row_title": "Gems", **details}

    def test_a_record_the_run_wrote_is_stored_and_one_it_cleared_is_forgotten(self, sessions):
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(summary_written="Hi", title_sort_written="!1_Gems")])
            row = session.get(Delivery, ("gems", "sarah", "1"))
            assert (row.summary_written, row.title_sort_written) == ("Hi", "!1_Gems")

            _record_deliveries(session, "sarah", [self._entry(summary_written=None, title_sort_written=None)])
            assert (row.summary_written, row.title_sort_written) == (None, None)

    def test_an_entry_without_the_keys_keeps_the_record(self, sessions):
        """A legacy breakdown, or a library delivery never reached the description step for, says
        nothing about what Plex holds — forgetting would strand a value Shortlist really wrote."""
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(summary_written="Hi", title_sort_written=None)])
            _record_deliveries(session, "sarah", [self._entry()])
            assert session.get(Delivery, ("gems", "sarah", "1")).summary_written == "Hi"


class TestTheLedgerRecordsTheSeasonACollectionWasBuiltFor:
    """#137 C-1: promotion keeps a seasonal collection built for another season hidden, and reads which season
    from here. "" (not seasonal) is a record too; a breakdown without the key says nothing."""

    def _entry(self, **season) -> dict:
        return {"row_slug": "seasonal", "library_key": "1", "rating_key": 42, "row_title": "Picks", **season}

    def test_each_delivery_records_its_season_and_a_plain_build_records_none(self, sessions):
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(season="christmas@2026-12-25")])
            row = session.get(Delivery, ("seasonal", "sarah", "1"))
            assert row.season == "christmas@2026-12-25"

            _record_deliveries(session, "sarah", [self._entry(season="")])
            assert row.season == ""

    def test_an_entry_without_the_key_keeps_the_record(self, sessions):
        from shortlist.server.db.models import Delivery
        from shortlist.server.services.run_persistence import _record_deliveries

        with sessions() as session:
            _record_deliveries(session, "sarah", [self._entry(season="pat@2027-03-17")])
            _record_deliveries(session, "sarah", [self._entry()])
            assert session.get(Delivery, ("seasonal", "sarah", "1")).season == "pat@2027-03-17"


class TestExclusionsSkippedAreAudited:
    """A row that set its no-repeat / keep-out rules aside for someone (#138) says so in the change log."""

    def _events(self, sessions, *, skipped: list[str], dry_run: bool = False) -> tuple[int, list]:
        from shortlist.engine.models import RunReport
        from shortlist.server.db.models import Event, Run, User
        from shortlist.server.services.run_persistence import persist_report

        with sessions() as session:
            session.add(User(plex_account_id=1, username="ann", slug="ann", enabled=True))
            run = Run(trigger="manual", status="running", dry_run=dry_run, stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        report = RunReport(started_at=datetime.now(UTC), dry_run=dry_run)
        report.users.append(UserRunReport(username="ann", slug="ann", exclusions_skipped=skipped))

        persist_report(sessions, run_id, report)

        with sessions() as session:
            return run_id, session.query(Event).filter_by(scope="row.exclusions_skipped").all()

    @pytest.mark.parametrize("dry_run", [False, True])
    def test_one_event_per_person_and_row_with_the_run_and_dry_run_flag(self, sessions, dry_run):
        run_id, events = self._events(sessions, skipped=["quiet-nights", "loud-nights"], dry_run=dry_run)

        assert [
            (e.level, e.message["run_id"], e.message["dry_run"], e.message["user"], e.message["row"]) for e in events
        ] == [
            ("info", run_id, dry_run, "ann", "quiet-nights"),
            ("info", run_id, dry_run, "ann", "loud-nights"),
        ]

    def test_nothing_is_written_when_no_row_skipped_its_rules(self, sessions):
        _, events = self._events(sessions, skipped=[])

        assert events == []
