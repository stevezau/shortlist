"""Bounded run, request and audit views without raw logs or personal traces."""

from __future__ import annotations

import re

from sqlalchemy import select

from shortlist.server.assistant_auth import AuthorizationDenied, Capability, require_authorized
from shortlist.server.db.models import Collection, Event, RequestCandidate, Run, RunSharedRow, RunUser, User, iso_utc

from .contracts import ToolResult
from .discovery import _ids, _page
from .operation_models import AssistantChange, AssistantOperation

_COMMON_HISTORY_REASON = re.compile(
    r"No title has been watched by (?P<threshold>[1-9][0-9]*) or more of the "
    r"[1-9][0-9]* (?:person|people) in this row's audience yet\. "
    r"A shared row is built only from titles several people have watched, "
    r"so it needs (?P=threshold) of them with some viewing in common\."
)
_SMALL_AUDIENCE_REASON = re.compile(
    r"A shared row needs at least [1-9][0-9]* people with overlapping viewing, "
    r"but only [1-9][0-9]* (?:person|people) (?:is|are) in this row's audience and active in runs "
    r"\(enabled, not paused\) — so it can never build\. Add more people to the audience, "
    r"or make this a per-person row so each of them gets their own\."
)


def shared_outcome_view(result: RunSharedRow, row_id: int | None) -> dict:
    """Project only known outcome categories, never persisted free-form reasons."""
    pick_count = len(result.picks or [])
    reason_code = guidance = None
    if result.status == "ok" and not result.error and pick_count == 0:
        reason_code = "no_picks"
        guidance = (
            "No titles were selected. Review the row's libraries, filters, active season and audience, "
            "or ask the owner to inspect this run in Runs."
        )
    elif result.status == "skipped" or result.error:
        reason = result.reason or ""
        reason_code = "owner_review_required"
        guidance = "The owner should inspect this run in Runs for details; private error and history text is omitted."
        if result.status == "skipped" and not result.error and len(reason) <= 1024:
            if _COMMON_HISTORY_REASON.fullmatch(reason):
                reason_code = "insufficient_common_history"
                guidance = (
                    "No common watch-history seeds met this row's minimum-watchers rule. "
                    "Review its minimum watchers and enabled audience, or use a per-person row."
                )
            elif _SMALL_AUDIENCE_REASON.fullmatch(reason):
                reason_code = "insufficient_active_audience"
                guidance = (
                    "The active audience is smaller than this shared row's minimum-watchers rule. "
                    "Review its enabled audience and minimum watchers, or use a per-person row."
                )
            elif reason == ("Nobody in this row's audience is enabled, so there was no history to build it from."):
                reason_code = "no_enabled_audience"
                guidance = "Enable the intended people in this row's audience before rebuilding it."
            elif reason == "An AI row can't be shared: it is built per person. Make it a per-person row.":
                reason_code = "incompatible_shared_template"
                guidance = "This AI row must use per-person mode; review its template and mode before rebuilding it."
    return {
        "row_id": row_id,
        "row_slug": result.collection_slug,
        "status": result.status,
        "pick_count": pick_count,
        "has_error": bool(result.error),
        "reason_code": reason_code,
        "guidance": guidance,
    }


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
        row_ids = dict(session.execute(select(Collection.slug, Collection.id).where(Collection.slug.in_(rows))).all())
        shared = [
            shared_outcome_view(result, row_ids.get(result.collection_slug))
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
