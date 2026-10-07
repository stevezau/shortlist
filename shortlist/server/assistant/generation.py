"""Explicit, quota-reserved theme generation with durable external-call outcomes."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from sqlalchemy import select, text

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.curator import make_curator
from shortlist.engine.models import MediaType
from shortlist.server.assistant_auth import Capability
from shortlist.server.assistant_auth.repository import StoredGrantIdentity, require_current_grant_in_session
from shortlist.server.db.adapters import DbCache
from shortlist.server.db.models import CacheRow, Job, Server, Setting
from shortlist.server.services.audit import add_audit
from shortlist.server.services.context_builder import curator_kwargs
from shortlist.server.services.theme_author import ThemeAuthorError, author_theme
from shortlist.server.settings_store import SettingsStore

from .budgets import AssistantBudget
from .changes import AccessRequirements, DomainPlan, DomainResult, EffectIntent, fingerprint
from .contracts import StrictModel
from .operation_models import AssistantChange, AssistantOperation
from .policy import AuthGrantPolicy, ChangeError


class GenerationIntent(StrictModel):
    brief: str = Field(
        min_length=1, max_length=1000, description="The theme to author; no personal watch history is sent."
    )
    media: Literal["movie", "show", "both"]
    guidance: str = Field(
        default="",
        max_length=4000,
        description="Optional editorial guidance; cannot replace Shortlist's locked mechanics.",
    )
    max_output_tokens: int = Field(
        default=4000,
        ge=256,
        le=8000,
        description="Hard output-token ceiling for the single provider call, not a monetary estimate.",
    )


class GenerateThemeInput(StrictModel):
    action: Literal["prepare", "apply"] = Field(
        description="Prepare the paid generation plan first; apply its exact change ID after review."
    )
    definition: GenerationIntent | None = None
    change_id: str | None = Field(default=None, min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def coherent_action(self):
        if self.action == "prepare":
            if self.definition is None or self.change_id is not None or self.idempotency_key is not None:
                raise ValueError("prepare requires a definition and no apply identifiers")
        elif self.definition is not None or self.change_id is None or self.idempotency_key is None:
            raise ValueError("apply requires change_id and idempotency_key, without a new definition")
        return self


def provider_destination(store: SettingsStore) -> str:
    """Pin the approved endpoint, including when an SDK supports environment overrides."""
    provider = str(store.get("curator.provider") or "")
    destinations = {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com/v1",
        "google": "https://generativelanguage.googleapis.com",
    }
    if provider in ("openai_compatible", "ollama"):
        from shortlist.engine.curator.openai_compatible import normalize_base_url

        destination = normalize_base_url(
            str(store.get("curator.openai_base_url") or store.get("curator.ollama_url") or "")
        )
    else:
        destination = destinations.get(provider, "")
    parsed = urlsplit(destination)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Configure a supported provider with an explicit credential-free endpoint first.")
    return destination.rstrip("/")


def _settings_hash(session) -> str:
    return fingerprint({row.key: row.value for row in session.scalars(select(Setting).order_by(Setting.key))})


class GenerationAdapter:
    kind = "generation"

    def __init__(self, state) -> None:
        self.state = state

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = GenerationIntent.model_validate(intent)
        store = SettingsStore(session, self.state.secrets)
        destination = provider_destination(store)
        if not store.get("tmdb.apikey"):
            raise ValueError("Configure TMDB metadata before generating a resolved theme.")
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies={"settings": _settings_hash(session)},
            requirements=AccessRequirements(
                capabilities=("ai.generate", "catalog.read"),
                destination_ids=(destination,),
                provider_calls=1,
                batch_size=1,
            ),
            effects=(EffectIntent("assistant.generate_theme", {}, "generation", max_attempts=3),),
            summary={
                "description": "Generate one theme draft using the configured provider.",
                "provider": str(store.get("curator.provider")),
                "destination": destination,
                "model": str(store.get("curator.model") or "provider default"),
                "provider_calls_reserved": 1,
                "max_output_tokens": body.max_output_tokens,
                "monetary_estimate": None,
                "history_sent": False,
                "saves_theme": False,
                "delivers_to_plex": False,
                "retry_policy": "Provider dispatch is never retried. Metadata resolution may reuse the saved response.",
                "library_availability": "Not checked by draft generation.",
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = GenerationIntent.model_validate(intent)
        return DomainResult(
            result={
                "generation_stage": "reserved",
                "provider_calls_reserved": 1,
                "max_output_tokens": body.max_output_tokens,
            },
            audit_diff={"provider_calls_reserved": 1, "max_output_tokens": body.max_output_tokens},
        )


class BudgetedCurator:
    """Restrict this authoring invocation to one bounded completion request."""

    def __init__(self, curator, *, maximum_tokens: int, record) -> None:
        self._curator = curator
        self._maximum_tokens = maximum_tokens
        self._record = record
        self._called = False
        self.name = curator.name
        self.can_complete = True

    @property
    def last_tokens(self) -> int:
        return int(getattr(self._curator, "last_tokens", 0) or 0)

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        if self._called:
            raise RuntimeError("The reservation permits only one provider dispatch.")
        self._called = True
        result = self._curator.complete(
            system, user, max_tokens=min(max_tokens or self._maximum_tokens, self._maximum_tokens)
        )
        if result:
            self._record(result, self.last_tokens)
        return result


class _ReplayCurator:
    name = "saved_response"
    can_complete = True

    def __init__(self, reply: str, tokens: int) -> None:
        self.reply, self.last_tokens = reply, tokens

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        return self.reply


class _NoLibraryReader:
    def __getattr__(self, name):
        raise RuntimeError("Draft generation has no authority to inspect a Plex collection.")


def _record_reply(state, operation_id: str, reply: str, tokens: int) -> None:
    if len(reply.encode()) > 262_144:
        raise RuntimeError("Provider response exceeded the bounded draft size.")
    with state.sessions() as session:
        operation = session.get(AssistantOperation, operation_id)
        session.merge(
            CacheRow(
                kind="assistant_generation",
                key=operation_id,
                value={"reply": reply, "tokens": tokens},
                expires_at=time.time() + 86_400,
            )
        )
        operation.result = {**operation.result, "generation_stage": "provider_returned", "provider_tokens": tokens}
        add_audit(session, "assistant.provider_call", "info", operation_id=operation_id, tokens=tokens, calls=1)
        session.commit()


def _finish(state, operation_id: str, *, status: str, result: dict) -> dict:
    with state.sessions() as session:
        operation = session.get(AssistantOperation, operation_id)
        operation.status = status
        operation.result = {**operation.result, **result}
        operation.finished_at = datetime.now(UTC)
        session.commit()
    return {"status": status, "detail": "Theme generation " + status.replace("_", " ")}


def dispatch_generation(state, payload: dict, *, job_id: int) -> dict:
    """Run or resolve a single paid dispatch, with a checkpoint before contacting the provider."""
    with state.sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(Job, job_id)
        if job is None or not job.operation_id:
            raise RuntimeError("Generation requires a correlated approved operation.")
        operation = session.get(AssistantOperation, job.operation_id)
        change = session.get(AssistantChange, operation.change_id) if operation else None
        if operation is None or change is None or change.kind != "generation":
            raise RuntimeError("Generation has no valid saved plan.")
        operation_id = operation.id
        if operation.status in {"completed", "failed", "cancelled", "outcome_unknown"}:
            return {"status": operation.status, "detail": "Generation already reached a terminal outcome"}
        stage = operation.result.get("generation_stage")
        reply = session.get(CacheRow, ("assistant_generation", operation_id))
        if reply is not None and reply.expires_at <= time.time():
            reply = None
        if stage in {"external_started", "provider_returned"} and reply is None:
            terminal = "outcome_unknown" if stage == "external_started" else "failed"
            operation.status = terminal
            operation.result = {**operation.result, "generation_stage": terminal}
            operation.finished_at = datetime.now(UTC)
            session.commit()
            return {
                "status": terminal,
                "detail": "The prior provider dispatch has no reusable response; it will not be repeated",
            }
        if getattr(state, "assistant_auth", None) is None:
            operation.status = "cancelled"
            session.commit()
            return {"status": "cancelled", "detail": "Assistant access is disabled"}
        principal = StoredGrantIdentity(
            grant_id=change.grant_id,
            owner_account_id=change.owner_account_id,
            client_id=change.client_id,
            revision=change.grant_revision,
        )
        owner = session.scalar(select(Server.owner_account_id).limit(1))
        try:
            if owner is None:
                raise ChangeError("owner_unavailable", "The verified owner is no longer available.")
            grant = require_current_grant_in_session(
                session,
                principal,
                check_revision=operation.authorization_basis == "operation_approval",
                current_owner_account_id=owner,
            )
            effective = frozenset(Capability(value) for value in operation.result.get("_effective_capabilities", []))
            grant = replace(grant, capabilities=grant.capabilities & effective)
            requirements = AccessRequirements(**change.requirements)
            if operation.authorization_basis != "operation_approval":
                AuthGrantPolicy().authorize(grant, requirements)
            budget = session.get(AssistantBudget, grant.grant_id)
            if budget is None or budget.provider_calls_reserved > grant.constraints.max_provider_calls:
                raise ChangeError("budget_exceeded", "The reserved provider work exceeds the current grant quota.")
            if _settings_hash(session) != change.dependencies.get("settings"):
                raise ChangeError("stale_plan", "Provider configuration changed before generation started.")
        except (PermissionError, ChangeError):
            operation.status = "cancelled"
            operation.result = {**operation.result, "generation_stage": "cancelled_before_dispatch"}
            session.commit()
            return {
                "status": "cancelled",
                "detail": "The saved generation authority or configuration is no longer current",
            }
        intent = GenerationIntent.model_validate(change.intent)
        store = SettingsStore(session, state.secrets)
        metadata_key = str(store.get("tmdb.apikey") or "")
        if reply is not None:
            curator = _ReplayCurator(str(reply.value["reply"]), int(reply.value.get("tokens", 0)))
        else:
            kwargs = curator_kwargs(store.get)
            kwargs.update(max_retries=0, base_url=provider_destination(store), follow_redirects=False)
            curator = make_curator(str(store.get("curator.provider")), **kwargs)
        operation.result = {
            **operation.result,
            "generation_stage": "provider_returned" if reply else "external_started",
        }
        session.commit()

    wrapper = BudgetedCurator(
        curator,
        maximum_tokens=intent.max_output_tokens,
        record=(lambda value, tokens: None)
        if reply is not None
        else lambda value, tokens: _record_reply(state, operation_id, value, tokens),
    )
    metadata = TmdbClient(metadata_key, cache=DbCache(state.sessions))
    media = (MediaType.MOVIE, MediaType.SHOW) if intent.media == "both" else MediaType(intent.media)
    try:
        draft = author_theme(
            brief=intent.brief,
            media=media,
            curator=wrapper,
            tmdb=metadata,
            plex=_NoLibraryReader(),
            library_index={MediaType.MOVIE: {}, MediaType.SHOW: {}},
            guidance=intent.guidance,
            max_details=50,
        )
    except ThemeAuthorError:
        with state.sessions() as session:
            received = session.get(CacheRow, ("assistant_generation", operation_id)) is not None
        return _finish(
            state,
            operation_id,
            status="failed" if received else "outcome_unknown",
            result={
                "generation_stage": "unusable_response" if received else "outcome_unknown",
                "message": "The provider response could not produce a usable draft. No provider retry was made.",
            },
        )
    except Exception:
        # A paid response was checkpointed before metadata processing; retries reuse it.
        raise RuntimeError(
            "Draft resolution did not finish. A saved provider response will be reused; no new call is authorized."
        ) from None
    picks = []
    verified = DbCache(state.sessions, kind="assistant_titles")
    for pick in draft.spec.picks:
        title = draft.titles.get((pick.media, pick.tmdb_id), "")
        item = {"tmdb_id": pick.tmdb_id, "media": pick.media.value, "title": title, "year": None}
        verified.set(f"{pick.media.value}:{pick.tmdb_id}", json.dumps(item), ttl_s=3600)
        picks.append({**item, "origin": "owner", "reason": pick.reason})
    generated = {
        "name": draft.spec.name,
        "emoji": draft.spec.emoji,
        "brief": draft.brief,
        "media": [kind.value for kind in draft.spec.media],
        "picks": picks,
        "genres": list(draft.spec.genres),
        "excluded_genres": list(draft.spec.excluded_genres),
        "rules": {
            "max_runtime": draft.spec.rules.max_runtime,
            "min_year": draft.spec.rules.min_year,
            "max_year": draft.spec.rules.max_year,
            "min_rating": draft.spec.rules.min_rating,
            "min_votes": draft.spec.min_votes,
        },
    }
    if not picks and not generated["genres"]:
        return _finish(
            state,
            operation_id,
            status="failed",
            result={
                "generation_stage": "unsupported_draft",
                "message": (
                    "The provider returned only discovery tags. Supply resolved titles or genres before saving a theme."
                ),
            },
        )
    return _finish(
        state,
        operation_id,
        status="completed",
        result={
            "generation_stage": "completed",
            "draft": generated,
            "provider_tokens": draft.tokens,
            "library_availability": "not_checked",
            "saved_theme": False,
            "source": "shortlist_provider",
            "next_action": "shortlist_plan_theme",
        },
    )
