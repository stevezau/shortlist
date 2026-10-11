"""0102 preserves only evidenced deliveries independently of disposable run history."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType
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
from shortlist.server.db.session import make_engine
from shortlist.server.services.watch_events import RowMembership
from tests.db_helpers import disposing_engine
from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations

START = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def legacy(tmp_path: Path) -> Iterator[Session]:
    command.upgrade(_alembic(tmp_path), "0101")
    with disposing_engine(make_engine(tmp_path)) as engine:
        assert not sa.inspect(engine).has_table("row_delivery_snapshots")
        with Session(engine) as session:
            session.add_all(
                [
                    User(id=1, plex_account_id=101, username="Alice", slug="alice"),
                    User(id=2, plex_account_id=202, username="Bob", slug="bob"),
                ]
            )
            session.commit()
            yield session


def _entry(
    tmdb_id: int = 10,
    *,
    library: str = "1",
    row: str = "daily",
    media_type: str = "movie",
) -> dict:
    return {
        "row_slug": row,
        "library_key": library,
        "rating_key": 1000 + int(library),
        "picks": [{"tmdb_id": tmdb_id, "media_type": media_type, "rating_key": 100 + tmdb_id, "title": "A pick"}],
    }


def _run(session: Session, run_id: int, *, hours: int = 0, dry: bool = False) -> Run:
    run = Run(
        id=run_id,
        trigger="manual",
        started_at=START + timedelta(hours=hours),
        began_at=START + timedelta(hours=hours),
        dry_run=dry,
        status="ok",
    )
    session.add(run)
    session.flush()
    return run


def _pick(session: Session, run_id: int | None, entry: dict, *, hours: int = 0, user_id: int = 1) -> PickRow:
    item = entry["picks"][0]
    pick = PickRow(
        run_id=run_id,
        user_id=user_id,
        collection_slug=entry["row_slug"],
        section_key=entry["library_key"],
        rank=1,
        tmdb_id=item["tmdb_id"],
        media_type=item["media_type"],
        rating_key=item["rating_key"],
        title=item["title"],
        created_at=START + timedelta(hours=hours, minutes=10),
    )
    session.add(pick)
    session.flush()
    return pick


def _upgrade(session: Session, tmp_path: Path) -> list[RowDeliverySnapshot]:
    session.commit()
    command.upgrade(_alembic(tmp_path), "0102")
    session.expire_all()
    return list(session.scalars(sa.select(RowDeliverySnapshot).order_by(RowDeliverySnapshot.delivered_at)))


def test_0102_backfills_exact_personal_library_contents_even_when_later_library_failed(
    legacy: Session, tmp_path: Path
) -> None:
    run = _run(legacy, 1)
    run.status = "error"
    movie, show = _entry(), _entry(library="2", media_type=MediaType.SHOW.value)
    delivered = [_pick(legacy, 1, movie), _pick(legacy, 1, show)]
    failed_library_pick = _pick(legacy, 1, _entry(99, library="3"))
    unlisted_pick = _pick(legacy, 1, _entry(88))
    legacy.add(RunUser(run_id=1, user_id=1, status="error", breakdown=[movie, show]))

    snapshots = _upgrade(legacy, tmp_path)

    assert len(snapshots) == 2
    assert {(s.library_key, s.picks[0]["media_type"]) for s in snapshots} == {("1", "movie"), ("2", "show")}
    assert {s.picks[0]["pick_id"] for s in snapshots} == {p.id for p in delivered}
    assert all(s.picks[0]["pick_id"] not in {failed_library_pick.id, unlisted_pick.id} for s in snapshots)
    assert all(s.delivered_at == START.replace(tzinfo=None) + timedelta(minutes=10) for s in snapshots)
    assert all(s.user_id == 1 and s.user_slug == "alice" and not s.shared and s.ended_at is None for s in snapshots)


@pytest.mark.parametrize(
    "invalid",
    [
        "detached",
        "dry",
        "no_breakdown",
        "no_collection_key",
        "no_picks",
        "wrong_user",
        "wrong_row",
        "wrong_library",
        "wrong_type",
        "wrong_title_key",
        "untyped",
        "failed_entry",
        "incomplete_picks",
    ],
)
def test_0102_refuses_unproven_personal_deliveries(legacy: Session, tmp_path: Path, invalid: str) -> None:
    _run(legacy, 1, dry=invalid == "dry")
    entry = _entry()
    pick = _pick(legacy, None if invalid == "detached" else 1, entry)
    if invalid == "wrong_user":
        pick.user_id = 2
    elif invalid == "wrong_row":
        pick.collection_slug = "another"
    elif invalid == "wrong_library":
        pick.section_key = "2"
    elif invalid == "wrong_type":
        pick.media_type = MediaType.SHOW.value
    elif invalid == "wrong_title_key":
        pick.rating_key += 1
    elif invalid == "no_collection_key":
        entry.pop("rating_key")
    elif invalid == "no_picks":
        entry.pop("picks")
    elif invalid == "untyped":
        entry["picks"][0].pop("media_type")
    elif invalid == "failed_entry":
        entry["status"] = "error"
    elif invalid == "incomplete_picks":
        entry["picks"].append(_entry(11)["picks"][0])
    legacy.add(RunUser(run_id=1, user_id=1, breakdown=[] if invalid == "no_breakdown" else [entry]))

    snapshots = _upgrade(legacy, tmp_path)
    if invalid in {"dry", "no_breakdown", "no_collection_key", "failed_entry"}:
        assert snapshots == []
    else:
        assert len(snapshots) == 1 and snapshots[0].picks == []


def test_0102_closes_each_library_at_its_next_delivery_including_explicit_empty(
    legacy: Session, tmp_path: Path
) -> None:
    # Run IDs do not establish chronology after clearing history can recycle them.
    for run_id, hours in [(50, 0), (2, 2), (3, 4)]:
        _run(legacy, run_id, hours=hours)
        entry = _entry(run_id)
        if run_id == 2:
            entry["picks"] = []
        else:
            _pick(legacy, run_id, entry, hours=hours)
        legacy.add(RunUser(run_id=run_id, user_id=1, breakdown=[entry]))
    second_library = _entry(99, library="2")
    _pick(legacy, 50, second_library)
    first_report = legacy.get(RunUser, (50, 1))
    first_report.breakdown = [*first_report.breakdown, second_library]

    snapshots = _upgrade(legacy, tmp_path)

    timeline = [s for s in snapshots if s.library_key == "1"]
    assert [s.picks == [] for s in timeline] == [False, True, False]
    assert timeline[0].ended_at == START.replace(tzinfo=None) + timedelta(hours=2)
    assert timeline[1].ended_at == START.replace(tzinfo=None) + timedelta(hours=4, minutes=10)
    assert timeline[2].ended_at is None
    assert next(s for s in snapshots if s.library_key == "2").ended_at is None
    before = [(s.id, s.source_key, s.ended_at, s.picks) for s in snapshots]

    legacy.execute(sa.update(PickRow).values(run_id=None))
    legacy.execute(sa.delete(RunUser))
    legacy.execute(sa.delete(Run))
    legacy.commit()
    command.stamp(_alembic(tmp_path), "0101")
    after = _upgrade(legacy, tmp_path)
    assert [(s.id, s.source_key, s.ended_at, s.picks) for s in after] == before
    assert all(len(s.source_key) <= 255 for s in after)


def test_0102_shared_snapshots_keep_typed_library_contents_audience_and_mutes(legacy: Session, tmp_path: Path) -> None:
    _run(legacy, 1)
    movie, show = _entry(row="shared-row"), _entry(library="2", row="shared-row", media_type=MediaType.SHOW.value)
    legacy.add(
        RunSharedRow(
            run_id=1,
            collection_slug="shared-row",
            status="error",
            breakdown=[movie, show],
            picks=[*movie["picks"], *show["picks"]],
            audience=[101, 202],
            muted=[202],
            delivered_at=START + timedelta(minutes=20),
        )
    )
    _run(legacy, 2, hours=2)
    public = _entry(30, row="shared-row")
    legacy.add(RunSharedRow(run_id=2, collection_slug="shared-row", breakdown=[public], picks=public["picks"]))

    snapshots = _upgrade(legacy, tmp_path)

    assert len(snapshots) == 3
    assert all(s.shared and s.user_id is None and s.user_slug == "shared_shared-row" for s in snapshots)
    subset = [s for s in snapshots if s.audience is not None]
    assert len(subset) == 2
    assert all(s.audience == [101, 202] and s.muted == [202] for s in subset)
    assert {(s.library_key, s.picks[0]["media_type"]) for s in subset} == {("1", "movie"), ("2", "show")}
    assert all(s.delivered_at == START.replace(tzinfo=None) + timedelta(minutes=20) for s in subset)
    assert next(s for s in subset if s.library_key == "1").ended_at == START.replace(tzinfo=None) + timedelta(hours=2)
    assert next(s for s in subset if s.library_key == "2").ended_at is None
    public_snapshot = next(s for s in snapshots if s.audience is None)
    assert public_snapshot.picks == public["picks"]
    assert public_snapshot.muted == []


@pytest.mark.parametrize("invalid", ["dry", "untyped", "malformed_audience", "missing_library", "wrong_row"])
def test_0102_refuses_unproven_shared_membership(legacy: Session, tmp_path: Path, invalid: str) -> None:
    _run(legacy, 1, dry=invalid == "dry")
    entry = _entry(row="shared-row")
    if invalid == "untyped":
        entry["picks"][0].pop("media_type")
    elif invalid == "missing_library":
        entry.pop("library_key")
    elif invalid == "wrong_row":
        entry["row_slug"] = "another"
    legacy.add(
        RunSharedRow(
            run_id=1,
            collection_slug="shared-row",
            breakdown=[entry],
            picks=entry["picks"],
            audience="unknown" if invalid == "malformed_audience" else None,
        )
    )

    snapshots = _upgrade(legacy, tmp_path)
    if invalid in {"dry", "missing_library", "wrong_row"}:
        assert snapshots == []
    else:
        assert len(snapshots) == 1 and snapshots[0].picks == [] and snapshots[0].audience == []


def test_0102_explicit_empty_shared_delivery_closes_membership_without_granting_an_unknown_audience(
    legacy: Session, tmp_path: Path
) -> None:
    for run_id, hours in [(1, 0), (2, 1)]:
        _run(legacy, run_id, hours=hours)
        entry = _entry(row="shared-row")
        if run_id == 2:
            entry["picks"] = []
        legacy.add(RunSharedRow(run_id=run_id, collection_slug="shared-row", breakdown=[entry], picks=entry["picks"]))

    first, empty = _upgrade(legacy, tmp_path)

    assert first.ended_at == START.replace(tzinfo=None) + timedelta(hours=1)
    assert empty.picks == [] and empty.audience == [] and empty.ended_at is None


def test_0102_schema_has_no_run_dependency_and_downgrade_removes_only_snapshots(
    legacy: Session, tmp_path: Path
) -> None:
    _upgrade(legacy, tmp_path)
    inspector = sa.inspect(legacy.get_bind())
    foreign_keys = inspector.get_foreign_keys("row_delivery_snapshots")
    assert [(fk["referred_table"], fk["options"]) for fk in foreign_keys] == [("users", {"ondelete": "RESTRICT"})]
    assert any(
        index["name"] == "ix_row_delivery_identity_time" for index in inspector.get_indexes("row_delivery_snapshots")
    )
    legacy.commit()

    command.downgrade(_alembic(tmp_path), "0101")

    assert not sa.inspect(legacy.get_bind()).has_table("row_delivery_snapshots")
    assert legacy.scalar(sa.select(sa.func.count()).select_from(User)) == 2


def test_0102_migrated_show_is_visible_to_the_runtime_membership_reader(legacy: Session, tmp_path: Path) -> None:
    _run(legacy, 1)
    personal = _entry(media_type=MediaType.SHOW.value)
    shared = _entry(row="shared-row", media_type=MediaType.SHOW.value)
    _pick(legacy, 1, personal)
    # Core inserts: the ORM would also write columns a later migration adds, which this old schema lacks.
    legacy.execute(sa.insert(Collection).values(slug="daily", name="Daily", enabled=True))
    legacy.execute(sa.insert(Collection).values(slug="shared-row", name="Shared", enabled=True, build="shared"))
    legacy.add_all(
        [
            Delivery(collection_slug="daily", user_slug="alice", library_key="1", rating_key=personal["rating_key"]),
            Delivery(
                collection_slug="shared-row",
                user_slug="shared_shared-row",
                library_key="1",
                rating_key=shared["rating_key"],
            ),
            RunUser(run_id=1, user_id=1, breakdown=[personal]),
            RunSharedRow(
                run_id=1, collection_slug="shared-row", breakdown=[shared], picks=shared["picks"], audience=[101]
            ),
        ]
    )

    _upgrade(legacy, tmp_path)
    membership = RowMembership(legacy)
    user = legacy.get(User, 1)
    when = START + timedelta(hours=1)
    show_key = {(10, MediaType.SHOW.value)}

    assert membership.visible_rows(user, show_key, when) == ["daily"]
    assert membership.visible_shared_rows(user, show_key, when) == ["shared-row"]
    assert membership.visible_rows(user, {(10, MediaType.MOVIE.value)}, when) == []
    assert membership.visible_shared_rows(legacy.get(User, 2), show_key, when) == []


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("invalid", ["missing_type", "missing_pick", "mismatched_pick"])
def test_0102_unprovable_successor_closes_previous_membership(
    legacy: Session, tmp_path: Path, shared: bool, invalid: str
) -> None:
    for run_id, hours in [(1, 0), (2, 2)]:
        _run(legacy, run_id, hours=hours)
        entry = _entry(run_id)
        if shared:
            flat_picks = [dict(p) for p in entry["picks"]]
            if run_id == 2:
                if invalid == "missing_type":
                    entry["picks"][0].pop("media_type")
                elif invalid == "missing_pick":
                    flat_picks = []
                else:
                    flat_picks[0]["tmdb_id"] += 1
            legacy.add(RunSharedRow(run_id=run_id, collection_slug="daily", breakdown=[entry], picks=flat_picks))
        else:
            pick = _pick(legacy, run_id, entry, hours=hours)
            if run_id == 2:
                if invalid == "missing_type":
                    entry["picks"][0].pop("media_type")
                elif invalid == "missing_pick":
                    pick.run_id = None
                else:
                    pick.tmdb_id += 1
            legacy.add(RunUser(run_id=run_id, user_id=1, breakdown=[entry]))

    earlier, unknown = _upgrade(legacy, tmp_path)

    assert earlier.picks[0]["tmdb_id"] == 1
    boundary = START.replace(tzinfo=None) + timedelta(hours=2, minutes=0 if shared or invalid == "missing_pick" else 10)
    assert earlier.ended_at == boundary
    assert unknown.picks == [] and unknown.ended_at is None
    if shared:
        assert unknown.audience == []


def test_0102_typed_breakdown_does_not_turn_legacy_unknown_audience_public(legacy: Session, tmp_path: Path) -> None:
    _run(legacy, 1)
    entry = _entry()
    old_flat_pick = {k: v for k, v in entry["picks"][0].items() if k != "media_type"}
    legacy.add(
        RunSharedRow(
            run_id=1,
            collection_slug="daily",
            breakdown=[entry],
            picks=[old_flat_pick],
            audience=None,
            delivered_at=None,
        )
    )

    [snapshot] = _upgrade(legacy, tmp_path)

    assert snapshot.picks == [] and snapshot.audience == []


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("known_execution", [False, True])
def test_0102_queued_successor_cannot_reactivate_older_membership(
    legacy: Session, tmp_path: Path, shared: bool, known_execution: bool
) -> None:
    first = _run(legacy, 1)
    second = _run(legacy, 2)
    second.started_at = START + timedelta(minutes=5)
    second.began_at = START + timedelta(minutes=15) if known_execution else None
    good, unknown = _entry(10), _entry(11)
    if shared:
        legacy.add_all(
            [
                RunSharedRow(
                    run_id=first.id,
                    collection_slug="daily",
                    breakdown=[good],
                    picks=good["picks"],
                    delivered_at=START + timedelta(minutes=10),
                ),
                RunSharedRow(run_id=second.id, collection_slug="daily", breakdown=[unknown], picks=[]),
            ]
        )
    else:
        _pick(legacy, first.id, good)
        legacy.add_all(
            [
                RunUser(run_id=first.id, user_id=1, breakdown=[good]),
                RunUser(run_id=second.id, user_id=1, breakdown=[unknown]),
            ]
        )
    snapshots = _upgrade(legacy, tmp_path)
    assert all(snapshot.picks == [] for snapshot in snapshots if snapshot.ended_at is None)
    if not known_execution:
        assert all(snapshot.picks == [] for snapshot in snapshots)
