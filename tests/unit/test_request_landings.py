"""Early requests-row updates (#191): what a landed request owes, and when the poll may start a run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest

from shortlist.engine.models import MediaType, RequestSources, SeerrTarget
from shortlist.server.db.models import Collection, CollectionAudience, CollectionUserOverride, Event, Run, RunUser, User
from shortlist.server.services import request_landings
from shortlist.server.services.request_landings import (
    MAX_EARLY_RUNS,
    Owed,
    landings,
    should_start,
    still_owed,
    tick,
)
from tests.conftest import make_profile

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GAP = timedelta(minutes=60)
SOURCES = RequestSources(overseerr=SeerrTarget(url="http://seerr", api_key="k"))


@pytest.fixture
def sessions(threaded_sessions):
    return threaded_sessions


def seed(sessions, *, people: int = 1, **row) -> tuple[int, list[int]]:
    """An enabled requests row and ``people`` enabled users (plex id 101..). Returns (row id, user ids)."""
    with sessions() as s:
        fields = {
            "slug": "reqs",
            "name": "Your requests",
            "requests_row": True,
            "requests_early_refresh": True,
            "enabled": True,
            "media": "both",
            **row,
        }
        collection = Collection(**fields)
        users = [
            User(plex_account_id=101 + i, username=f"u{i}", slug=f"u{i}", enabled=True, user_type="shared")
            for i in range(people)
        ]
        s.add_all([collection, *users])
        s.commit()
        return collection.id, [u.id for u in users]


def profiles(n: int = 1):
    return [make_profile(username=f"u{i}", account_id=101 + i, slug=f"u{i}") for i in range(n)]


def request(rid: int, seerr_user: int, *, tmdb: int = 603, kind: str = "movie", ago: timedelta = timedelta(minutes=20)):
    return {
        "id": rid,
        "status": 5,
        "type": kind,
        "updatedAt": (NOW - ago).isoformat(),
        "requestedBy": {"id": seerr_user, "plexId": None},
        "media": {"tmdbId": tmdb, "status": 5},
    }


def owed(
    completed_ago: timedelta,
    *,
    row: int = 1,
    user: int = 1,
    tmdb: int = 603,
    rid: int = 1,
    media_type: MediaType = MediaType.MOVIE,
) -> Owed:
    return Owed(
        request_id=rid,
        tmdb_id=tmdb,
        media_type=media_type,
        completed_at=NOW - completed_ago,
        collection_id=row,
        user_id=user,
    )


def add_run(
    sessions,
    *,
    started: datetime,
    trigger: str = "request_landed",
    status: str = "ok",
    rows: tuple[str, ...] = ("reqs",),
    users: tuple[str, ...] = ("u0",),
    verdicts: dict[str, dict[int, str]] | None = None,
    dry_run: bool = False,
) -> int:
    """A run covering the given row/user slugs; ``verdicts`` = {user slug: {tmdb id: result}} become traces."""
    with sessions() as s:
        run = Run(
            trigger=trigger,
            started_at=started,
            status=status,
            dry_run=dry_run,
            stats={"expected_users": [{"slug": u} for u in users], "expected_rows": [{"slug": r} for r in rows]},
        )
        s.add(run)
        s.flush()
        for slug, results in (verdicts or {}).items():
            uid = s.query(User).filter_by(slug=slug).one().id
            trace = {
                "selection": [
                    {
                        "row": rows[0],
                        "decision": "requests",
                        "requests": [{"tmdb_id": t, "media_type": "movie", "result": r} for t, r in results.items()],
                    }
                ]
            }
            s.add(RunUser(run_id=run.id, user_id=uid, status="ok", trace=trace))
        s.commit()
        return run.id


class TestLandings:
    def test_landings_match_by_plex_id_and_chosen_account(self, sessions):
        row, (u0, u1) = seed(sessions, people=2)
        people = profiles(2)
        people[1].seerr_user_id = 9  # chosen: Overseerr user 9 is person 1, whatever plexId says
        db_ids = {101: u0, 102: u1}
        done = [request(1, 7), request(2, 9, tmdb=11)]
        read = {7: 101, 9: 555}
        with sessions() as s:
            got = landings(s, done, SOURCES, people, db_ids, read)
        assert {(o.request_id, o.tmdb_id, o.collection_id, o.user_id) for o in got} == {
            (1, 603, row, u0),
            (2, 11, row, u1),
        }
        assert got[0].completed_at == NOW - timedelta(minutes=20)

    def test_landings_skip_request_as_ambiguous_and_unmatched(self, sessions):
        seed(sessions, people=2)
        people = profiles(2)
        people[0].seerr_user_id = 9
        people[1].seerr_user_id = 9  # two people claim one account: it maps to nobody
        done = [request(1, 9), request(2, 33)]
        with sessions() as s:
            assert landings(s, done, SOURCES, people, {101: 1, 102: 2}, {9: 101, 33: None}) == []

    def test_ineligible_rows_or_people_excluded(self, sessions):
        _, (u0, u1, u2, u3) = seed(sessions, people=4, audience="subset")
        with sessions() as s:
            row = s.query(Collection).one()
            s.add_all([CollectionAudience(collection_id=row.id, user_id=u) for u in (u0, u1, u2)])
            s.add(CollectionUserOverride(collection_id=row.id, user_id=u1, muted=True))
            s.commit()
        people = profiles(4)
        db_ids = {101: u0, 102: u1, 103: u2, 104: u3}
        read = {1: 101, 2: 102, 3: 103, 4: 104}
        done = [request(i, i) for i in (1, 2, 3, 4)]
        with sessions() as s:
            # u1 muted, u3 outside the subset
            assert {o.user_id for o in landings(s, done, SOURCES, people, db_ids, read)} == {u0, u2}
            # a show request on a movie-only row
            s.query(Collection).update({"media": "movie"})
            s.commit()
            assert landings(s, [request(5, 1, kind="tv")], SOURCES, people, db_ids, read) == []
            assert len(landings(s, [request(5, 1)], SOURCES, people, db_ids, read)) == 1
            s.query(Collection).update({"requests_early_refresh": False})
            s.commit()
            assert landings(s, done, SOURCES, people, db_ids, read) == []
            s.query(Collection).update({"requests_early_refresh": True, "enabled": False})
            s.commit()
            assert landings(s, done, SOURCES, people, db_ids, read) == []
            s.query(Collection).update({"enabled": True, "requests_row": False})
            s.commit()
            assert landings(s, done, SOURCES, people, db_ids, read) == []


class TestShouldStart:
    def test_settle_window_holds_then_releases(self, sessions):
        with sessions() as s:
            assert should_start(s, [owed(timedelta(minutes=4))], NOW, GAP) is False
            assert should_start(s, [owed(timedelta(minutes=11))], NOW, GAP) is True

    def test_max_wait_beats_a_steady_trickle(self, sessions):
        with sessions() as s:
            trickle = [owed(timedelta(minutes=31), rid=1), owed(timedelta(minutes=1), rid=2)]
            assert should_start(s, trickle, NOW, GAP) is True

    def test_empty_owed_never_starts(self, sessions):
        with sessions() as s:
            assert should_start(s, [], NOW, GAP) is False

    def test_min_gap(self, sessions):
        add_run(sessions, started=NOW - timedelta(minutes=30))
        with sessions() as s:
            assert should_start(s, [owed(timedelta(minutes=11))], NOW, GAP) is False
        with sessions() as s:
            s.query(Run).update({"started_at": NOW - timedelta(minutes=61)})
            s.commit()
            assert should_start(s, [owed(timedelta(minutes=11))], NOW, GAP) is True

    @pytest.mark.parametrize("status", ["queued", "running"])
    @pytest.mark.parametrize("trigger", ["schedule", "manual", "request_landed"])
    def test_never_stacks(self, sessions, status, trigger):
        add_run(sessions, started=NOW - timedelta(hours=5), trigger=trigger, status=status)
        with sessions() as s:
            assert should_start(s, [owed(timedelta(minutes=11))], NOW, GAP) is False

    def test_schedule_close_defers(self, sessions):
        row, _ = seed(sessions)
        soon = (NOW + timedelta(minutes=20)).astimezone()
        with sessions() as s:
            s.query(Collection).update({"schedule": f"{soon.minute} {soon.hour} * * *"})
            s.commit()
            assert should_start(s, [owed(timedelta(minutes=11), row=row)], NOW, GAP) is False
            later = (NOW + timedelta(minutes=120)).astimezone()
            s.query(Collection).update({"schedule": f"{later.minute} {later.hour} * * *"})
            s.commit()
            assert should_start(s, [owed(timedelta(minutes=11), row=row)], NOW, GAP) is True
            s.query(Collection).update({"schedule": "not a cron"})
            s.commit()
            assert should_start(s, [owed(timedelta(minutes=11), row=row)], NOW, GAP) is True


class TestStillOwed:
    def test_left_out_title_settles(self, sessions):
        row, (u0,) = seed(sessions)
        for verdict in ("watched", "hidden", "over_size", "in_row", "too_old"):
            with sessions() as s:
                s.query(Run).delete()
                s.query(RunUser).delete()
                s.commit()
            add_run(sessions, started=NOW - timedelta(minutes=5), verdicts={"u0": {603: verdict}})
            with sessions() as s:
                assert still_owed(s, [owed(timedelta(minutes=20), row=row, user=u0)]) == [], verdict

    def test_no_trace_entry_for_the_title_settles(self, sessions):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), verdicts={"u0": {999: "in_row"}})
        with sessions() as s:
            assert still_owed(s, [owed(timedelta(minutes=20), row=row, user=u0)]) == []

    def test_a_movie_and_a_show_sharing_a_tmdb_id_are_judged_apart(self, sessions):
        row, (u0,) = seed(sessions)
        run_id = add_run(sessions, started=NOW - timedelta(minutes=5), verdicts={"u0": {603: "in_row"}})
        with sessions() as s:
            trace = s.get(RunUser, (run_id, u0)).trace
            trace["selection"][0]["requests"] = [
                {"tmdb_id": 603, "media_type": "movie", "result": "in_row"},
                {"tmdb_id": 603, "media_type": "show", "result": "not_on_plex"},
            ]
            s.get(RunUser, (run_id, u0)).trace = trace
            s.commit()
        movie = owed(timedelta(minutes=20), row=row, user=u0, media_type=MediaType.MOVIE)
        show = owed(timedelta(minutes=20), row=row, user=u0, media_type=MediaType.SHOW)
        with sessions() as s:
            assert still_owed(s, [movie, show]) == [show]

    def test_not_on_plex_retries_then_stops(self, sessions):
        row, (u0,) = seed(sessions)
        item = owed(timedelta(hours=5), row=row, user=u0)
        for n in range(MAX_EARLY_RUNS):
            with sessions() as s:
                assert still_owed(s, [item]) == [item], n
            add_run(sessions, started=NOW - timedelta(hours=4, minutes=-n * 30), verdicts={"u0": {603: "not_on_plex"}})
        with sessions() as s:
            assert still_owed(s, [item]) == []

    def test_season_not_landed_is_still_owed(self, sessions):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), verdicts={"u0": {603: "season_not_landed"}})
        item = owed(timedelta(minutes=20), row=row, user=u0)
        with sessions() as s:
            assert still_owed(s, [item]) == [item]

    @pytest.mark.parametrize("status", ["error", "aborted"])
    def test_failed_early_run_settles(self, sessions, status):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), status=status)
        with sessions() as s:
            assert still_owed(s, [owed(timedelta(minutes=20), row=row, user=u0)]) == []

    def test_failed_nightly_does_not_settle(self, sessions):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), trigger="schedule", status="error")
        item = owed(timedelta(minutes=20), row=row, user=u0)
        with sessions() as s:
            assert still_owed(s, [item]) == [item]

    def test_covered_by_nightly_settles(self, sessions):
        row, (u0, _u1) = seed(sessions, people=2)
        add_run(
            sessions,
            started=NOW - timedelta(minutes=5),
            trigger="schedule",
            users=("u0", "u1"),
            verdicts={"u0": {603: "in_row"}},
        )
        with sessions() as s:
            assert still_owed(s, [owed(timedelta(minutes=20), row=row, user=u0)]) == []

    def test_run_before_completion_does_not_cover(self, sessions):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=30), verdicts={"u0": {603: "in_row"}})
        item = owed(timedelta(minutes=20), row=row, user=u0)
        with sessions() as s:
            assert still_owed(s, [item]) == [item]

    def test_run_for_other_person_or_row_does_not_cover(self, sessions):
        row, (u0, _u1) = seed(sessions, people=2)
        add_run(sessions, started=NOW - timedelta(minutes=5), users=("u1",), verdicts={"u1": {603: "in_row"}})
        add_run(sessions, started=NOW - timedelta(minutes=5), rows=("other",), verdicts={"u0": {603: "in_row"}})
        item = owed(timedelta(minutes=20), row=row, user=u0)
        with sessions() as s:
            assert still_owed(s, [item]) == [item]

    @pytest.mark.parametrize("status", ["queued", "running"])
    def test_in_flight_covering_run_settles(self, sessions, status):
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), status=status)
        with sessions() as s:
            assert still_owed(s, [owed(timedelta(minutes=20), row=row, user=u0)]) == []

    def test_dry_run_does_not_cover_in_live_mode(self, sessions, monkeypatch):
        monkeypatch.setattr(request_landings, "force_dry_run", lambda: False)
        row, (u0,) = seed(sessions)
        add_run(sessions, started=NOW - timedelta(minutes=5), dry_run=True, verdicts={"u0": {603: "in_row"}})
        item = owed(timedelta(minutes=20), row=row, user=u0)
        with sessions() as s:
            assert still_owed(s, [item]) == [item]

    def test_safe_mode_dry_runs_settle_and_cap(self, sessions, monkeypatch):
        monkeypatch.setattr(request_landings, "force_dry_run", lambda: True)
        row, (u0,) = seed(sessions)
        item = owed(timedelta(hours=5), row=row, user=u0)
        for n in range(MAX_EARLY_RUNS):
            with sessions() as s:
                assert still_owed(s, [item]) == [item], n
            add_run(
                sessions,
                started=NOW - timedelta(hours=4, minutes=-n * 30),
                dry_run=True,
                verdicts={"u0": {603: "not_on_plex"}},
            )
        with sessions() as s:
            assert still_owed(s, [item]) == []
        # a real run is not what this instance is making, so it covers nothing
        with sessions() as s:
            s.query(Run).delete()
            s.query(RunUser).delete()
            s.commit()
        add_run(sessions, started=NOW - timedelta(minutes=5), verdicts={"u0": {603: "watched"}})
        with sessions() as s:
            assert still_owed(s, [item]) == [item]
        add_run(sessions, started=NOW - timedelta(minutes=4), dry_run=True, verdicts={"u0": {603: "watched"}})
        with sessions() as s:
            assert still_owed(s, [item]) == []


class FakeSeerr:
    calls = 0
    fail = False
    completed: ClassVar[list[dict]] = []

    def __init__(self, target) -> None:
        pass

    def recently_completed(self, since):
        type(self).calls += 1
        if type(self).fail:
            raise RuntimeError("seerr down")
        return type(self).completed

    def user_plex_ids(self):
        return {i: 100 + i for i in range(1, 100)}


class FakeRunService:
    def __init__(self, people, db_ids, sources=SOURCES) -> None:
        self.people, self.db_ids, self.sources = people, db_ids, sources
        self.started: list[dict] = []

    def build_request_sources_only(self):
        return self.sources, self.people, self.db_ids

    async def start_run(self, **kwargs) -> int:
        self.started.append(kwargs)
        return 77


@pytest.fixture
def seerr(monkeypatch):
    FakeSeerr.calls, FakeSeerr.fail, FakeSeerr.completed = 0, False, []
    monkeypatch.setattr(request_landings, "SeerrClient", FakeSeerr)
    monkeypatch.setattr(request_landings, "_now", lambda: NOW)
    return FakeSeerr


def make_app(sessions, service):
    return SimpleNamespace(state=SimpleNamespace(sessions=sessions, secrets=None, run_service=service))


class TestTick:
    def test_burst_is_one_run(self, sessions, seerr):
        row, ids = seed(sessions, people=40)
        seerr.completed = [request(i, i, ago=timedelta(minutes=15)) for i in range(1, 41)]
        service = FakeRunService(profiles(40), {101 + i: uid for i, uid in enumerate(ids)})
        asyncio.run(tick(make_app(sessions, service)))
        assert len(service.started) == 1
        call = service.started[0]
        assert call == {
            "trigger": "request_landed",
            "dry_run": False,
            "user_ids": sorted(ids),
            "collection_ids": [row],
        }
        with sessions() as s:
            event = s.query(Event).filter_by(scope="requests.landed").one()
            assert event.message["run_id"] == 77
            assert event.message["people"] == 40
            assert event.message["requests"] == 40

    def test_idle_tick_writes_nothing(self, sessions, seerr):
        seed(sessions)
        service = FakeRunService(profiles(), {101: 1})
        asyncio.run(tick(make_app(sessions, service)))
        assert service.started == []
        with sessions() as s:
            assert s.query(Event).count() == 0

    def test_seerr_failure_starts_nothing(self, sessions, seerr):
        _, ids = seed(sessions)
        seerr.fail = True
        service = FakeRunService(profiles(), {101: ids[0]})
        asyncio.run(tick(make_app(sessions, service)))
        assert service.started == []

    def test_tick_noop_when_switch_off(self, sessions, seerr):
        seed(sessions, requests_early_refresh=False)
        service = FakeRunService(profiles(), {101: 1})
        asyncio.run(tick(make_app(sessions, service)))
        assert seerr.calls == 0
        assert service.started == []

    def test_tick_noop_without_overseerr(self, sessions, seerr):
        seed(sessions)
        service = FakeRunService(profiles(), {101: 1}, sources=RequestSources())
        asyncio.run(tick(make_app(sessions, service)))
        assert seerr.calls == 0
