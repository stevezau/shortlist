"""Pinned assistant runs, durable handoff and pre-network authorization checks.

Paid work reserves finite request limits and rechecks authority at each outbound call.
A dry run can perform expensive reads; it is not a free configuration preview.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, date, datetime
from enum import Enum

from pydantic import ConfigDict, Field
from sqlalchemy import select

from shortlist.server.assistant_auth import Capability, StoredGrantIdentity, require_authorized
from shortlist.server.db.models import Collection, Run, Server, Theme, User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.settings_store import SettingsStore

from .changes import AccessRequirements, ChangeError, DomainPlan, DomainResult, EffectIntent, fingerprint
from .contracts import StrictModel
from .people_seasons import _dependencies, _snapshot
from .policy import AuthGrantPolicy


class RunLimits(StrictModel):
    """Finite outbound controls shared by run and preview tools."""

    max_provider_calls: int = Field(
        default=0,
        ge=0,
        le=500,
        description=(
            "Maximum outbound AI/search/image requests; reserved against the connection lifetime quota. "
            "Not a currency cap."
        ),
    )
    max_output_tokens: int = Field(
        default=2048,
        ge=128,
        le=16384,
        description="Maximum output tokens per model request, including provider reasoning where applicable.",
    )
    max_native_tool_uses: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Maximum built-in search uses per request where the provider exposes a hard control.",
    )
    max_acquisitions: int = Field(
        default=0, ge=0, le=100, description="Maximum titles this run may send to configured acquisition services."
    )
    max_images: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Maximum image requests, each producing one image, within the aggregate provider-call budget.",
    )
    allow_provider_managed_search: bool = Field(
        default=False,
        description=(
            "Permit Google native search only after exact owner approval; its internal search count "
            "and monetary cost cannot be capped by Shortlist."
        ),
    )


class RunIntent(RunLimits):
    model_config = ConfigDict(extra="forbid", strict=True)
    row_ids: list[int] = Field(min_length=1, max_length=100)
    person_ids: list[int] = Field(min_length=1, max_length=100)
    dry_run: bool
    include_shared: bool = False


def _serializable(value):
    if dataclasses.is_dataclass(value):
        return _serializable(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_serializable(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


def config_fingerprint(config) -> str:
    return fingerprint(_serializable(config))


def _run_dependencies(session) -> dict:
    """Pin owner-controlled person state while allowing the run's own derived progress writes."""
    dependencies = _dependencies(session)
    people = []
    for person in session.scalars(select(User).order_by(User.id)):
        record = {
            column.name: _serializable(getattr(person, column.name))
            for column in User.__table__.columns
            if column.name != "cold_start"
        }
        record["prefs"] = {key: value for key, value in (person.prefs or {}).items() if key != "history_depth"}
        people.append(record)
    dependencies[User.__tablename__] = fingerprint(people)
    dependencies.update(server=_snapshot(session, Server), themes=_snapshot(session, Theme))
    return dependencies


