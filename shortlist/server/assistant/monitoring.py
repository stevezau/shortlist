"""Bounded run, request and audit views without raw logs or personal traces."""

from __future__ import annotations

from sqlalchemy import select

from shortlist.server.assistant_auth import AuthorizationDenied, Capability, require_authorized
from shortlist.server.db.models import Collection, Event, RequestCandidate, Run, RunSharedRow, RunUser, User, iso_utc

from .contracts import ToolResult
from .discovery import _ids, _page
from .operation_models import AssistantChange, AssistantOperation


class MonitoringService:
    def __init__(self, state) -> None:
        self.state = state

    @staticmethod
    def _allowed(session, principal) -> tuple[set[int], set[str]]:
        people = set(session.scalars(select(User.id)))
        rows = set(
            session.scalars(
                _ids(
                    select(Collection.slug),
                    Collection.id,
                    principal.constraints.row_ids,
                    principal.constraints.include_future_rows,
                )
            )
        )
        return people, rows

    @staticmethod
    def _report(session, run: Run, people: set[int], rows: set[str]) -> dict | None:
        person_results = []
        for result in session.scalars(select(RunUser).where(RunUser.run_id == run.id, RunUser.user_id.in_(people))):
            considered = {slug: value for slug, value in (result.rows_considered or {}).items() if slug in rows}
            if not considered:
                continue
            person_results.append(
                {
                    "person_id": result.user_id,
                    "status": result.status,
                    "rows_considered": considered,
                    "has_error": bool(result.error),
                }
            )
        shared = [
            {"row_slug": result.collection_slug, "status": result.status, "has_error": bool(result.error)}
            for result in session.scalars(
                select(RunSharedRow).where(RunSharedRow.run_id == run.id, RunSharedRow.collection_slug.in_(rows))
            )
        ]
        expected = (run.stats or {}).get("expected_rows", [])
        selected_expected = [item["slug"] for item in expected if isinstance(item, dict) and item.get("slug") in rows]
        if not person_results and not shared and not selected_expected:
            return None
        status = run.status
        if run.trigger == "assistant":
            from .run_spend import run_usage

            usage = run_usage(session, run.id, (run.stats or {}).get("assistant_actor", {}).get("operation_id"))
            if usage["outcome_unknown"] or (run.stats or {}).get("assistant_outcome_unknown"):
                status = "outcome_unknown"
            elif usage["stopped"]:
                status = "partially_applied"
        return {
            "run_id": run.id,
            "status": status,
            "dry_run": run.dry_run,
            "queued_at": iso_utc(run.started_at),
            "began_at": iso_utc(run.began_at),
            "finished_at": iso_utc(run.finished_at),
            "people": person_results,
            "shared_rows": shared,
            "expected_row_slugs": selected_expected,
            "scope": "Only permitted row and person outcomes are shown.",
        }

    def runs(self, principal, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.ACTIVITY_READ])
        with self.state.sessions() as session:
            people, rows = self._allowed(session, principal)
            reports = [
                view
                for run in session.scalars(select(Run).order_by(Run.id.desc()).limit(1000))
                if (view := self._report(session, run, people, rows)) is not None
            ]
        return ToolResult(
            summary="Recent run outcomes restricted to permitted people and rows.",
            data=_page(reports, limit, offset),
            warnings=[
                "At most the newest 1,000 runs are scanned. Raw logs, seed titles and history traces are omitted."
            ],
        )

    def run(self, principal, run_id: int) -> ToolResult:
        require_authorized(principal, [Capability.ACTIVITY_READ])
        with self.state.sessions() as session:
            people, rows = self._allowed(session, principal)
            run = session.get(Run, run_id)
            report = self._report(session, run, people, rows) if run is not None else None
            if report is None:
                raise AuthorizationDenied("run report is not available to this connection")
        return ToolResult(
            summary="Run status and permitted outcome categories.",
            data=report,
            warnings=[
                "Error details and personal traces remain in the owner's browser; no private title history is exported."
            ],
        )

    def activity(self, principal, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.ACTIVITY_READ])
        with self.state.sessions() as session:
            operation_ids = set(
                session.scalars(select(AssistantOperation.id).where(AssistantOperation.grant_id == principal.grant_id))
            )
            change_ids = set(
                session.scalars(select(AssistantChange.id).where(AssistantChange.grant_id == principal.grant_id))
            )
            entries = []
            for event in session.scalars(
                select(Event).where(Event.scope.like("assistant.%")).order_by(Event.id.desc()).limit(1000)
            ):
                message = event.message or {}
                if message.get("operation_id") not in operation_ids and message.get("change_id") not in change_ids:
                    continue
                entries.append(
                    {
                        "event_id": event.id,
                        "at": iso_utc(event.ts),
                        "action": event.scope,
                        "level": event.level,
                        "operation_id": message.get("operation_id"),
                        "change_id": message.get("change_id"),
                    }
                )
        return ToolResult(
            summary="This connection's redacted plan and operation audit trail.", data=_page(entries, limit, offset)
        )

    def requests(self, principal, *, limit: int = 25, offset: int = 0) -> ToolResult:
        require_authorized(principal, [Capability.REQUESTS_READ])
        with self.state.sessions() as session:
            _people, rows = self._allowed(session, principal)
            entries = []
            for candidate in session.scalars(
                select(RequestCandidate)
                .where(RequestCandidate.hidden.is_(False))
                .order_by(RequestCandidate.id.desc())
                .limit(1000)
            ):
                if candidate.row_slug not in rows and not (
                    candidate.row_slug is None and principal.constraints.include_future_rows
                ):
                    continue
                entries.append(
                    {
                        "id": candidate.id,
                        "tmdb_id": candidate.tmdb_id,
                        "media": candidate.media_type,
                        "title": candidate.title,
                        "year": candidate.year,
                        "status": candidate.status,
                        "rating": candidate.rating,
                        "vote_count": candidate.vote_count,
                        "excluded": candidate.excluded,
                    }
                )
        return ToolResult(
            summary="Permitted acquisition candidates and their recorded status.",
            data=_page(entries, limit, offset),
            warnings=[
                "Personal request provenance and seed titles are omitted. Recorded status may lag the external service."
            ],
        )
