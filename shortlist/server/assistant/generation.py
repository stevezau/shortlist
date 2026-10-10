"""Provider endpoint pinning and safe retirement of standalone assistant generation."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import text

from shortlist.server.db.models import Job
from shortlist.server.settings_store import SettingsStore

from .operation_models import AssistantChange, AssistantOperation


def provider_destination(store: SettingsStore) -> str:
    """Pin the configured endpoint, including when an SDK supports environment overrides."""
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


def dispatch_generation(state, payload: dict, *, job_id: int) -> dict:
    """Cancel historical queued MCP generation before any provider or metadata I/O."""
    del payload
    with state.sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        job = session.get(Job, job_id)
        if job is None or not job.operation_id:
            raise RuntimeError("Generation requires a correlated approved operation.")
        operation = session.get(AssistantOperation, job.operation_id)
        change = session.get(AssistantChange, operation.change_id) if operation else None
        if operation is None or change is None or change.kind != "generation":
            raise RuntimeError("Generation has no valid saved plan.")
        if operation.status in {"completed", "failed", "cancelled", "outcome_unknown"}:
            return {"status": operation.status, "detail": "Generation already reached a terminal outcome"}
        prior_stage = operation.result.get("generation_stage")
        if prior_stage in {"external_started", "provider_returned"}:
            operation.status = "outcome_unknown"
            operation.result = {**operation.result, "generation_retired": True}
            detail = "Historical provider dispatch may have occurred; no replay is allowed"
        else:
            operation.status = "cancelled"
            operation.result = {**operation.result, "generation_stage": "cancelled_before_dispatch"}
            detail = "Standalone assistant generation is no longer available"
        operation.finished_at = datetime.now(UTC)
        session.commit()
        return {"status": operation.status, "detail": detail}
