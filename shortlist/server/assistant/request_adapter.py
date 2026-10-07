"""Bounded request inbox changes and non-replayable acquisition dispatch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from loguru import logger
from pydantic import Field, model_validator
from sqlalchemy import select, text

from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.requests import request_titles_by_row
from shortlist.server.assistant_auth import Capability
from shortlist.server.assistant_auth.repository import StoredGrantIdentity, require_current_grant_in_session
from shortlist.server.db.models import Collection, Event, Job, RequestCandidate, Server, Setting
from shortlist.server.services import jobs
from shortlist.server.services.context_builder import ContextBuilder, row_request_overrides
from shortlist.server.services.request_actions import (
    MAX_ASSISTANT_REQUESTS,
    apply_request_action_in_session,
    build_request_send_payload,
    candidate_resources,
    ensure_request_claims_available,
    finish_candidate_in_session,
    missing_title,
    normalize_candidate_ids,
    request_candidates_in_session,
    reserve_request_dispatches,
)
from shortlist.server.settings_store import SettingsStore

from .changes import AccessRequirements, DomainPlan, DomainResult, EffectIntent, fingerprint
from .contracts import StrictModel
from .operation_models import AssistantChange, AssistantOperation, AssistantRequestDispatch
from .policy import AuthGrantPolicy, ChangeError


class RequestIntent(StrictModel):
    action: Literal["reject", "restore", "archive", "send"]
    candidate_ids: list[int] = Field(min_length=1, max_length=MAX_ASSISTANT_REQUESTS)

    @model_validator(mode="after")
    def validate_ids(self) -> RequestIntent:
        normalize_candidate_ids(self.candidate_ids)
        return self


def _candidate_snapshot(session, ids: list[int]) -> str:
    rows = session.scalars(select(RequestCandidate).where(RequestCandidate.id.in_(ids)).order_by(RequestCandidate.id))
    return fingerprint(
        [
            {column.name: repr(getattr(row, column.name)) for column in RequestCandidate.__table__.columns}
            for row in rows
        ]
    )


def _settings_revision(session) -> str:
    return fingerprint({row.key: row.value for row in session.scalars(select(Setting).order_by(Setting.key))})


def _request_config(state, session):
    return ContextBuilder._build_requests(SettingsStore(session, state.secrets))


class RequestAdapter:
    kind = "requests"

    def __init__(self, state) -> None:
        self.state = state

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = RequestIntent.model_validate(intent)
        dependencies = {"candidates": _candidate_snapshot(session, body.candidate_ids)}
        if body.action == "send":
            cfg = _request_config(self.state, session)
            if cfg is None:
                raise ValueError("requests are not configured")
            ensure_request_claims_available(session, request_candidates_in_session(session, body.candidate_ids))
            payload, resources = build_request_send_payload(session, body.candidate_ids, cfg)
            payload["settings_revision"] = _settings_revision(session)
            for entry in payload["entries"]:
                entry["candidate_revision"] = _candidate_snapshot(session, [entry["candidate_id"]])
            dependencies["dispatch"] = fingerprint(payload)
            effects = (EffectIntent("assistant.request_send", payload, "request-send", max_attempts=1),)
            capabilities = (Capability.REQUESTS_SEND.value,)
            summary = {
                "description": f"Send {payload['count']} explicit pending title(s) for acquisition.",
                "configuration_diff": {},
                "candidate_ids": list(body.candidate_ids),
                "destinations": payload["destinations"],
                "count": payload["count"],
                "titles": [
                    {"candidate_id": entry["candidate_id"], "title": entry["title"], "target": entry["target"]}
                    for entry in payload["entries"]
                ],
                "retry_policy": (
                    "Each title is recorded before dispatch. An interrupted or uncertain external call is never "
                    "sent again automatically."
                ),
            }
        else:
            rows = session.query(RequestCandidate).filter(RequestCandidate.id.in_(body.candidate_ids)).all()
            if len(rows) != len(body.candidate_ids):
                raise ValueError("one or more request candidates do not exist")
            resources = candidate_resources(session, rows)
            effects = ()
            capabilities = (Capability.REQUESTS_MANAGE.value,)
            summary = {
                "description": f"{body.action.title()} {len(rows)} explicit request candidate(s).",
                "configuration_diff": {str(row.id): {"before": row.status, "after": body.action} for row in rows},
                "candidate_ids": list(body.candidate_ids),
            }
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=capabilities,
                row_ids=resources["row_ids"],
                person_ids=resources["person_ids"],
                destination_ids=resources["destination_ids"],
                library_keys=resources["library_keys"],
                dynamic_libraries=resources["dynamic_libraries"],
                requires_approval=resources["unresolved_provenance"],
                batch_size=len(body.candidate_ids),
                work_units=len(body.candidate_ids),
            ),
            effects=effects,
            summary=summary,
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = RequestIntent.model_validate(intent)
        if body.action == "send":
            return DomainResult(
                result={"candidate_ids": body.candidate_ids, "dispatch_pending": True},
                audit_diff={"send_reserved": body.candidate_ids},
            )
        result = apply_request_action_in_session(session, body.action, body.candidate_ids)
        return DomainResult(result=result, audit_diff={body.action: body.candidate_ids})

    def stage_operation(self, session, operation: AssistantOperation, intent: dict) -> None:
        body = RequestIntent.model_validate(intent)
        if body.action != "send":
            return
        rows = request_candidates_in_session(session, body.candidate_ids)
        ensure_request_claims_available(session, rows)
        payload, _ = build_request_send_payload(session, body.candidate_ids, _request_config(self.state, session))
        for entry in payload["entries"]:
            entry["candidate_revision"] = _candidate_snapshot(session, [entry["candidate_id"]])
        reserve_request_dispatches(session, rows, payload["entries"], operation_id=operation.id)


def _authorized_operation(session, job_id: int) -> tuple[Job, AssistantOperation]:
    job = session.get(Job, job_id)
    if job is None or not job.operation_id:
        raise RuntimeError("Request dispatch has no correlated operation.")
    operation = session.get(AssistantOperation, job.operation_id)
    change = session.get(AssistantChange, operation.change_id) if operation else None
    if operation is None or change is None or change.kind != "requests":
        raise RuntimeError("Request dispatch has no saved request plan.")
    owner = session.scalar(select(Server.owner_account_id).limit(1))
    if owner is None:
        raise PermissionError("The verified owner is no longer available.")
    identity = StoredGrantIdentity(
        grant_id=change.grant_id,
        owner_account_id=change.owner_account_id,
        client_id=change.client_id,
        revision=change.grant_revision,
    )
    exact = operation.authorization_basis == "operation_approval"
    grant = require_current_grant_in_session(session, identity, check_revision=exact, current_owner_account_id=owner)
    original_scopes = frozenset(Capability(value) for value in operation.result.get("_effective_capabilities", []))
    grant = replace(grant, capabilities=grant.capabilities & original_scopes)
    if not exact:
        AuthGrantPolicy().authorize(grant, AccessRequirements(**change.requirements))
    return job, operation


def _begin_dispatch(state, job_id: int, entry: dict, settings_revision: str):
    """Freeze authority, credentials and target in the same SQLite snapshot as the dispatch marker."""
    from shortlist.server.services.run_service import force_dry_run

    with state.sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(Job, job_id)
        dispatch = session.scalar(
            select(AssistantRequestDispatch).where(
                AssistantRequestDispatch.operation_id == (job.operation_id if job else None),
                AssistantRequestDispatch.candidate_id == entry["candidate_id"],
            )
        )
        if dispatch is None or dispatch.request_body != entry:
            raise RuntimeError("Request dispatch has no matching committed title reservation.")
        if dispatch.status in {"succeeded", "refused", "outcome_unknown"}:
            return dispatch.id, dispatch.status, None, None
        if dispatch.status == "external_started":
            dispatch.status = "outcome_unknown"
            dispatch.result = {"detail": "An earlier acquisition may have completed; it will not be repeated."}
            dispatch.finished_at = datetime.now(UTC)
            session.commit()
            return dispatch.id, dispatch.status, None, None
        try:
            if getattr(state, "assistant_auth", None) is None:
                raise PermissionError("Assistant integration is disabled.")
            _authorized_operation(session, job_id)
            if force_dry_run():
                raise ValueError("Acquisition is disabled by deployment safe mode.")
            if _settings_revision(session) != settings_revision:
                raise ValueError("Request configuration changed after approval.")
            if _candidate_snapshot(session, [entry["candidate_id"]]) != entry["candidate_revision"]:
                raise ValueError("Request candidate changed after approval.")
            cfg = _request_config(state, session)
            if cfg is None:
                raise ValueError("Requests are no longer configured.")
            current, _ = build_request_send_payload(session, [entry["candidate_id"]], cfg)
            frozen = {key: value for key, value in entry.items() if key != "candidate_revision"}
            if current["entries"][0] != frozen:
                raise ValueError("The approved request target or title changed.")
            row = session.scalar(select(Collection).where(Collection.slug == entry.get("row_slug")))
            effective = resolve_request_config(cfg, row_request_overrides(row)) if row is not None else cfg
            metadata_key = str(SettingsStore(session, state.secrets).get("tmdb.apikey") or "")
        except (PermissionError, ChangeError, ValueError):
            dispatch.status = "refused"
            dispatch.result = {
                "detail": "Current ownership, authority, safe mode or saved configuration prevents dispatch."
            }
            dispatch.finished_at = datetime.now(UTC)
            session.commit()
            return dispatch.id, dispatch.status, None, None
        dispatch.status = "external_started"
        dispatch.external_started_at = datetime.now(UTC)
        session.commit()
        return dispatch.id, "external_started", effective, metadata_key


def _send_once(cfg, metadata_key: str, entry: dict, batch=None):
    """Reuse the engine's target, exclusion, identity and safe non-idempotent HTTP rules."""
    from shortlist.engine.clients.tmdb import TmdbClient

    tmdb = TmdbClient(metadata_key) if metadata_key else None
    key = entry.get("row_slug") or ""
    report = request_titles_by_row({key: cfg}, tmdb, [(key, missing_title(entry["title"]))], dry_run=False, batch=batch)
    if not report.outcomes:
        raise RuntimeError("The acquisition service returned no outcome.")
    return report.outcomes[0]


