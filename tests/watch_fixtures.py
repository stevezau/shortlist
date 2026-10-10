"""Explicit delivery records for tests that build picks directly instead of running Plex.

Call these at the simulated delivery boundary. Merely inserting a diagnostic run
or pick does not establish that Plex received it.
"""

from datetime import UTC

from sqlalchemy import select

from shortlist.server.db.models import Collection, Delivery, PickRow, RowDeliverySnapshot, Run, RunSharedRow, User


def live_row(session, user_id, slug, library):
    """Declare that the simulated delivery created a current Plex row."""
    user = session.get(User, user_id)
    if session.scalar(select(Collection).where(Collection.slug == slug)) is None:
        session.add(Collection(slug=slug, name=slug, enabled=True))
    if session.get(Delivery, (slug, user.slug, library)) is None:
        session.add(Delivery(user_slug=user.slug, collection_slug=slug, library_key=library, rating_key=1))
    session.flush()


def personal_delivery(session, run_id, *, user_id=1, slug="picked", library="1"):
    session.flush()
    run = session.get(Run, run_id) if run_id is not None else None
    if run_id is None or (run is not None and run.dry_run):
        return None
    picks = list(
        session.scalars(
            select(PickRow).where(
                PickRow.run_id == run_id,
                PickRow.user_id == user_id,
                PickRow.collection_slug == slug,
                PickRow.section_key == library,
            )
        )
    )
    user = session.get(User, user_id)
    ledger = session.get(Delivery, (slug, user.slug, library))
    when = min(p.created_at.replace(tzinfo=UTC) for p in picks) if picks else run.started_at
    return _delivery(
        session,
        f"fixture:{run_id}:{user_id}:{slug}:{library}",
        user.slug,
        slug,
        library,
        ledger.rating_key if ledger else 1,
        when,
        [
            {
                "pick_id": p.id,
                "tmdb_id": p.tmdb_id,
                "media_type": p.media_type,
                "rating_key": p.rating_key,
                "title": p.title or "",
            }
            for p in picks
        ],
        user_id=user_id,
    )


def shared_delivery(session, run_id, *, slug="staff", library="1"):
    session.flush()
    report = session.get(RunSharedRow, (run_id, slug))
    run = session.get(Run, run_id)
    if run.dry_run:
        return None
    ledger = session.get(Delivery, (slug, f"shared_{slug}", library))
    return _delivery(
        session,
        f"fixture:{run_id}:shared:{slug}:{library}",
        f"shared_{slug}",
        slug,
        library,
        ledger.rating_key if ledger else 1,
        report.delivered_at or run.started_at,
        [dict(p) for p in report.picks],
        audience=report.audience,
        muted=report.muted or [],
        shared=True,
    )


def _delivery(session, source, owner, slug, library, rating_key, when, picks, **extra):
    previous = session.scalar(select(RowDeliverySnapshot).where(RowDeliverySnapshot.source_key == source))
    if previous is not None:
        previous.picks = picks
        previous.delivered_at = when
        for name, value in extra.items():
            setattr(previous, name, value)
    else:
        previous = RowDeliverySnapshot(
            source_key=source,
            user_slug=owner,
            collection_slug=slug,
            library_key=library,
            rating_key=rating_key,
            delivered_at=when,
            picks=picks,
            **extra,
        )
        session.add(previous)
    session.flush()
    timeline = list(
        session.scalars(
            select(RowDeliverySnapshot)
            .where(
                RowDeliverySnapshot.user_slug == owner,
                RowDeliverySnapshot.collection_slug == slug,
                RowDeliverySnapshot.library_key == library,
            )
            .order_by(RowDeliverySnapshot.delivered_at, RowDeliverySnapshot.id)
        )
    )
    for position, snapshot in enumerate(timeline):
        snapshot.ended_at = timeline[position + 1].delivered_at if position + 1 < len(timeline) else None
    return previous