class RunAdapter:
    kind = "run"

    def __init__(self, state) -> None:
        self.state = state

    def project(self, session, intent: dict):
        body = RunIntent.model_validate(intent)
        body.row_ids = sorted(set(body.row_ids))
        body.person_ids = sorted(set(body.person_ids))
        rows = list(session.scalars(select(Collection).where(Collection.id.in_(body.row_ids)).order_by(Collection.id)))
        if len(rows) != len(body.row_ids) or any(row_id <= 0 for row_id in body.row_ids + body.person_ids):
            raise ChangeError("invalid_selection", "Choose existing rows and people.")
        effective_dry = body.dry_run or force_dry_run()
        if not effective_dry and any(not row.enabled for row in rows):
            raise ChangeError("invalid_selection", "Enable the selected rows before running them.")
        shared = any(row.build == "shared" for row in rows)
        if shared != body.include_shared:
            raise ChangeError("invalid_selection", "Explicitly include shared rows when selecting a shared row.")
        service = self.state.run_service
        profiles = service.enabled_profiles(session, body.person_ids)
        people = list(session.scalars(select(User).where(User.id.in_(body.person_ids)).order_by(User.id)))
        if len(people) != len(body.person_ids) or {person.slug for person in people} != {p.slug for p in profiles}:
            raise ChangeError("invalid_selection", "Every selected person must currently be eligible for a run.")
        if shared and {p.slug for p in profiles} != {p.slug for p in service.enabled_profiles(session)}:
            raise ChangeError("invalid_selection", "Shared runs require the complete resolved eligible roster.")
        config = service._ctx._engine_config(
            session, SettingsStore(session, self.state.secrets), dry_run=effective_dry, collection_ids=body.row_ids
        )
        from .run_effects import paid_effect_contract

        spend = paid_effect_contract(
            config, SettingsStore(session, self.state.secrets), body, config_hash=config_fingerprint
        )
        capabilities = ["runs.preview" if effective_dry else "runs.execute", "history.use", "history.providers"]
        if body.max_provider_calls:
            capabilities.append(Capability.AI_GENERATE.value)
        if spend["acquisitions"]:
            capabilities.append("requests.send")
        if spend["queue_requests"]:
            capabilities.append("requests.manage")
        destinations = {item["destination"] for item in spend["providers"] + spend["acquisitions"]}
        dependencies = _run_dependencies(session)
        # The current engine's auxiliary privacy, retirement and shelf passes cover all rows.
        # Declare that footprint instead of treating build_only as a security boundary.
        touched_rows = list(session.scalars(select(Collection).order_by(Collection.id))) if not effective_dry else rows
        requirements = AccessRequirements(
            capabilities=tuple(capabilities),
            row_ids=tuple(row.id for row in touched_rows),
            person_ids=tuple(body.person_ids),
            library_keys=tuple(sorted({str(key) for row in touched_rows for key in row.library_keys})),
            dynamic_libraries=True,
            destination_ids=tuple(sorted(destinations)),
            batch_size=max(len(rows), body.max_acquisitions),
            work_units=max(len(rows) * len(people), body.max_acquisitions),
            provider_calls=body.max_provider_calls,
            requires_approval=spend["provider_managed_search"],
        )
        contract = {
            "intent": body.model_dump(mode="json"),
            "dependencies": dependencies,
            "config_hash": config_fingerprint(config),
            "row_slugs": [row.slug for row in rows],
            "person_slugs": [p.slug for p in profiles],
            "effective_dry_run": effective_dry,
            "spend": spend,
        }
        return body, requirements, contract, profiles

    def prepare(self, session, intent: dict) -> DomainPlan:
        body, requirements, contract, _ = self.project(session, intent)
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies=contract["dependencies"],
            requirements=requirements,
            effects=(EffectIntent("assistant.run", {"run_id": {"$ref": "run_id"}}, "run", max_attempts=1),),
            summary={
                "description": "Queue one bounded run; validate its saved scope again before network access.",
                "row_ids": body.row_ids,
                "person_ids": body.person_ids,
                "dry_run": contract["effective_dry_run"],
                "include_shared": body.include_shared,
                "external_generation": bool(contract["spend"]["providers"]),
                "acquisition_requests": bool(contract["spend"]["acquisitions"]),
                "limits": {key: value for key, value in body.model_dump(mode="json").items() if key.startswith("max_")},
                "provider_calls": contract["spend"]["providers"],
                "acquisition_destinations": sorted({item["destination"] for item in contract["spend"]["acquisitions"]}),
                "billing": (
                    "Limits count Shortlist requests and output tokens, not currency. "
                    "Unused reservations are retained; "
                    "scheduled theme top-ups and future scheduled runs have separate authority."
                ),
                "provider_managed_search": contract["spend"]["provider_managed_search"],
                "provider_managed_search_notice": (
                    "Google may perform multiple billable internal searches per request; "
                    "Shortlist cannot cap that count or monetary cost."
                )
                if contract["spend"]["provider_managed_search"]
                else None,
                "work_units": requirements.work_units,
                "library_scope": "The engine reads all available library indexes.",
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body, _, contract, _profiles = self.project(session, intent)
        run = self.state.run_service.queue_run_in_session(
            session,
            trigger="assistant",
            dry_run=contract["effective_dry_run"],
            user_ids=body.person_ids,
            collection_ids=body.row_ids,
            assistant_contract=contract,
        )
        return DomainResult(
            result={"run_id": run.id, "row_ids": body.row_ids, "person_ids": body.person_ids},
            audit_diff={"run_id": run.id, "dry_run": run.dry_run},
            references={"run_id": run.id},
            created_run_ids=(run.id,),
        )


def validate_execution_in_session(session, state, run: Run):
    """Revalidate a queued run's identity, exact effects and configuration in one snapshot."""
    if getattr(state, "assistant_auth", None) is None:
        raise ChangeError("missing_permission", "Assistant access is disabled; queued runs cannot start.")
    if run is None:
        raise ChangeError("invalid_selection", "The assistant run no longer exists.")
    actor = (run.stats or {}).get("assistant_actor")
    contract = (run.stats or {}).get("assistant_contract")
    if not actor or not contract:
        raise ChangeError("missing_permission", "The run has no approved assistant execution contract.")
    principal = StoredGrantIdentity(
        grant_id=actor["grant_id"],
        owner_account_id=actor["owner_account_id"],
        client_id=actor["client_id"],
        revision=actor["grant_revision"],
    )
    from shortlist.server.assistant_auth.repository import require_current_grant_in_session

    owner_id = session.scalar(select(Server.owner_account_id).limit(1))
    if owner_id is None:
        raise ChangeError("missing_permission", "The installation has no verified owner.")
    # Standing authority is rechecked against the exact saved effects. An exact
    # approval remains revision-bound and cannot survive later permission changes.
    grant = require_current_grant_in_session(
        session,
        principal,
        now=datetime.now(UTC),
        current_owner_account_id=owner_id,
        check_revision=actor["authorization_basis"] == "operation_approval",
    )
    grant = dataclasses.replace(
        grant,
        capabilities=grant.capabilities & frozenset(Capability(value) for value in actor["effective_capabilities"]),
    )
    requirements = AccessRequirements(**actor["requirements"])
    if fingerprint(dataclasses.asdict(requirements)) != actor["requirements_hash"]:
        raise ChangeError("stale_plan", "The run's effect contract changed.")
    if actor["authorization_basis"] != "operation_approval":
        require_authorized(grant, (Capability(value) for value in requirements.capabilities), requirements.selection())
    _, current_requirements, projected, profiles = RunAdapter(state).project(session, contract["intent"])
    if projected != contract or fingerprint(dataclasses.asdict(current_requirements)) != fingerprint(
        dataclasses.asdict(requirements)
    ):
        raise ChangeError("stale_plan", "The run's configuration or resolved scope changed; prepare another run.")
    return contract, profiles


async def dispatch_assistant_run(state, payload: dict) -> dict:
    if set(payload) != {"run_id"} or type(payload["run_id"]) is not int:
        raise ChangeError("invalid_selection", "Invalid run dispatch reference.")
    return await state.run_service.dispatch_queued_assistant_run(payload["run_id"])


def cancel_assistant_run(state, principal, run_id: int) -> dict:
    """Cancel only this connection's bounded run; committed privacy work still settles."""
    policy = AuthGrantPolicy()
    with state.sessions() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        grant = policy.current_grant(session, principal, datetime.now(UTC))
        run = session.get(Run, run_id)
        actor = (run.stats or {}).get("assistant_actor") if run else None
        if not actor or (actor["grant_id"], actor["client_id"], actor["owner_account_id"]) != (
            grant.grant_id,
            grant.client_id,
            grant.owner_account_id,
        ):
            raise ChangeError("invalid_selection", "The run is not available to this connection.")
        original = AccessRequirements(**actor["requirements"])
        policy.authorize(grant, dataclasses.replace(original, capabilities=("jobs.cancel",), requires_approval=False))
        if run.status not in {"queued", "running"}:
            return {"run_id": run_id, "status": run.status, "cancel_requested": False}
        if run.began_at is None:
            run.status = "aborted"
            run.finished_at = datetime.now(UTC)
        run.stats = {**run.stats, "cancel_requested": True}
        from shortlist.server.services.audit import add_audit

        add_audit(session, "assistant.run_cancelled", "info", grant_id=grant.grant_id, run_id=run_id)
        session.commit()
    # Signal after commit; the run service checks persisted cancellation again before it starts.
    signalled = state.run_service.cancel_run(run_id)
    return {"run_id": run_id, "status": "stopping" if signalled else "cancelled", "cancel_requested": True}


def get_assistant_run_report(state, principal, run_id: int) -> dict:
    """Project aggregate progress for one owned run, without traces, titles or watch history."""
    from collections import Counter

    from shortlist.server.db.models import RunUser, iso_utc

    policy = AuthGrantPolicy()
    with state.sessions() as session:
        grant = policy.current_grant(session, principal, datetime.now(UTC))
        run = session.get(Run, run_id)
        actor = (run.stats or {}).get("assistant_actor") if run else None
        if not actor or (actor["grant_id"], actor["client_id"], actor["owner_account_id"]) != (
            grant.grant_id,
            grant.client_id,
            grant.owner_account_id,
        ):
            raise ChangeError("invalid_selection", "The run is not available to this connection.")
        requirements = AccessRequirements(**actor["requirements"])
        policy.authorize(
            grant, dataclasses.replace(requirements, capabilities=("activity.read",), requires_approval=False)
        )
        if not set(actor["effective_capabilities"]).issubset(grant.capabilities):
            raise ChangeError("missing_permission", "This token has fewer permissions than the run's original token.")
        results = list(session.scalars(select(RunUser).where(RunUser.run_id == run_id)))
        from .run_spend import run_usage

        usage = run_usage(session, run_id, actor["operation_id"])
        return {
            "run_id": run_id,
            "status": "outcome_unknown"
            if run.stats.get("assistant_outcome_unknown") or usage["outcome_unknown"]
            else "partially_applied"
            if usage["stopped"]
            else run.status,
            "dry_run": run.dry_run,
            "started_at": iso_utc(run.began_at),
            "finished_at": iso_utc(run.finished_at),
            "row_ids": list(requirements.row_ids),
            "person_ids": list(requirements.person_ids),
            "people_by_status": dict(Counter(result.status for result in results)),
            "cancel_requested": bool(run.stats.get("cancel_requested")),
            "usage": usage,
        }
