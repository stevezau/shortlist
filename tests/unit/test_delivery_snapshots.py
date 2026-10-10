"""Delivered rows retain identity and at-play membership independently of run logs."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import UserRunReport
from shortlist.server.db.models import (
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
from tests.db_helpers import create_schema, disposing_engine

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)


@pytest.fixture
def sessions():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
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
        yield factory


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


@pytest.fixture
def retry_delivery(monkeypatch):
    """A real two-library delivery whose second library fails after the first one lands."""
    from copy import deepcopy
    from threading import Lock
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import requests

    from shortlist.engine import delivery, rows
    from shortlist.engine.clients import plex_pms
    from shortlist.engine.clients.plex_pms import PlexClient
    from shortlist.engine.models import EngineConfig, MediaType, Pick, RowSpec
    from tests.conftest import make_profile

    world = SimpleNamespace(
        clock=NOW, retries=0, first_entries=None, before_retry=lambda: None, writes=[], fail_library="3"
    )
    profile = make_profile("alex", account_id=99)
    sections = [
        SimpleNamespace(key="1", type="movie", title="Movies"),
        SimpleNamespace(key="3", type="movie", title="More Movies"),
    ]
    members = {"1": [1010], "3": [1020]}
    collections = {}
    for section in sections:
        collection = MagicMock(title="Picked" + delivery.row_marker(profile.plex_account_id))
        collection.ratingKey = 700 if section.key == "1" else 701
        collection.labels = [SimpleNamespace(tag="shortlist_alex"), SimpleNamespace(tag="shortlist")]
        collection.items.side_effect = lambda key=section.key: [
            SimpleNamespace(ratingKey=rating_key, title=f"Title {rating_key}") for rating_key in members[key]
        ]
        collections[section.key] = collection
    plex = MagicMock(spec=PlexClient)
    plex.find_owned_collections.side_effect = lambda section, label: [collections[section.key]]
    plex.matches_section.return_value = True
    plex.fetch_items.side_effect = lambda keys: ([SimpleNamespace(ratingKey=key) for key in keys], [])
    plex.stored_label.side_effect = lambda collection, label, **kw: label
    report = UserRunReport(username="alex", slug="alex", status="ok")

    def set_items(collection, existing, added, wanted):
        key = next(key for key, candidate in collections.items() if candidate is collection)
        if key == world.fail_library and world.first_entries is None:
            world.first_entries = deepcopy(report.breakdown)
            raise requests.exceptions.ReadTimeout("second library temporarily unavailable")
        members[key] = list(wanted)
        world.writes.append(key)

    plex.set_items.side_effect = set_items

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return world.clock if tz else world.clock.replace(tzinfo=None)

    def backoff(_seconds):
        world.retries += 1
        world.clock = NOW + timedelta(minutes=5 * world.retries)
        world.before_retry()

    monkeypatch.setattr(delivery, "datetime", Clock)
    monkeypatch.setattr(plex_pms.time, "sleep", backoff)
    spec = RowSpec(slug="picked", name_template="Picked", size=1)
    first = Pick(11, 1011, "New", 1, "fixture", MediaType.MOVIE)
    second = Pick(21, 1021, "Sibling", 1, "fixture", MediaType.MOVIE)
    section_picks = {"1": [first], "3": [second]}
    cfg = EngineConfig()
    ctx = SimpleNamespace(
        plex=plex,
        write_lock=Lock(),
        cancelled=lambda: False,
        progress=None,
        delivered_keys={},
        delivered_details={},
        delivery_sections=sections,
        section_index={},
        poster_artist=None,
    )
    policy = SimpleNamespace(ctx=ctx, user=profile, cfg=cfg, report=report)
    world.__dict__.update(
        report=report,
        plex=plex,
        members=members,
        collections=collections,
        section_picks=section_picks,
        policy=policy,
        spec=spec,
        sections=sections,
    )
    world.run = lambda: rows._deliver_row(
        policy,
        spec,
        [first, second],
        section_picks,
        sole_row=True,
        stored_labels={},
        order_work=None,
    )
    return world


def test_unchanged_retry_preserves_first_delivery_time_and_closes_old_membership(sessions, retry_delivery):
    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    assert retry_delivery.run()
    assert retry_delivery.retries == 1
    assert retry_delivery.writes == ["1", "3"], "the retry must really confirm library1 without rewriting it"
    with sessions() as session:
        run = Run(trigger="manual", status="ok", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, retry_delivery.report, False)
        session.commit()
        membership = RowMembership(session)
        during_retry = NOW + timedelta(minutes=2)
        assert membership.visible_rows(user, {(11, "movie")}, during_retry) == ["picked"]
        assert membership.visible_rows(user, {(10, "movie")}, during_retry) == []
        pick = session.query(PickRow).filter_by(run_id=run.id, tmdb_id=11).one()
        assert pick.created_at.replace(tzinfo=UTC) == NOW
    first = retry_delivery.first_entries[0]
    final = retry_delivery.report.breakdown[0]
    assert final["delivery_id"] == first["delivery_id"]
    assert final["delivered_at"] == first["delivered_at"] == NOW.isoformat()


def test_changed_retry_closes_old_state_without_inventing_intermediate_credit(sessions, retry_delivery):
    from shortlist.engine.models import MediaType, Pick

    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    retry_delivery.before_retry = lambda: retry_delivery.section_picks.update(
        {"1": [Pick(12, 1012, "Later", 1, "fixture", MediaType.MOVIE)]}
    )
    assert retry_delivery.run()
    with sessions() as session:
        run = Run(trigger="manual", status="ok", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, retry_delivery.report, False)
        session.commit()
        membership = RowMembership(session)
        during_retry = NOW + timedelta(minutes=2)
        assert membership.visible_rows(user, {(10, "movie")}, during_retry) == []
        assert membership.visible_rows(user, {(11, "movie")}, during_retry) == []
        assert membership.visible_rows(user, {(12, "movie")}, during_retry) == []
        assert membership.visible_rows(user, {(12, "movie")}, NOW + timedelta(minutes=5)) == ["picked"]


def test_terminal_retry_failure_keeps_known_boundary_without_claiming_delivery(sessions, retry_delivery):
    import requests

    deliver(sessions, [10], at=NOW - timedelta(hours=1))

    def fail_before_confirmation():
        retry_delivery.collections["1"].items.side_effect = requests.exceptions.ReadTimeout("unknown library state")

    retry_delivery.before_retry = fail_before_confirmation
    with pytest.raises(requests.exceptions.ReadTimeout):
        retry_delivery.run()
    assert retry_delivery.report.breakdown == []
    retry_delivery.report.status = "error"
    with sessions() as session:
        before = [(d.collection_slug, d.user_slug, d.library_key, d.rating_key) for d in session.query(Delivery)]
        run = Run(trigger="manual", status="error", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, retry_delivery.report, False)
        session.commit()
        assert [
            (d.collection_slug, d.user_slug, d.library_key, d.rating_key) for d in session.query(Delivery)
        ] == before
        assert session.query(PickRow).filter_by(run_id=run.id).count() == 0
        assert session.query(RowDeliverySnapshot).count() == 1, "a boundary must never invent delivered membership"
        membership = RowMembership(session)
        assert membership.visible_rows(user, {(10, "movie")}, NOW - timedelta(minutes=1)) == ["picked"]
        assert membership.visible_rows(user, {(10, "movie")}, NOW + timedelta(minutes=2)) == []
        assert current_pick_ids(session) == {}


@pytest.mark.parametrize(
    "change", ["collection", "content", "media_type", "audience", "muted", "rewritten", "rebuilt", "uncertain"]
)
def test_retry_does_not_backdate_changed_or_uncertain_confirmations(retry_delivery, change):
    from dataclasses import replace
    from types import SimpleNamespace

    import requests

    from shortlist.engine.models import MediaType

    world = retry_delivery

    def alter_retry():
        if change == "collection":
            world.collections["1"].ratingKey = 777
        elif change == "content":
            world.section_picks["1"] = [replace(world.section_picks["1"][0], tmdb_id=12, rating_key=1012)]
        elif change == "media_type":
            world.section_picks["1"] = [replace(world.section_picks["1"][0], media_type=MediaType.SHOW)]
        elif change == "audience":
            world.spec.audience = {99, 100}
        elif change == "muted":
            world.spec.muted_accounts = {100}
        elif change == "rewritten":
            world.members["1"] = [9999]  # same final set, but the retry actually writes membership again
        elif change == "rebuilt":
            world.plex.find_owned_collections.side_effect = lambda section, label: (
                [] if section.key == "1" else [world.collections[section.key]]
            )

            def recreate(section, title, items):
                # Plex may reuse a deleted collection's numeric key; creation is still a new interval.
                collection = world.collections[section.key]
                collection.title = title
                world.members[section.key] = [item.ratingKey for item in items]
                return collection

            world.plex.create_collection.side_effect = recreate
        elif world.retries == 1:
            world.collections["1"].items.side_effect = requests.exceptions.ReadTimeout("uncertain read")
        else:
            world.collections["1"].items.side_effect = lambda: [
                SimpleNamespace(ratingKey=key, title=str(key)) for key in world.members["1"]
            ]

    world.before_retry = alter_retry
    assert world.run()
    first, final = world.first_entries[0], world.report.breakdown[0]
    assert final["delivery_id"] != first["delivery_id"]
    assert final["delivered_at"] == world.clock.isoformat()
    assert final["delivered_at"] != first["delivered_at"]
    boundary = next(entry for entry in world.report.delivery_boundaries if entry["library_key"] == "1")
    assert boundary["delivered_at"] == NOW.isoformat()


def test_a_separate_row_delivery_never_reuses_the_previous_retry_confirmation(retry_delivery):
    world = retry_delivery
    assert world.run()
    earlier = dict(world.report.breakdown[0])
    world.clock = NOW + timedelta(hours=1)
    assert world.run()
    final = world.report.breakdown[-2]
    assert final["library_key"] == "1"
    assert final["delivery_id"] != earlier["delivery_id"]
    assert final["delivered_at"] == world.clock.isoformat()


@pytest.mark.parametrize("mode", ["dry_run", "unnamed"])
def test_no_write_result_establishes_no_retry_boundary(retry_delivery, mode):
    world = retry_delivery
    if mode == "dry_run":
        world.policy.cfg.dry_run = True
    else:
        world.spec.name_template = "{top_seed}"
        world.spec.fallback_name = ""
    assert world.run()
    assert world.writes == []
    assert world.report.delivery_boundaries == []
    assert world.members == {"1": [1010], "3": [1020]}


def test_cancel_before_retry_keeps_the_confirmed_partial_delivery(sessions, retry_delivery):
    world = retry_delivery
    world.before_retry = lambda: setattr(world.policy.ctx, "cancelled", lambda: True)
    assert world.run() is False
    assert len(world.report.breakdown) == 1
    assert world.report.breakdown[0]["delivery_id"] == world.first_entries[0]["delivery_id"]
    with sessions() as session:
        run = Run(trigger="manual", status="aborted", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, world.report, False)
        session.commit()
        assert RowMembership(session).visible_rows(user, {(11, "movie")}, NOW + timedelta(minutes=2)) == ["picked"]


def test_an_unattempted_successful_library_survives_terminal_failure(sessions, retry_delivery):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import requests

    from shortlist.engine.models import MediaType, Pick

    world = retry_delivery
    world.fail_library = "4"
    world.sections.append(SimpleNamespace(key="4", type="movie", title="Third Movies"))
    world.members["4"] = [1030]
    third = MagicMock(title=world.collections["1"].title, ratingKey=704, labels=[])
    third.items.side_effect = lambda: [SimpleNamespace(ratingKey=key, title=str(key)) for key in world.members["4"]]
    world.collections["4"] = third
    world.section_picks["4"] = [Pick(31, 1031, "Third", 1, "fixture", MediaType.MOVIE)]

    def fail_first_library():
        world.collections["1"].items.side_effect = requests.exceptions.ReadTimeout("first library now unavailable")

    world.before_retry = fail_first_library
    with pytest.raises(requests.exceptions.ReadTimeout):
        world.run()
    assert [entry["library_key"] for entry in world.report.breakdown] == ["3"]
    with sessions() as session:
        run = Run(trigger="manual", status="error", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, world.report, False)
        session.commit()
        membership = RowMembership(session)
        assert membership.visible_rows(user, {(21, "movie")}, NOW + timedelta(minutes=2)) == ["picked"]
        assert membership.visible_rows(user, {(11, "movie")}, NOW + timedelta(minutes=2)) == []
        assert membership.visible_rows(user, {(31, "movie")}, NOW + timedelta(minutes=2)) == []


def test_replayed_boundaries_preserve_same_time_and_newer_deliveries(sessions, retry_delivery):
    from shortlist.server.services.delivery_snapshots import record_delivery_boundaries, record_snapshots

    deliver(sessions, [10], at=NOW - timedelta(hours=1))
    world = retry_delivery
    assert world.run()
    with sessions() as session:
        run = Run(trigger="manual", status="ok", started_at=NOW)
        session.add(run)
        session.flush()
        run_id = run.id
        user = session.get(User, 1)
        _persist_user_report(session, run_id, user, world.report, False)
        session.commit()
        for _ in range(2):
            record_delivery_boundaries(session, user, world.report.delivery_boundaries)
            record_snapshots(session, run_id, user.slug, world.report.breakdown, user_id=user.id)
        session.commit()
        assert RowMembership(session).visible_rows(user, {(11, "movie")}, NOW + timedelta(minutes=2)) == ["picked"]
    deliver(sessions, [13], at=NOW + timedelta(hours=1))
    with sessions() as session:
        user = session.get(User, 1)
        record_delivery_boundaries(session, user, world.report.delivery_boundaries)
        record_snapshots(session, run_id, user.slug, world.report.breakdown, user_id=user.id)
        session.commit()
        assert RowMembership(session).visible_rows(user, {(13, "movie")}, NOW + timedelta(hours=2)) == ["picked"]
        current = (
            session.query(RowDeliverySnapshot).filter_by(collection_slug="picked", library_key="1", ended_at=None).one()
        )
        assert current.delivered_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


def test_boundary_is_scoped_to_exact_person_row_and_library_and_only_shortens(sessions):
    from shortlist.server.services.delivery_snapshots import record_delivery_boundaries

    with sessions() as session:
        user = session.get(User, 1)
        variants = [
            {},
            {"user_id": 2},  # same old slug, different historical owner
            {"user_slug": "other"},
            {"collection_slug": "other"},
            {"library_key": "3"},
            {"shared": True},
            {"delivered_at": NOW},
            {"delivered_at": NOW + timedelta(minutes=1)},
            {"ended_at": NOW - timedelta(minutes=1)},
        ]
        snapshots = []
        for index, variant in enumerate(variants):
            values = dict(
                source_key=f"scope:{index}",
                user_id=1,
                user_slug="alex",
                collection_slug="picked",
                library_key="1",
                shared=False,
                rating_key=700,
                delivered_at=NOW - timedelta(hours=1),
                picks=[],
            )
            values.update(variant)
            snapshot = RowDeliverySnapshot(**values)
            session.add(snapshot)
            snapshots.append(snapshot)
        session.flush()
        before = [snapshot.ended_at for snapshot in snapshots]
        boundary = {"row_slug": "picked", "library_key": "1", "rating_key": 700, "delivered_at": NOW.isoformat()}
        record_delivery_boundaries(session, user, [boundary, boundary])
        assert snapshots[0].ended_at == NOW
        assert [snapshot.ended_at for snapshot in snapshots[1:]] == before[1:]


def test_library_skipped_on_retry_retains_its_previous_success(sessions, retry_delivery):
    world = retry_delivery
    world.before_retry = lambda: world.section_picks.update({"1": []})
    assert world.run()
    first = next(entry for entry in world.report.breakdown if entry["library_key"] == "1")
    assert first["delivery_id"] == world.first_entries[0]["delivery_id"]
    assert first["delivered_at"] == NOW.isoformat()
    with sessions() as session:
        run = Run(trigger="manual", status="ok", started_at=NOW)
        session.add(run)
        session.flush()
        user = session.get(User, 1)
        _persist_user_report(session, run.id, user, world.report, False)
        session.commit()
        assert RowMembership(session).visible_rows(user, {(11, "movie")}, NOW + timedelta(minutes=2)) == ["picked"]
