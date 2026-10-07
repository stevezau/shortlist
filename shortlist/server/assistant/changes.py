"""Typed, bounded assistant plans with atomic configuration and durable consequences."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation, AssistantOperationKey
from shortlist.server.assistant.policy import (
    AccessRequirements,
    AuthGrantPolicy,
    AuthorizationPolicy,
    ChangeError,
    Principal,
)
from shortlist.server.assistant_auth import Capability, StoredGrantIdentity
from shortlist.server.db.models import Collection, Job, Run, iso_utc
from shortlist.server.services.audit import add_audit

__all__ = [
    "AccessRequirements",
    "ChangeAdapter",
    "ChangeError",
    "ChangeService",
    "DomainPlan",
    "DomainResult",
    "EffectIntent",
    "OperationStager",
    "fingerprint",
]

MAX_JSON_BYTES = 262_144
MAX_EFFECTS = 64
MAX_DEPENDENCIES = 1024
PLAN_TTL = timedelta(minutes=15)


# A review preview is deliberately derived from stable plan mechanics rather than the
# plan's intent, summary or effect payloads.  An inspect-and-propose connection may
# need to tell its owner what category of work awaits review, but must not learn
# ungranted names, values, targets or destinations in doing so.
_REVIEW_CATEGORIES = {
    "configuration": ("configuration",),
    "generation": ("theme_generation",),
    "maintenance": ("maintenance",),
    "people": ("people",),
    "requests": ("requests",),
    "row": ("row",),
    "run": ("run",),
    "seasons": ("season",),
    "setup": ("theme", "row"),
    "theme": ("theme",),
}
_REVIEW_EFFECT_CATEGORIES = {
    "assistant.converge": "configuration_convergence",
    "assistant.generate_theme": "provider_generation",
    "assistant.request_send": "acquisition_dispatch",
    "assistant.run": "bounded_run",
}
_REVIEW_ACTIONS = {
    "configuration": frozenset(),
    "generation": frozenset(),
    "maintenance": frozenset(),
    "people": frozenset(),
    "requests": frozenset({"archive", "reject", "restore", "send"}),
    "row": frozenset({"create", "delete", "update"}),
    "run": frozenset(),
    "seasons": frozenset({"create", "delete", "update"}),
    "setup": frozenset(),
    "theme": frozenset({"create", "update"}),
}
_REVIEW_COST_LIMIT_KEYS = frozenset(
    {
        "max_acquisitions",
        "max_images",
        "max_native_tool_uses",
        "max_output_tokens",
        "max_provider_calls",
    }
)


@dataclass(frozen=True)
class EffectIntent:
    """One registered job, or one ordered convergence job with typed steps in its payload."""

    kind: str
    payload: dict
    effect_key: str
    max_attempts: int = 3


@dataclass(frozen=True)
class DomainPlan:
    """A domain's pure projection; summary must already obey disclosure policy."""

    normalized_intent: dict
    dependencies: dict[str, str]
    requirements: AccessRequirements
    effects: tuple[EffectIntent, ...] = ()
    summary: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DomainResult:
    """Transaction-owned mutation result with safe output and explicit creation references."""

    result: dict
    audit_diff: dict
    references: dict[str, str | int] = field(default_factory=dict)
    created_row_ids: tuple[int, ...] = ()
    created_run_ids: tuple[int, ...] = ()


class ChangeAdapter(Protocol):
    """Trusted domain code; never commits, opens a session, or performs external work."""

    kind: str

    def prepare(self, session: Session, intent: dict) -> DomainPlan: ...

    def apply(self, session: Session, intent: dict) -> DomainResult: ...


@runtime_checkable
class TransactionLocker(Protocol):
    """Optional domain lock, acquired before SQLite and held through commit or rollback."""

    def transaction_lock(self, normalized_intent: dict) -> AbstractContextManager[None]: ...


