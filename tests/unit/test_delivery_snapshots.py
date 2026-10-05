"""Delivered rows retain identity and at-play membership independently of run logs."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import UserRunReport
from shortlist.server.db.models import (
    Base,
    Collection,
    Delivery,
    PickRow,
    RowDeliverySnapshot,
    Run,
    RunSharedRow,
    RunUser,
    User,
)
from shortlist.server.services.collection_reconcile import forget_user_deliveries
from shortlist.server.services.delivery_snapshots import current_pick_ids
from shortlist.server.services.run_persistence import (
    _decide_outcomes,
    _persist_shared_row_report,
    _persist_user_report,
    prune_runs,
)
from shortlist.server.services.watch_events import RowMembership, tmdb_by_rating_key

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add_all(
            [
                User(id=1, plex_account_id=99, username="alex", slug="alex", enabled=True),
                User(id=2, plex_account_id=100, username="sam", slug="sam", enabled=True),
                Collection(id=1, slug="picked", name="Picked", enabled=True),
                Collection(id=2, slug="shared", name="Shared", enabled=True, build="shared"),
            ]
        )
        session.commit()
    return factory


def deliver(sessions, titles, *, at=NOW, shared=False, audience=None, muted=(), dry_run=False, status="ok"):
    """Persist a genuine per-library delivery result through the production write seam."""
    with sessions() as session:
        user = session.get(User, 1)
        slug = "shared" if shared else "picked"
        report = UserRunReport(username=user.username, slug=f"shared_{slug}" if shared else user.slug, status=status)
        run = Run(trigger="manual", status=status, started_at=at, dry_run=dry_run)
        session.add(run)
        session.flush()
        report.breakdown = [
            {
                "delivery_id": str(uuid4()),
                "delivered_at": at.isoformat(),
                "row_slug": slug,
                "library_key": "1",
                "library_title": "Movies",
                "rating_key": 700,
                "audience": audience,
                "muted": list(muted),
                "picks": [
                    {
                        "tmdb_id": tid,
                        "rating_key": tid + 1000,
                        "media_type": "movie",
                        "rank": rank,
                        "title": f"Title {tid}",
                    }
                    for rank, tid in enumerate(titles, 1)
                ],
            }
        ]
        if shared:
            _persist_shared_row_report(session, run.id, report, dry_run)
        else:
            _persist_user_report(session, run.id, user, report, dry_run)
        session.commit()
        return run.id, report


def test_latest_delivery_at_play_time_survives_rotation(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=2))
    deliver(sessions, [11], at=NOW - timedelta(hours=1))
    with sessions() as session:
        membership = RowMembership(session)
        user = session.get(User, 1)
        assert membership.visible_rows(user, {(10, "movie")}, NOW - timedelta(minutes=90)) == ["picked"]
        assert membership.visible_rows(user, {(10, "movie")}, NOW - timedelta(minutes=30)) == []
        assert membership.visible_rows(user, {(11, "movie")}, NOW - timedelta(minutes=90)) == []


def test_explicit_empty_delivery_closes_membership(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=2))
    deliver(sessions, [], at=NOW - timedelta(hours=1))
    with sessions() as session:
        assert current_pick_ids(session) == {}
        membership = RowMembership(session)
        assert membership.visible_rows(session.get(User, 1), {(10, "movie")}, NOW) == []
        assert membership.visible_rows(session.get(User, 1), {(10, "movie")}, NOW - timedelta(minutes=90))


def test_dry_run_does_not_replace_delivered_contents(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=2))
    deliver(sessions, [11], at=NOW - timedelta(hours=1), dry_run=True)
    with sessions() as session:
        assert session.query(RowDeliverySnapshot).count() == 1
        assert RowMembership(session).visible_rows(session.get(User, 1), {(10, "movie")}, NOW)


def test_partial_success_records_confirmed_library_despite_overall_error(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=1), status="error")
    with sessions() as session:
        picks = session.query(PickRow).all()
        assert len(picks) == 1
        assert current_pick_ids(session) == {1: {picks[0].id}}
        assert picks[0].created_at.replace(tzinfo=UTC) == NOW - timedelta(hours=1)


def test_shared_only_titles_resolve_with_frozen_audience_and_mutes(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=1), shared=True, audience=[99, 100], muted=[100])
    with sessions() as session:
        assert session.query(PickRow).count() == 0
        assert tmdb_by_rating_key(session)[1010] == (10, "movie")
        membership = RowMembership(session)
        assert membership.visible_shared_rows(session.get(User, 1), {(10, "movie")}, NOW) == ["shared"]
        assert membership.visible_shared_rows(session.get(User, 2), {(10, "movie")}, NOW) == []


def test_shared_snapshot_and_current_personal_picks_survive_run_deletion(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    deliver(sessions, [11], at=NOW - timedelta(hours=1), shared=True)
    with sessions() as session:
        before = current_pick_ids(session)
        session.query(PickRow).update({PickRow.run_id: None})
        session.query(RunUser).delete()
        session.query(RunSharedRow).delete()
        session.query(Run).delete()
        session.commit()
        assert current_pick_ids(session) == before
        assert RowMembership(session).visible_shared_rows(session.get(User, 1), {(11, "movie")}, NOW)


def test_removal_and_recreation_do_not_bridge_the_gap(sessions, monkeypatch):
    from shortlist.server.services import delivery_snapshots
    from tests.conftest import freeze_clock

    deliver(sessions, [10], at=NOW - timedelta(hours=3))
    freeze_clock(monkeypatch, delivery_snapshots, NOW - timedelta(hours=2))
    with sessions() as session:
        forget_user_deliveries(session, "alex")
        session.commit()
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    with sessions() as session:
        membership = RowMembership(session)
        user = session.get(User, 1)
        assert membership.visible_rows(user, {(10, "movie")}, NOW - timedelta(minutes=90)) == []
        assert membership.visible_rows(user, {(10, "movie")}, NOW)


def test_snapshot_watch_flag_does_not_credit_an_off_row_gap(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=3))
    deliver(sessions, [11], at=NOW - timedelta(hours=2))
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    with sessions() as session:
        out = _decide_outcomes(
            session,
            session.get(User, 1),
            credits={},
            progress={},
            latest_watch={(10, "movie"): NOW - timedelta(minutes=90)},
            finished_keys=set(),
            live_pick_ids_for_user=current_pick_ids(session)[1],
        )
        assert out == {}


def test_repeated_persistence_keeps_original_interval(sessions):
    run_id, report = deliver(sessions, [10], at=NOW - timedelta(hours=1), shared=True)
    with sessions() as session:
        _persist_shared_row_report(session, run_id, report, False)
        session.commit()
        rows = session.query(RowDeliverySnapshot).all()
        assert len(rows) == 1
        assert rows[0].delivered_at.replace(tzinfo=UTC) == NOW - timedelta(hours=1)
        assert rows[0].ended_at is None


def test_current_snapshot_outlives_run_retention(sessions):
    deliver(sessions, [10], at=datetime.now(UTC) - timedelta(days=60))
    with sessions() as session:
        before = current_pick_ids(session)
        assert prune_runs(session, 1) == 1
        session.commit()
        assert current_pick_ids(session) == before


def test_pick_ids_are_validated_against_person_and_title(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    with sessions() as session:
        row = session.query(RowDeliverySnapshot).one()
        pick = session.query(PickRow).one()
        pick.user_id = 2
        session.commit()
        assert row.picks[0]["pick_id"] == pick.id
        assert current_pick_ids(session) == {}


def test_replaced_plex_collection_does_not_reactivate_stale_snapshot(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    with sessions() as session:
        session.query(Delivery).update({Delivery.rating_key: 999})
        session.commit()
        assert current_pick_ids(session) == {}
        assert RowMembership(session).visible_rows(session.get(User, 1), {(10, "movie")}, NOW) == []


def test_closed_interval_still_credits_late_play_after_collection_rebuild(sessions):
    deliver(sessions, [10], at=NOW - timedelta(hours=2))
    deliver(sessions, [11], at=NOW - timedelta(hours=1))
    with sessions() as session:
        session.query(Delivery).update({Delivery.rating_key: 999})
        current = session.query(RowDeliverySnapshot).filter(RowDeliverySnapshot.ended_at.is_(None)).one()
        current.rating_key = 999
        session.commit()
        membership = RowMembership(session)
        assert membership.visible_rows(session.get(User, 1), {(10, "movie")}, NOW - timedelta(minutes=90)) == ["picked"]
