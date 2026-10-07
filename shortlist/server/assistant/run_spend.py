"""Durable per-call admission for a paid run, with no database lock over network I/O."""

from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from threading import Event

from sqlalchemy import func, select

from shortlist.engine.provider_calls import ProviderCall
from shortlist.server.assistant_auth import GrantConstraints
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.db.models import RequestCandidate, Run
from shortlist.server.services.request_actions import (
    _target_snapshot,
    ensure_request_entries_available,
    finish_request_dispatch,
)

from .budgets import AssistantBudget
from .operation_models import AssistantOperation, AssistantRequestDispatch, AssistantRunCall
from .policy import ChangeError
from .run_adapter import config_fingerprint, validate_execution_in_session


class RunSpendGuard:
    """Revalidate the approved contract immediately before each new external effect."""

    def __init__(self, state, run_id: int):
        self.state = state
        self.run_id = run_id
        # One guard instance is shared by every provider and acquisition callback in a run.
        # Admission must stop even when the database cannot record a post-I/O failure.
        self._halted = Event()

    def _current(self, session):
        if self._halted.is_set():
            raise ChangeError("outcome_unknown", "An external result could not be safely checkpointed.")
        run = session.get(Run, self.run_id)
        if run is None:
            raise ChangeError("missing_permission", "The run no longer exists.")
        stats = run.stats or {}
        operation_id = stats.get("assistant_actor", {}).get("operation_id")
        operation = session.get(AssistantOperation, operation_id) if operation_id else None
        usage = run_usage(session, self.run_id, operation_id)
        if stats.get("assistant_outcome_unknown") or usage["outcome_unknown"]:
            raise ChangeError("outcome_unknown", "A previous external call has an unknown outcome.")
        if (
            stats.get("assistant_stop_reason")
            or stats.get("cancel_requested")
            or run.status != "running"
            or operation is None
            or operation.status in {"failed", "cancelled"}
        ):
            raise ChangeError("missing_permission", "The run is stopped or is no longer active.")
        contract, _profiles = validate_execution_in_session(session, self.state, run)
        actor = run.stats["assistant_actor"]
        grant = session.get(AssistantGrant, actor["grant_id"])
        maximum = GrantConstraints.from_dict(grant.constraints).max_provider_calls
        budget = session.get(AssistantBudget, actor["grant_id"])
        reserved = budget.provider_calls_reserved if budget else 0
        if reserved > maximum or reserved < contract["intent"]["max_provider_calls"]:
            raise ChangeError("budget_exceeded", "The connection's reserved provider-call budget is no longer valid.")
        return run, contract, actor

    def _stop(self, session, run, code, *, unknown=False):
        if run is None:
            return
        stats = dict(run.stats or {})
        if unknown:
            self._halted.set()
            stats["assistant_outcome_unknown"] = True
        stats["assistant_stop_reason"] = code
        run.stats = stats
        cancel = getattr(self.state.run_service, "_cancels", {}).get(self.run_id)
        if cancel is not None:
            cancel.set()
        actor = stats.get("assistant_actor", {})
        operation = session.get(AssistantOperation, actor.get("operation_id")) if actor.get("operation_id") else None
        if operation is not None:
            if unknown or operation.status == "outcome_unknown":
                operation.status = "outcome_unknown"
            else:
                operation.status = "failed"

    @staticmethod
    def _check_descriptor(call, contract):
        if not isinstance(call, ProviderCall):
            raise ChangeError("missing_permission", "The provider call has no typed effect descriptor.")
        intent = contract["intent"]
        identity = {key: asdict(call)[key] for key in ("kind", "provider", "destination", "model")}
        permitted = [{key: item[key] for key in identity} for item in contract["spend"]["providers"]]
        if identity not in permitted:
            raise ChangeError(
                "missing_permission", "The provider, model, or destination was not approved for this run."
            )
        if call.kind in {"completion", "native_search"} and (
            type(call.output_tokens) is not int or not 0 < call.output_tokens <= intent["max_output_tokens"]
        ):
            raise ChangeError("budget_exceeded", "The provider output-token budget would be exceeded.")
        if call.kind == "native_search":
            managed = call.provider == "google" and intent["allow_provider_managed_search"]
            if not managed and (
                type(call.native_tool_uses) is not int
                or not 0 < call.native_tool_uses <= intent["max_native_tool_uses"]
            ):
                raise ChangeError("budget_exceeded", "The provider native-search budget would be exceeded.")

    @contextmanager
    def __call__(self, call: ProviderCall):
        error = None
        with self.state.sessions() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _run, contract, actor = self._current(session)
                self._check_descriptor(call, contract)
                count = session.scalar(
                    select(func.count()).select_from(AssistantRunCall).where(AssistantRunCall.run_id == self.run_id)
                )
                if count >= contract["intent"]["max_provider_calls"]:
                    raise ChangeError("budget_exceeded", "The run's provider-call budget is exhausted.")
                if call.kind == "image":
                    images = session.scalar(
                        select(func.count())
                        .select_from(AssistantRunCall)
                        .where(AssistantRunCall.run_id == self.run_id, AssistantRunCall.kind == "image")
                    )
                    if images >= contract["intent"]["max_images"]:
                        raise ChangeError("budget_exceeded", "The run's image budget is exhausted.")
                claim = AssistantRunCall(run_id=self.run_id, operation_id=actor["operation_id"], **asdict(call))
                session.add(claim)
                session.flush()
                claim_id = claim.id
            except (ValueError, PermissionError) as exc:
                error = exc
                code = getattr(exc, "code", "missing_permission")
                self._stop(session, session.get(Run, self.run_id), code, unknown=code == "outcome_unknown")
            session.commit()
        if error is not None:
            raise error
        unknown = True
        try:
            yield
            unknown = False
        finally:
            if unknown:
                self._halted.set()
            try:
                self._finish_provider(claim_id, unknown=unknown)
            except BaseException:
                self._halted.set()
                # A transient commit failure may permit a subsequent uncertainty checkpoint.
                # If it does not, the original started ledger remains for startup reconciliation.
                with suppress(Exception):
                    self._finish_provider(claim_id, unknown=True)
                raise

    def _finish_provider(self, claim_id, *, unknown):
        with self.state.sessions() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            claim = session.get(AssistantRunCall, claim_id)
            claim.status = "outcome_unknown" if unknown else "returned"
            claim.finished_at = datetime.now(UTC)
            run = session.get(Run, self.run_id)
            if unknown:
                self._stop(session, run, "outcome_unknown", unknown=True)
            session.commit()

    @contextmanager
    def acquisition(self, row_slug, title, cfg):
        """Reserve both the run allowance and shared title claim in the same transaction."""
        blocked = False
        error = None
        with self.state.sessions() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                _run, contract, actor = self._current(session)
                target = _target_snapshot(cfg, title.media_type.value)
                descriptor = {
                    "row_slug": row_slug,
                    "media": title.media_type.value,
                    "destination": target["destination"],
                    "config_hash": config_fingerprint(cfg),
                }
                if contract["effective_dry_run"] or descriptor not in contract["spend"]["acquisitions"]:
                    raise ChangeError(
                        "missing_permission", "This acquisition target or row was not approved for the run."
                    )
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
                    "target": target,
                }
                try:
                    ensure_request_entries_available(session, [body])
                except ValueError:
                    blocked = True
                candidate = session.scalar(
                    select(RequestCandidate).where(
                        RequestCandidate.tmdb_id == title.tmdb_id, RequestCandidate.media_type == title.media_type.value
                    )
                )
                blocked = blocked or (candidate is not None and candidate.status in {"sent", "rejected"})
                if not blocked:
                    count = session.scalar(
                        select(func.count())
                        .select_from(AssistantRequestDispatch)
                        .where(
                            AssistantRequestDispatch.operation_id == actor["operation_id"],
                            AssistantRequestDispatch.origin == "assistant_run",
                        )
                    )
                    if count >= contract["intent"]["max_acquisitions"]:
                        raise ChangeError("budget_exceeded", "The run's acquisition budget is exhausted.")
                    body["candidate_id"] = candidate.id if candidate else None
                    claim = AssistantRequestDispatch(
                        operation_id=actor["operation_id"],
                        origin="assistant_run",
                        candidate_id=body["candidate_id"],
                        destination=target["destination"],
                        request_body=body,
                        status="external_started",
                        external_started_at=datetime.now(UTC),
                    )
                    session.add(claim)
                    session.flush()
                    claim_id = claim.id
            except (ValueError, PermissionError) as exc:
                error = exc
                code = getattr(exc, "code", "missing_permission")
                self._stop(session, session.get(Run, self.run_id), code, unknown=code == "outcome_unknown")
            session.commit()
        if error is not None:
            raise error
        if blocked:
            yield None
            return
        recorded = False

        def record(outcome):
            nonlocal recorded
            if recorded:
                raise ValueError("An acquisition outcome can only be recorded once")
            try:
                with self.state.sessions() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    finish_request_dispatch(session, claim_id, outcome)
                    if session.get(AssistantRequestDispatch, claim_id).status == "outcome_unknown":
                        self._stop(session, session.get(Run, self.run_id), "outcome_unknown", unknown=True)
                    session.commit()
            except BaseException:
                self._halted.set()
                raise
            recorded = True

        try:
            yield record
        finally:
            if not recorded:
                self._halted.set()
                with self.state.sessions() as session:
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    claim = session.get(AssistantRequestDispatch, claim_id)
                    claim.status = "outcome_unknown"
                    claim.finished_at = datetime.now(UTC)
                    self._stop(session, session.get(Run, self.run_id), "outcome_unknown", unknown=True)
                    session.commit()


