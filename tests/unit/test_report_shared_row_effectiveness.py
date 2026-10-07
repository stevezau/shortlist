"""Shared delivery history is real delivery history, even before anyone watches it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.db.models import Base, Collection, RowDeliverySnapshot, Run, RunSharedRow
from shortlist.server.services.report_service import row_effectiveness
from tests.db_helpers import disposing_engine

NOW = datetime(2026, 10, 7, 17, 33, 3, tzinfo=UTC)


@pytest.fixture
def sessions():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        with factory() as session:
            session.add(Collection(slug="shared", name="Shared", enabled=True, build="shared"))
            session.commit()
        yield factory


def _shared_run(
    session,
    *,
    run_id: int,
    when: datetime,
    status: str = "ok",
    dry_run: bool = False,
    row_status: str = "ok",
    delivered_at: datetime | None = None,
    evidence: bool = True,
    slug: str = "shared",
) -> None:
    """Persist a shared report shape without fabricating personal pick history."""
    session.add(Run(id=run_id, trigger="schedule", status=status, dry_run=dry_run, started_at=when))
    session.add(
        RunSharedRow(
            run_id=run_id,
            collection_slug=slug,
            status=row_status,
            delivered_at=delivered_at,
            # Only the non-null delivery stamp proves that the row reached Plex.
            breakdown=[{"library_key": "1"}] if evidence else [],
        )
    )


def test_shared_delivery_marks_row_built_without_personal_watch_history(sessions):
    """The row card must not call a delivered shared row “Not built yet”."""
    mixed_run_delivery = NOW - timedelta(hours=3)
    actual_delivery = NOW - timedelta(hours=2)
    with sessions() as session:
        # Current durable delivery timestamp is the authoritative last-built clock.
        _shared_run(session, run_id=1, when=actual_delivery, delivered_at=actual_delivery)
        # Another row may fail after this shared row has already landed; the shared
        # row's own successful delivery is the authoritative evidence.
        _shared_run(session, run_id=2, when=mixed_run_delivery, status="error", delivered_at=mixed_run_delivery)
        # A failed shared outcome, a dry run, a different row, or a record
        # without the durable delivery stamp may manufacture row history.
        _shared_run(
            session,
            run_id=3,
            when=NOW - timedelta(hours=1),
            row_status="error",
            delivered_at=NOW - timedelta(hours=1),
        )
        _shared_run(
            session, run_id=4, when=NOW - timedelta(minutes=30), dry_run=True, delivered_at=NOW - timedelta(minutes=30)
        )
        _shared_run(session, run_id=5, when=NOW - timedelta(minutes=15), delivered_at=None, evidence=True)
        _shared_run(
            session, run_id=6, when=NOW - timedelta(minutes=5), delivered_at=NOW - timedelta(minutes=5), slug="other"
        )
        session.commit()

    with sessions() as session:
        panel = row_effectiveness(session, "shared", now=NOW)

    assert panel["first_delivered_at"] == mixed_run_delivery.isoformat()
    assert panel["last_delivered_at"] == actual_delivery.isoformat()
    assert panel["runs"] == 2
    # Shared rows are one Plex collection, never fabricated per-person picks.
    assert panel["delivered"] == panel["watched"] == panel["finished"] == 0
    assert panel["matured"] is None
    assert panel["per_library"] == []


def test_historical_shared_snapshot_marks_row_built_without_guessing_from_run_start(sessions):
    """A retained delivery ledger is enough when an old run report lacks its exact stamp.

    Snapshots are written only after an actual non-dry Plex delivery and survive disposable run
    history.  A successful-looking legacy report without either an exact stamp or a snapshot must
    not turn its queue/start time into a delivery claim.
    """
    actual_delivery = NOW - timedelta(days=4)
    with sessions() as session:
        # This old report is not itself sufficient delivery evidence.
        _shared_run(session, run_id=1, when=NOW - timedelta(days=1), delivered_at=None)
        # The independent shared delivery ledger is retained after the run report is pruned.
        session.add(
            RowDeliverySnapshot(
                source_key="legacy-shared-delivery",
                collection_slug="shared",
                user_slug="shared_shared",
                library_key="1",
                shared=True,
                rating_key=1,
                delivered_at=actual_delivery,
                picks=[],
                audience=None,
                muted=[],
            )
        )
        session.commit()

    with sessions() as session:
        panel = row_effectiveness(session, "shared", now=NOW)

    assert panel["first_delivered_at"] == actual_delivery.isoformat()
    assert panel["last_delivered_at"] == actual_delivery.isoformat()
    # The Runs tile mirrors retained RunSharedRow records selected by /api/runs.  This historical
    # snapshot proves delivery, but its missing exact run stamp cannot invent a retained run.
    assert panel["runs"] == 0


def test_migration_legacy_snapshot_does_not_claim_a_delivery_time(sessions):
    """Migration 0102 may have clocked this row from a run start, so it is not delivery evidence."""
    with sessions() as session:
        session.add(
            RowDeliverySnapshot(
                source_key="legacy:inferred-shared-time",
                collection_slug="shared",
                user_slug="shared_shared",
                library_key="1",
                shared=True,
                rating_key=1,
                delivered_at=NOW - timedelta(days=7),
                picks=[],
                audience=None,
                muted=[],
            )
        )
        session.commit()

    with sessions() as session:
        panel = row_effectiveness(session, "shared", now=NOW)

    assert panel["first_delivered_at"] is None
    assert panel["last_delivered_at"] is None
    assert panel["runs"] == 0


def test_confirmed_shared_snapshot_ignores_an_older_migration_inference(sessions):
    """A real retained delivery remains visible without letting a guessed legacy time predate it."""
    confirmed_delivery = NOW - timedelta(days=3)
    with sessions() as session:
        session.add_all(
            [
                RowDeliverySnapshot(
                    source_key="legacy:inferred-shared-time",
                    collection_slug="shared",
                    user_slug="shared_shared",
                    library_key="1",
                    shared=True,
                    rating_key=1,
                    delivered_at=NOW - timedelta(days=8),
                    picks=[],
                    audience=None,
                    muted=[],
                ),
                RowDeliverySnapshot(
                    source_key="confirmed-shared-delivery",
                    collection_slug="shared",
                    user_slug="shared_shared",
                    library_key="2",
                    shared=True,
                    rating_key=2,
                    delivered_at=confirmed_delivery,
                    picks=[],
                    audience=None,
                    muted=[],
                ),
            ]
        )
        session.commit()

    with sessions() as session:
        panel = row_effectiveness(session, "shared", now=NOW)

    assert panel["first_delivered_at"] == confirmed_delivery.isoformat()
    assert panel["last_delivered_at"] == confirmed_delivery.isoformat()
    assert panel["runs"] == 0