def _finish_dispatch(state, dispatch_id: int, *, status: str, result: dict, candidate_outcome=None) -> None:
    with state.sessions() as session:
        dispatch = session.get(AssistantRequestDispatch, dispatch_id)
        if dispatch is None:
            raise RuntimeError("Request dispatch disappeared.")
        dispatch.status = status
        dispatch.result = result
        dispatch.finished_at = datetime.now(UTC)
        if candidate_outcome is not None:
            finish_candidate_in_session(session, dispatch.candidate_id, candidate_outcome)
        session.add(
            Event(
                scope="assistant.requests.send",
                level="warning" if status == "outcome_unknown" else "info",
                message={
                    "operation_id": dispatch.operation_id,
                    "candidate_id": dispatch.candidate_id,
                    "status": status,
                },
            )
        )
        session.commit()


@jobs.handler("assistant.request_send")
async def dispatch_assistant_requests(state, payload: dict, *, job_id: int) -> dict:
    """Recover durable title claims; never repeat an uncertain external attempt."""
    import asyncio
    from types import SimpleNamespace

    from shortlist.engine.requests import RequestBatch

    batch = RequestBatch()

    async def send(entry):
        dispatch_id, status, cfg, metadata_key = await asyncio.to_thread(
            _begin_dispatch, state, job_id, entry, payload["settings_revision"]
        )
        if cfg is None:
            return {"candidate_id": entry["candidate_id"], "status": status}
        try:
            outcome = await asyncio.to_thread(_send_once, cfg, metadata_key, entry, batch)
            if outcome.status == "requested":
                status, detail = "succeeded", "The acquisition service accepted this title."
            elif outcome.status == "skipped_content":
                # This reason is generated locally by the content policy, not a vendor exception.
                status, detail = "refused", outcome.detail
            elif outcome.status.startswith("skipped_"):
                status, detail = (
                    "refused",
                    "The service's existing-title, exclusion or identity checks prevented acquisition.",
                )
            else:
                status, detail = (
                    "outcome_unknown",
                    "The acquisition outcome is uncertain. It will not be repeated automatically.",
                )
            safe_outcome = SimpleNamespace(status=outcome.status, detail=detail, arr_slug=None)
            await asyncio.to_thread(
                _finish_dispatch,
                state,
                dispatch_id,
                status=status,
                result={"detail": detail, "status": status},
                candidate_outcome=safe_outcome,
            )
        except Exception as exc:
            status = "outcome_unknown"
            logger.warning("Assistant acquisition outcome unknown ({})", type(exc).__name__)
            await asyncio.to_thread(
                _finish_dispatch,
                state,
                dispatch_id,
                status=status,
                result={"detail": "The acquisition may have completed. No retry is authorized."},
            )
        return {"candidate_id": entry["candidate_id"], "status": status}

    outcomes = []
    for entry in payload.get("entries", []):
        outcomes.append(await send(entry))
    with state.sessions() as session:
        job = session.get(Job, job_id)
        operation = session.get(AssistantOperation, job.operation_id)
        status = (
            "outcome_unknown"
            if any(item["status"] == "outcome_unknown" for item in outcomes)
            else ("completed" if outcomes and all(item["status"] == "succeeded" for item in outcomes) else "failed")
        )
        operation.status = status
        operation.result = {**operation.result, "dispatch_pending": False, "request_outcomes": outcomes}
        operation.finished_at = datetime.now(UTC)
        session.commit()
    return {"status": status, "outcomes": outcomes, "detail": f"Processed {len(outcomes)} explicit acquisitions."}


__all__ = ["RequestAdapter", "RequestIntent", "dispatch_assistant_requests"]