def recover_assistant_run_calls(sessions) -> int:
    """Startup-only reconciliation before workers: never replay an interrupted paid call."""
    operation_ids = set()
    changed = 0
    with sessions() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        for claim in session.scalars(select(AssistantRunCall).where(AssistantRunCall.status == "external_started")):
            claim.status = "outcome_unknown"
            claim.finished_at = datetime.now(UTC)
            operation_ids.add(claim.operation_id)
            changed += 1
        for claim in session.scalars(
            select(AssistantRequestDispatch).where(
                AssistantRequestDispatch.origin == "assistant_run",
                AssistantRequestDispatch.status == "external_started",
            )
        ):
            claim.status = "outcome_unknown"
            claim.finished_at = datetime.now(UTC)
            claim.result = {"detail": "The server stopped before the acquisition outcome was recorded."}
            operation_ids.add(claim.operation_id)
            changed += 1
        if operation_ids:
            for run in session.scalars(select(Run).where(Run.trigger == "assistant")):
                if (run.stats or {}).get("assistant_actor", {}).get("operation_id") in operation_ids:
                    run.stats = {
                        **run.stats,
                        "assistant_outcome_unknown": True,
                        "assistant_stop_reason": "outcome_unknown",
                    }
            for operation in session.scalars(
                select(AssistantOperation).where(AssistantOperation.id.in_(operation_ids))
            ):
                operation.status = "outcome_unknown"
        session.commit()
    return changed


def run_usage(session, run_id, operation_id) -> dict:
    """Safe aggregate counts from durable ledgers, independent of mutable progress JSON."""
    calls = list(session.scalars(select(AssistantRunCall).where(AssistantRunCall.run_id == run_id)))
    requests = (
        list(
            session.scalars(
                select(AssistantRequestDispatch).where(
                    AssistantRequestDispatch.operation_id == operation_id,
                    AssistantRequestDispatch.origin == "assistant_run",
                )
            )
        )
        if operation_id
        else []
    )
    operation = session.get(AssistantOperation, operation_id) if operation_id else None
    run = session.get(Run, run_id)
    abandoned = (run is None or run.status not in {"queued", "running"}) and any(
        call.status == "external_started" for call in [*calls, *requests]
    )
    return {
        "provider_calls_started": len(calls),
        "images_started": sum(call.kind == "image" for call in calls),
        "acquisitions_started": len(requests),
        "outcome_unknown": abandoned
        or (operation is not None and operation.status == "outcome_unknown")
        or any(call.status == "outcome_unknown" for call in [*calls, *requests]),
        "stopped": operation is not None and operation.status in {"failed", "cancelled", "outcome_unknown"},
    }