@runtime_checkable
class OperationStager(Protocol):
    """Optional typed reservation hook, inside the same operation transaction."""

    def stage_operation(self, session: Session, operation: AssistantOperation, normalized_intent: dict) -> None: ...


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _canonical(value: object) -> str:
    def validate(item: object, depth: int = 0) -> None:
        if depth > 16:
            raise ChangeError("invalid_selection", "The proposed change is nested too deeply.")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ChangeError("invalid_selection", "Change objects require string keys.")
            for child in item.values():
                validate(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ChangeError("invalid_selection", "The proposed change is not JSON data.")

    validate(value)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ChangeError("invalid_selection", "The proposed change is not valid JSON data.") from exc
    if len(encoded.encode()) > MAX_JSON_BYTES:
        raise ChangeError("invalid_selection", "The proposed change is too large.")
    return encoded


def fingerprint(value: object) -> str:
    """Hash a canonical JSON snapshot, including relationships needed to detect other writers."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _plan_data(plan: DomainPlan) -> dict:
    return {
        "intent": plan.normalized_intent,
        "dependencies": plan.dependencies,
        "requirements": asdict(plan.requirements),
        "effects": [asdict(effect) for effect in plan.effects],
        "summary": plan.summary,
    }


def _stored_data(change: AssistantChange) -> dict:
    return {
        "intent": change.intent,
        "dependencies": change.dependencies,
        "requirements": change.requirements,
        "effects": change.effects,
        "summary": change.summary,
    }


def _hash(kind: str, data: dict) -> str:
    return fingerprint({"schema_version": 1, "kind": kind, **data})


def _resolve(value: object, references: dict[str, str | int]) -> object:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            name = value["$ref"]
            if not isinstance(name, str) or name not in references:
                raise ChangeError("invalid_selection", "An effect refers to an unresolved created resource.")
            result = references[name]
            if isinstance(result, bool) or not isinstance(result, (str, int)):
                raise ChangeError("invalid_selection", "A created resource reference is invalid.")
            return result
        return {key: _resolve(child, references) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_resolve(child, references) for child in value]
    return value


@contextmanager
def _no_commit(session: Session):
    def refuse(_session: Session) -> None:
        raise ChangeError("operation_conflict", "A domain adapter attempted to commit outside the operation boundary.")

    event.listen(session, "before_commit", refuse)
    try:
        yield
    finally:
        event.remove(session, "before_commit", refuse)


@contextmanager
def _read_only(session: Session):
    def refuse_flush(_session, _context, _instances) -> None:
        raise ChangeError("operation_conflict", "A domain planner attempted to write configuration.")

    def refuse_write(state) -> None:
        if not state.is_select:
            raise ChangeError("operation_conflict", "Domain planning permits only typed reads.")

    event.listen(session, "before_flush", refuse_flush)
    event.listen(session, "do_orm_execute", refuse_write)
    try:
        yield
    finally:
        event.remove(session, "before_flush", refuse_flush)
        event.remove(session, "do_orm_execute", refuse_write)


class ChangeService:
    """Prepare, approve and apply plans through explicit domain adapters and grant policy."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        adapters: Mapping[str, ChangeAdapter],
        policy: AuthorizationPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self.adapters = dict(adapters)
        self.policy = policy or AuthGrantPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _adapter(self, kind: str) -> ChangeAdapter:
        if kind not in self.adapters:
            raise ChangeError("invalid_selection", "This kind of change is not supported.")
        return self.adapters[kind]

    def _project(self, session: Session, kind: str, intent: dict) -> DomainPlan:
        _canonical(intent)
        with _no_commit(session), _read_only(session):
            plan = self._adapter(kind).prepare(session, json.loads(_canonical(intent)))
        if session.new or session.dirty or session.deleted:
            raise ChangeError("operation_conflict", "A domain planner attempted to mutate configuration.")
        if not isinstance(plan, DomainPlan) or not isinstance(plan.requirements, AccessRequirements):
            raise ChangeError("invalid_selection", "The domain returned an invalid plan.")
        if len(plan.effects) > MAX_EFFECTS or len(plan.dependencies) > MAX_DEPENDENCIES:
            raise ChangeError("invalid_selection", "The proposed change has too many effects or dependencies.")
        for value in (plan.requirements.batch_size, plan.requirements.work_units, plan.requirements.provider_calls):
            if value is not None and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise ChangeError("invalid_selection", "Declared work and provider-call limits must be bounded counts.")
        keys = [effect.effect_key for effect in plan.effects]
        if len(set(keys)) != len(keys) or any(not key or len(key) > 128 for key in keys):
            raise ChangeError("invalid_selection", "Effect identifiers must be unique and bounded.")
        if any(not 1 <= effect.max_attempts <= 20 for effect in plan.effects):
            raise ChangeError("invalid_selection", "Effect retry limits are invalid.")
        try:
            for capability in plan.requirements.capabilities:
                Capability(capability)
        except ValueError as exc:
            raise ChangeError("invalid_selection", "The domain requires an unrecognized capability.") from exc
        _canonical(_plan_data(plan))
        return plan

    def _can_apply(self, grant, requirements: AccessRequirements) -> bool:
        try:
            self.policy.authorize(grant, requirements)
        except (ChangeError, PermissionError):
            return False
        return not requirements.requires_approval

    def prepare(self, principal: Principal, kind: str, intent: dict) -> dict:
        """Persist a bounded proposal without changing product configuration or issuing jobs."""
        now = _utc(self.clock())
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            grant = self.policy.current_grant(session, principal, now)
            self.policy.authorize(grant, AccessRequirements(capabilities=("changes.prepare",)))
            plan = self._project(session, kind, intent)
            data = json.loads(_canonical(_plan_data(plan)))
            change = AssistantChange(
                id="chg_" + secrets.token_urlsafe(24),
                grant_id=grant.grant_id,
                owner_account_id=grant.owner_account_id,
                client_id=grant.client_id,
                grant_revision=grant.revision,
                kind=kind,
                **data,
                content_hash=_hash(kind, data),
                created_at=now,
                expires_at=now + PLAN_TTL,
            )
            session.add(change)
            add_audit(session, "assistant.prepared", "info", change_id=change.id, grant_id=grant.grant_id, kind=kind)
            session.commit()
            return self._change_view(change, can_apply=self._can_apply(grant, plan.requirements))

    def _owned(self, session: Session, principal: Principal, change_id: str) -> AssistantChange:
        change = session.get(AssistantChange, change_id)
        if change is None or (change.grant_id, change.client_id, change.owner_account_id) != (
            principal.grant_id,
            principal.client_id,
            principal.owner_account_id,
        ):
            raise ChangeError("invalid_selection", "The change is not available to this connection.")
        return change

    def _valid(self, change: AssistantChange, grant, now: datetime) -> None:
        if _utc(change.expires_at) <= now or grant.revision != change.grant_revision:
            raise ChangeError("stale_plan", "The plan expired or the connection's permissions changed.")
        if change.schema_version != 1 or _hash(change.kind, _stored_data(change)) != change.content_hash:
            raise ChangeError("stale_plan", "The stored plan changed; prepare a new plan.")

    def approve(self, change_id: str, *, owner_account_id: int) -> dict:
        """Approve an exact plan; callers MUST verify a browser owner session and CSRF first."""
        now = _utc(self.clock())
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            change = session.get(AssistantChange, change_id)
            if change is None or change.owner_account_id != owner_account_id:
                raise ChangeError("invalid_selection", "The change is not available to this owner.")
            principal = StoredGrantIdentity(change.grant_id, owner_account_id, change.client_id, change.grant_revision)
            grant = self.policy.current_grant(session, principal, now)
            self._valid(change, grant, now)
            if change.operation_id is not None:
                raise ChangeError("operation_conflict", "The change has already been applied.")
            change.approved_by, change.approved_at, change.approved_hash = owner_account_id, now, change.content_hash
            add_audit(session, "assistant.approved", "info", change_id=change.id, owner_account_id=owner_account_id)
            session.commit()
            return self._change_view(change, can_apply=True)

    @contextmanager
    def _apply_transaction(self, principal: Principal, change_id: str):
        # Rotation holds its target lock before opening a transaction. Waiting for
        # that lock with BEGIN IMMEDIATE already held would invert the order.
        with self.sessions() as lookup:
            grant = self.policy.current_grant(lookup, principal, _utc(self.clock()))
            change = self._owned(lookup, principal, change_id)
            if change.operation_id is None:
                self._valid(change, grant, _utc(self.clock()))
            adapter = self._adapter(change.kind)
            needs_lock = change.operation_id is None and isinstance(adapter, TransactionLocker)
            intent = json.loads(_canonical(change.intent))
            content_hash = change.content_hash
        lock = adapter.transaction_lock(intent) if needs_lock else nullcontext()
        with lock, self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            change = self._owned(session, principal, change_id)
            if change.content_hash != content_hash:
                raise ChangeError("stale_plan", "The stored plan changed while waiting; prepare a new plan.")
            yield session

    def apply(self, principal: Principal, change_id: str, idempotency_key: str) -> dict:
        """Atomically authorize, mutate, audit and enqueue, or return an existing receipt."""
        from shortlist.server.services.jobs import enqueue_in_session

        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ChangeError("invalid_selection", "An idempotency key of 1 to 128 characters is required.")
        with self._apply_transaction(principal, change_id) as session:
            now = _utc(self.clock())
            grant = self.policy.current_grant(session, principal, now)
            change = self._owned(session, principal, change_id)
            request_hash = fingerprint({"change_id": change.id, "content_hash": change.content_hash})
            key_identity = (grant.grant_id, grant.client_id, idempotency_key)
            known_key = session.get(AssistantOperationKey, key_identity)
            if known_key is not None and known_key.request_hash != request_hash:
                raise ChangeError("operation_conflict", "This idempotency key was used for another change.")
            if change.operation_id is not None:
                operation = session.get(AssistantOperation, change.operation_id)
                if operation is None or operation.request_hash != request_hash:
                    raise ChangeError("operation_conflict", "The saved operation does not match this change.")
                disclose = self._can_disclose_receipt(session, operation, grant)
                self._remember_key(session, key_identity, operation)
                session.commit()
                return self._receipt(operation, disclose=disclose)
            if known_key is not None:
                raise ChangeError("operation_conflict", "The change has an inconsistent prior receipt.")
            self._valid(change, grant, now)
            plan = self._project(session, change.kind, change.intent)
            if _hash(change.kind, _plan_data(plan)) != change.content_hash:
                raise ChangeError("stale_plan", "Configuration or dependent resources changed; prepare a new plan.")
            approved = (
                change.approved_by == grant.owner_account_id
                and change.approved_hash == change.content_hash
                and change.approved_at is not None
                and change.approval_consumed_at is None
            )
            if not self._can_apply(grant, plan.requirements) and not approved:
                raise ChangeError("missing_permission", "This exact change needs owner approval.")
            if plan.requirements.provider_calls:
                from .budgets import reserve_provider_calls

                reserve_provider_calls(
                    session,
                    grant.grant_id,
                    requested=plan.requirements.provider_calls,
                    limit=grant.constraints.max_provider_calls,
                )
            # A creation may extend only its own grant, and only with rows this transaction created.
            existing_rows = (
                set(session.scalars(select(Collection.id)))
                if "rows.create" in plan.requirements.capabilities
                else set()
            )
            existing_run_max = session.scalar(select(func.max(Run.id))) or 0
            with _no_commit(session):
                result = self._adapter(change.kind).apply(session, json.loads(_canonical(change.intent)))
            if not isinstance(result, DomainResult):
                raise ChangeError("invalid_selection", "The domain returned an invalid mutation result.")
            _canonical(result.result)
            _canonical(result.audit_diff)
            session.flush()
            if result.created_row_ids:
                if "rows.create" not in plan.requirements.capabilities or any(
                    isinstance(row_id, bool)
                    or not isinstance(row_id, int)
                    or row_id in existing_rows
                    or session.get(Collection, row_id) is None
                    for row_id in result.created_row_ids
                ):
                    raise ChangeError("operation_conflict", "A creation attempted to claim an existing or invalid row.")
                from shortlist.server.assistant_auth.repository import grant_created_rows_in_session

                grant = grant_created_rows_in_session(
                    session, grant, result.created_row_ids, now=now, approved_creation=approved
                )
            operation = AssistantOperation(
                id="op_" + secrets.token_urlsafe(24),
                grant_id=grant.grant_id,
                owner_account_id=grant.owner_account_id,
                client_id=grant.client_id,
                change_id=change.id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="delivery_pending" if plan.effects else "completed",
                authorization_basis="operation_approval" if approved else "standing_grant",
                result={
                    **result.result,
                    "grant_revision": grant.revision,
                    "_effective_capabilities": sorted(getattr(grant, "capabilities", ())),
                },
                created_at=now,
                committed_at=now,
                finished_at=None if plan.effects else now,
            )
            session.add(operation)
            session.flush()
            adapter = self._adapter(change.kind)
            if isinstance(adapter, OperationStager):
                with _no_commit(session):
                    adapter.stage_operation(session, operation, json.loads(_canonical(change.intent)))
                _canonical(operation.result)
            for run_id in result.created_run_ids:
                run = session.get(Run, run_id)
                if type(run_id) is not int or run_id <= existing_run_max or run is None or run.trigger != "assistant":
                    raise ChangeError("operation_conflict", "An adapter attempted to claim an existing run.")
                run.stats = {
                    **run.stats,
                    "assistant_actor": {
                        "grant_id": grant.grant_id,
                        "owner_account_id": grant.owner_account_id,
                        "client_id": grant.client_id,
                        "grant_revision": grant.revision,
                        "effective_capabilities": sorted(getattr(grant, "capabilities", ())),
                        "requirements": asdict(plan.requirements),
                        "requirements_hash": fingerprint(asdict(plan.requirements)),
                        "authorization_basis": operation.authorization_basis,
                        "operation_id": operation.id,
                    },
                }
            jobs = [
                enqueue_in_session(
                    session,
                    effect.kind,
                    _resolve(effect.payload, result.references),
                    max_attempts=effect.max_attempts,
                    operation_id=operation.id,
                    effect_key=effect.effect_key,
                )
                for effect in plan.effects
            ]
            operation.job_ids = [job.id for job in jobs]
            change.operation_id = operation.id
            if approved:
                change.approval_consumed_at = now
            self._remember_key(session, key_identity, operation)
            add_audit(
                session,
                "assistant.applied",
                "info",
                operation_id=operation.id,
                change_id=change.id,
                grant_id=grant.grant_id,
                client_id=grant.client_id,
                owner_account_id=grant.owner_account_id,
                authorization_basis=operation.authorization_basis,
                diff=result.audit_diff,
                job_ids=operation.job_ids,
            )
            session.commit()
            return self._receipt(operation)

    @staticmethod
    def _remember_key(session: Session, identity: tuple[str, str, str], operation: AssistantOperation) -> None:
        if session.get(AssistantOperationKey, identity) is None:
            session.add(
                AssistantOperationKey(
                    grant_id=identity[0],
                    client_id=identity[1],
                    key=identity[2],
                    operation_id=operation.id,
                    request_hash=operation.request_hash,
                )
            )

    def get_change(self, principal: Principal, change_id: str) -> dict:
        """Return this connection's proposal without exposing dependency snapshots."""
        with self.sessions() as session:
            grant = self.policy.current_grant(session, principal, _utc(self.clock()))
            change = self._owned(session, principal, change_id)
            self._valid(change, grant, _utc(self.clock()))
            requirements = AccessRequirements(**change.requirements)
            return self._change_view(
                change,
                disclose=self._can_apply(grant, requirements),
                can_apply=self._can_apply(grant, requirements)
                or (
                    change.approved_by == grant.owner_account_id
                    and change.approved_hash == change.content_hash
                    and change.approval_consumed_at is None
                ),
            )

    def get_operation(self, principal: Principal, operation_id: str) -> dict:
        """Return a durable receipt with conservative aggregate job completion status."""
        with self.sessions() as session:
            grant = self.policy.current_grant(session, principal, _utc(self.clock()))
            operation = session.get(AssistantOperation, operation_id)
            if operation is None or (operation.grant_id, operation.client_id, operation.owner_account_id) != (
                principal.grant_id,
                principal.client_id,
                principal.owner_account_id,
            ):
                raise ChangeError("invalid_selection", "The operation is not available to this connection.")
            disclose = self._can_disclose_receipt(session, operation, grant)
            receipt = self._receipt(operation, disclose=disclose)
            if operation.job_ids and operation.status not in {"outcome_unknown", "failed", "cancelled"}:
                jobs = list(session.scalars(select(Job).where(Job.id.in_(operation.job_ids))))
                if len(jobs) != len(operation.job_ids):
                    receipt["status"] = "outcome_unknown"
                elif all(job.status == "done" for job in jobs):
                    receipt["status"] = "completed"
                elif any(job.status == "failed" for job in jobs):
                    receipt["status"] = "partially_applied"
            run_id = operation.result.get("run_id")
            if isinstance(run_id, int):
                from .run_spend import run_usage

                run = session.get(Run, run_id)
                usage = run_usage(session, run_id, operation.id)
                if disclose:
                    receipt["run_usage"] = usage
                if (
                    run is None
                    or (run.stats or {}).get("assistant_actor", {}).get("operation_id") != operation.id
                    or run.stats.get("assistant_outcome_unknown")
                    or usage["outcome_unknown"]
                ):
                    receipt["status"] = "outcome_unknown"
                elif run.stats.get("assistant_stop_reason") or usage["stopped"]:
                    receipt["status"] = "partially_applied"
                else:
                    receipt["status"] = {
                        "queued": "queued",
                        "running": "running",
                        "ok": "completed",
                        "error": "partially_applied",
                        "aborted": "cancelled",
                    }.get(run.status, "outcome_unknown")
            return receipt

    def _can_disclose_receipt(self, session: Session, operation: AssistantOperation, grant) -> bool:
        # Same-owner progress survives permission edits. Detailed results remain bounded
        # by the original token and, after a revision, the original resolved effects.
        if not set(operation.result.get("_effective_capabilities", ())).issubset(
            set(getattr(grant, "capabilities", ()))
        ):
            return False
        if operation.result.get("grant_revision") == grant.revision:
            return True
        change = session.get(AssistantChange, operation.change_id)
        if change is None:
            return False
        requirements = replace(AccessRequirements(**change.requirements), requires_approval=False)
        try:
            self.policy.authorize(grant, requirements)
        except (ChangeError, PermissionError):
            return False
        return True

    @staticmethod
    def _review_preview(change: AssistantChange) -> dict:
        """Return a useful, non-content description while an owner must review a plan.

        This projection intentionally never reads the plan summary, dependencies, resource
        requirements or effect payloads.  Those can contain names, target IDs, settings or
        destinations that an inspect-only grant is not entitled to see.
        """
        kind = change.kind if change.kind in _REVIEW_CATEGORIES else "change"
        intent = change.intent if isinstance(change.intent, dict) else {}
        candidate_action = intent.get("action")
        action = (
            candidate_action
            if isinstance(candidate_action, str) and candidate_action in _REVIEW_ACTIONS.get(kind, frozenset())
            else "prepare"
        )
        effects = change.effects if isinstance(change.effects, list) else []
        effect_categories = sorted(
            {
                _REVIEW_EFFECT_CATEGORIES[effect["kind"]]
                for effect in effects
                if isinstance(effect, dict) and effect.get("kind") in _REVIEW_EFFECT_CATEGORIES
            }
        )
        cost_limits = {
            key: value
            for key, value in intent.items()
            if key in _REVIEW_COST_LIMIT_KEYS and type(value) is int and value >= 0
        }
        return {
            "kind": kind,
            "action": action,
            "requested_categories": list(_REVIEW_CATEGORIES.get(kind, ("change",))),
            "effect_categories": effect_categories,
            "cost_limits": cost_limits,
            "requires_owner_review": True,
        }

    @staticmethod
    def _change_view(change: AssistantChange, *, can_apply: bool, disclose: bool | None = None) -> dict:
        can_disclose = can_apply if disclose is None else disclose
        view = {
            "change_id": change.id,
            "kind": change.kind,
            "revision": change.content_hash,
            "expires_at": iso_utc(change.expires_at),
            "summary": change.summary if can_disclose else {"message": "This change requires owner review."},
            "required_capabilities": change.requirements.get("capabilities", []),
            "authorization": {"can_apply": can_apply, "approved": change.approved_at is not None},
            "operation_id": change.operation_id,
        }
        if not can_disclose:
            view["review_preview"] = ChangeService._review_preview(change)
        return view

    @staticmethod
    def _receipt(operation: AssistantOperation, *, disclose: bool = True) -> dict:
        if not disclose:
            return {
                "operation_id": operation.id,
                "change_id": operation.change_id,
                "status": operation.status,
                "committed_at": iso_utc(operation.committed_at),
                "finished_at": iso_utc(operation.finished_at),
                "details_available": False,
            }
        return {
            "operation_id": operation.id,
            "change_id": operation.change_id,
            "status": operation.status,
            "authorization_basis": operation.authorization_basis,
            "result": {key: value for key, value in operation.result.items() if not key.startswith("_")},
            "job_ids": operation.job_ids,
            "committed_at": iso_utc(operation.committed_at),
        }
