"""Transaction-owned request inbox actions and frozen acquisition payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, MissingTitle, RequestConfig
from shortlist.engine.request_config import resolve_request_config
from shortlist.server.db.models import Collection, Event, RequestCandidate, User

MAX_ASSISTANT_REQUESTS = 25

__all__ = [
    "MAX_ASSISTANT_REQUESTS",
    "AutomaticRequestGuard",
    "acquisition_review_token",
    "apply_request_action_in_session",
    "build_request_send_payload",
    "durable_handled_requests",
    "finish_candidate_in_session",
    "finish_request_dispatch",
    "missing_title",
    "normalize_candidate_ids",
    "recover_abandoned_request_dispatches",
    "request_candidates_in_session",
    "reserve_request_dispatches",
    "start_manual_request_dispatch",
]


def normalize_candidate_ids(ids: list[int]) -> tuple[int, ...]:
    if not ids or len(ids) > MAX_ASSISTANT_REQUESTS or any(type(value) is not int or value <= 0 for value in ids):
        raise ValueError(f"choose 1 to {MAX_ASSISTANT_REQUESTS} positive request candidate IDs")
    if len(set(ids)) != len(ids):
        raise ValueError("request candidate IDs must not be repeated")
    return tuple(ids)


def request_candidates_in_session(session: Session, ids: list[int] | tuple[int, ...]) -> list[RequestCandidate]:
    wanted = normalize_candidate_ids(list(ids))
    rows = session.query(RequestCandidate).filter(RequestCandidate.id.in_(wanted)).all()
    by_id = {row.id: row for row in rows}
    missing = [candidate_id for candidate_id in wanted if candidate_id not in by_id]
    if missing:
        raise ValueError(f"unknown request candidate IDs: {missing}")
    return [by_id[candidate_id] for candidate_id in wanted]


def apply_request_action_in_session(session: Session, action: str, ids: list[int]) -> dict:
    """Apply a local inbox decision without committing."""
    rows = request_candidates_in_session(session, ids)
    changed = 0
    if action == "reject":
        for row in rows:
            if row.status != "rejected":
                row.status = "rejected"
                changed += 1
    elif action == "restore":
        for row in rows:
            if row.status == "rejected":
                row.status = "pending"
                changed += 1
    elif action == "archive":
        for row in rows:
            if row.status == "sent":
                if not row.hidden:
                    row.hidden = True
                    changed += 1
            else:
                session.delete(row)
                changed += 1
    else:
        raise ValueError(f"unknown request action {action!r}")
    session.add(Event(scope=f"requests.{action}", level="info", message={"ids": list(ids), "count": changed}))
    session.flush()
    return {"action": action, "changed": changed, "candidate_ids": list(ids)}


def _target_snapshot(cfg: RequestConfig, media_type: str) -> dict:
    if cfg.target == "overseerr":
        target = cfg.overseerr
        return {
            "service": "overseerr",
            "destination": _destination(target.url) if target else "",
            "configured": target is not None,
            "request_as_user_id": target.request_as_user_id if target else 0,
        }
    target = cfg.radarr if media_type == "movie" else cfg.sonarr
    return {
        "service": "radarr" if media_type == "movie" else "sonarr",
        "destination": _destination(target.url) if target else "",
        "configured": target is not None,
        "quality_profile_id": target.quality_profile_id if target else None,
        "root_folder": target.root_folder if target else None,
        "tag": target.tag if target else "",
        "monitor": cfg.sonarr_monitor if media_type == "show" else None,
    }


def _destination(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Request destinations must be configured as credential-free HTTP service URLs.")
    return url.rstrip("/")


def candidate_resources(session: Session, rows: list[RequestCandidate]) -> dict:
    """Resolve every contributor and row; never silently drop unknown historical provenance."""
    people_by_slug = {user.slug: user.id for user in session.query(User).all()}
    rows_by_slug = {row.slug: row for row in session.query(Collection).all()}
    people, selected_rows, libraries = set(), set(), set()
    unresolved = False
    dynamic_libraries = False
    for candidate in rows:
        row = rows_by_slug.get(candidate.row_slug)
        if row is None:
            unresolved = True
        else:
            selected_rows.add(row.id)
            libraries.update(str(key) for key in row.library_keys or [])
            dynamic_libraries |= not row.library_keys
        for slug in candidate.wanters or []:
            if slug not in people_by_slug:
                unresolved = True
            else:
                people.add(people_by_slug[slug])
    return {
        "row_ids": tuple(sorted(selected_rows)),
        "person_ids": tuple(sorted(people)),
        "library_keys": tuple(sorted(libraries)),
        "dynamic_libraries": dynamic_libraries,
        "unresolved_provenance": unresolved,
        "destination_ids": (),
    }


def ensure_request_claims_available(session: Session, rows: list[RequestCandidate]) -> None:
    """A fresh operation cannot replay an outstanding or uncertain title acquisition."""
    entries = [
        {"candidate_id": row.id, "title": {"tmdb_id": row.tmdb_id, "media_type": row.media_type}} for row in rows
    ]
    ensure_request_entries_available(session, entries)


def ensure_request_entries_available(session: Session, entries: list[dict]) -> None:
    """Compare immutable title identities even after the inbox candidate was archived."""
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    if not entries:
        return
    ids = [entry["candidate_id"] for entry in entries if entry.get("candidate_id") is not None]
    conditions = [AssistantRequestDispatch.candidate_id.in_(ids)] if ids else []
    conditions.extend(
        and_(
            AssistantRequestDispatch.request_body["title"]["tmdb_id"].as_integer() == entry["title"]["tmdb_id"],
            AssistantRequestDispatch.request_body["title"]["media_type"].as_string() == entry["title"]["media_type"],
        )
        for entry in entries
    )
    exists = session.scalar(
        select(AssistantRequestDispatch.id)
        .where(
            AssistantRequestDispatch.status.in_(("reserved", "external_started", "outcome_unknown", "succeeded")),
            or_(*conditions),
        )
        .limit(1)
    )
    if exists is not None:
        raise ValueError(
            "A selected title already has a reserved, completed or uncertain acquisition. "
            "Review its operation before retrying."
        )


def _effective_config(session: Session, base: RequestConfig, row_slug: str | None) -> RequestConfig:
    from shortlist.server.services.context_builder import row_request_overrides

    row = session.query(Collection).filter_by(slug=row_slug).first() if row_slug else None
    return resolve_request_config(base, row_request_overrides(row)) if row is not None else base


def build_request_send_payload(session: Session, ids: list[int], base: RequestConfig) -> tuple[dict, dict]:
    """Freeze title bodies and non-secret destinations for one reviewed send."""
    if not base.enabled:
        raise ValueError("requests are disabled")
    rows = request_candidates_in_session(session, ids)
    not_pending = [row.id for row in rows if row.status != "pending"]
    if not_pending:
        raise ValueError(f"only pending candidates may be sent: {not_pending}")
    resources = candidate_resources(session, rows)
    entries = []
    destinations: set[str] = set()
    for row in rows:
        cfg = _effective_config(session, base, row.row_slug)
        target = _target_snapshot(cfg, row.media_type)
        if not target["configured"]:
            raise ValueError(f"request candidate {row.id} has no configured {target['destination']} destination")
        destinations.add(target["destination"])
        entries.append(request_send_entry(session, row, base))
    payload = {"entries": entries, "count": len(entries), "destinations": sorted(destinations)}
    resources["destination_ids"] = tuple(sorted(destinations))
    return payload, resources


def request_send_entry(session: Session, row: RequestCandidate, base: RequestConfig) -> dict:
    """Freeze the non-secret request body for either owner or assistant dispatch."""
    return {
        "candidate_id": row.id,
        "row_slug": row.row_slug,
        "title": {
            "tmdb_id": row.tmdb_id,
            "title": row.title,
            "media_type": row.media_type,
            "year": row.year,
            "rating": row.rating,
            "vote_count": row.vote_count,
            "language": row.language or "",
            "demand": row.demand,
            "tags": sorted(row.tags or []),
        },
        "target": _target_snapshot(_effective_config(session, base, row.row_slug), row.media_type),
    }


def reserve_request_dispatches(
    session: Session,
    rows: list[RequestCandidate],
    entries: list[dict],
    *,
    operation_id: str | None = None,
    origin: str = "assistant",
) -> list:
    """Claim a whole explicit batch under the caller's BEGIN IMMEDIATE transaction.

    Explicit inbox send entry points use this lock and ledger before I/O. NULL operation IDs retain
    manual claims, and SET NULL on operation deletion preserves uncertain external outcomes.
    """
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    if origin not in {"assistant", "manual"} or (origin == "assistant" and not operation_id):
        raise ValueError("Invalid acquisition origin")
    if {row.id for row in rows} != {entry["candidate_id"] for entry in entries}:
        raise ValueError("Acquisition entries do not match selected candidates")
    ensure_request_claims_available(session, rows)
    claims = [
        AssistantRequestDispatch(
            operation_id=operation_id,
            origin=origin,
            candidate_id=entry["candidate_id"],
            destination=entry["target"]["destination"],
            request_body=entry,
            status="reserved",
        )
        for entry in entries
    ]
    session.add_all(claims)
    session.flush()
    return claims


def start_manual_request_dispatch(session: Session, dispatch_id: int) -> dict | None:
    """Persist before-call uncertainty; the caller commits before contacting a vendor."""
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    claim = session.get(AssistantRequestDispatch, dispatch_id)
    if claim is None or claim.origin != "manual" or claim.status != "reserved":
        raise ValueError("Manual acquisition claim is not available")
    row = session.get(RequestCandidate, claim.candidate_id)
    title = claim.request_body["title"]
    if (
        row is None
        or row.status != "pending"
        or (row.tmdb_id, row.media_type) != (title["tmdb_id"], title["media_type"])
    ):
        claim.status = "refused"
        claim.finished_at = datetime.now(UTC)
        claim.result = {"detail": "The selected candidate is no longer pending."}
        return None
    claim.status = "external_started"
    claim.external_started_at = datetime.now(UTC)
    return claim.request_body


def finish_request_dispatch(session: Session, dispatch_id: int, outcome) -> None:
    """Finish a manual claim conservatively; uncertain errors never permit another send."""
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    claim = session.get(AssistantRequestDispatch, dispatch_id)
    if claim is None:
        raise ValueError("Acquisition claim disappeared")
    claim.status = (
        "succeeded"
        if outcome.status == "requested"
        else "refused"
        if outcome.status.startswith("skipped_")
        else "outcome_unknown"
    )
    claim.finished_at = datetime.now(UTC)
    claim.result = {"status": claim.status, "detail": outcome.detail}
    finish_candidate_in_session(session, claim.candidate_id, outcome)


class AutomaticRequestGuard:
    """A live durable acquisition gate injected into engine contexts by the server.

    The engine supplies its already-resolved target. The database transaction ends before yielding;
    failure or interruption after this checkpoint remains uncertain and cannot be sent again.
    """

    def __init__(self, sessions):
        self.sessions = sessions

    def __call__(self, row_slug: str, title: MissingTitle, cfg: RequestConfig):
        from contextlib import contextmanager

        from sqlalchemy import text

        from shortlist.server.assistant.operation_models import AssistantRequestDispatch

        @contextmanager
        def claim():
            body = {
                "candidate_id": None,
                "row_slug": row_slug,
                "title": {
                    "tmdb_id": title.tmdb_id,
                    "media_type": title.media_type.value,
                    "title": title.title,
                    "year": title.year,
                    "rating": title.rating,
                    "vote_count": title.vote_count,
                    "language": title.language,
                    "demand": title.demand,
                    "tags": sorted(title.tags),
                },
                "target": _target_snapshot(cfg, title.media_type.value),
            }
            blocked = False
            with self.sessions() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                try:
                    ensure_request_entries_available(session, [body])
                except ValueError:
                    blocked = True
                if not blocked:
                    row = session.scalar(
                        select(RequestCandidate).where(
                            RequestCandidate.tmdb_id == title.tmdb_id,
                            RequestCandidate.media_type == title.media_type.value,
                        )
                    )
                    if row is not None and row.status in {"sent", "rejected"}:
                        blocked = True
                    else:
                        body["candidate_id"] = row.id if row is not None else None
                        dispatch = AssistantRequestDispatch(
                            origin="automatic",
                            candidate_id=body["candidate_id"],
                            destination=body["target"]["destination"],
                            request_body=body,
                            status="external_started",
                            external_started_at=datetime.now(UTC),
                        )
                        session.add(dispatch)
                        session.flush()
                        dispatch_id = dispatch.id
                        session.commit()
            if blocked:
                yield None
                return
            recorded = False

            def record(outcome):
                nonlocal recorded
                if recorded:
                    raise ValueError("An acquisition outcome can only be recorded once")
                with self.sessions() as session:
                    finish_request_dispatch(session, dispatch_id, outcome)
                    session.commit()
                recorded = True

            try:
                yield record
            finally:
                if not recorded:
                    with self.sessions() as session:
                        dispatch = session.get(AssistantRequestDispatch, dispatch_id)
                        dispatch.status = "outcome_unknown"
                        dispatch.finished_at = datetime.now(UTC)
                        dispatch.result = {"detail": "The acquisition outcome is uncertain; it will not be repeated."}
                        session.commit()

        return claim()


def durable_handled_requests(session: Session) -> set[tuple[int, str]]:
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    bodies = session.scalars(
        select(AssistantRequestDispatch.request_body).where(
            AssistantRequestDispatch.status.in_(("reserved", "external_started", "outcome_unknown", "succeeded"))
        )
    )
    return {(body["title"]["tmdb_id"], body["title"]["media_type"]) for body in bodies}


def missing_title(body: dict) -> MissingTitle:
    return MissingTitle(
        tmdb_id=body["tmdb_id"],
        title=body["title"],
        media_type=MediaType(body["media_type"]),
        year=body["year"],
        rating=body["rating"],
        vote_count=body["vote_count"],
        language=body["language"],
        demand=body["demand"],
        tags=set(body["tags"]),
    )


def finish_candidate_in_session(session: Session, candidate_id: int, outcome) -> None:
    row = session.get(RequestCandidate, candidate_id) if candidate_id is not None else None
    if row is None:
        return
    row.detail = outcome.detail
    if outcome.arr_slug:
        row.arr_slug = outcome.arr_slug
    if outcome.status == "requested":
        row.status = "sent"
        row.sent_at = datetime.now(UTC)


def recover_abandoned_request_dispatches(sessions) -> int:
    """Run once during startup, before workers: an old process cannot still finish these calls."""
    from sqlalchemy import text

    from shortlist.server.assistant.operation_models import AssistantRequestDispatch

    changed = 0
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        claims = session.scalars(
            select(AssistantRequestDispatch).where(
                AssistantRequestDispatch.origin.in_(("manual", "automatic")),
                AssistantRequestDispatch.status.in_(("reserved", "external_started")),
            )
        )
        for claim in claims:
            claim.status = "outcome_unknown" if claim.status == "external_started" else "refused"
            claim.finished_at = datetime.now(UTC)
            claim.result = {"detail": "The previous server stopped before a final acquisition outcome was recorded."}
            changed += 1
        session.commit()
    return changed


def acquisition_review_token(claim) -> str:
    """Bind the browser decision to exact title, destination, status and outcome facts."""
    import hashlib
    import json

    facts = {
        "id": claim.id,
        "origin": claim.origin,
        "request_body": claim.request_body,
        "destination": claim.destination,
        "status": claim.status,
        "result": claim.result,
        "created_at": str(claim.created_at),
        "external_started_at": str(claim.external_started_at),
        "finished_at": str(claim.finished_at),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
