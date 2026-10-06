"""Closed, ordered follow-up effects shared by assistant mutation domains.

Mutations persist one ``assistant.converge`` job in their own transaction.  The
job owns the writer lock and runs this finite set of steps in order.  A
successful step is checkpointed on the job before the next one starts, so a
retry does not repeat already completed network work.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy.orm import Session

from shortlist.server.db.models import Job
from shortlist.server.services import jobs

from .changes import EffectIntent


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _EmptyPayload(_StrictModel):
    pass


class _ReasonPayload(_StrictModel):
    reason: str = Field(min_length=1, max_length=512)


class _PersonPayload(_StrictModel):
    slug: str = Field(min_length=1, max_length=255)
    dry_run: bool = False


class _NicknamePayload(_StrictModel):
    was_called: dict[str, str] = Field(min_length=1, max_length=500)


class _VisibilityPayload(_StrictModel):
    row: str | None = Field(default=None, min_length=1, max_length=255)
    dry_run: bool = False


class _RenamePayload(_StrictModel):
    slug: str = Field(min_length=1, max_length=255)
    new_template: str = Field(max_length=512)
    old_template: str = Field(max_length=512)
    scope: str = Field(min_length=1, max_length=120)


class _ReconcilePayload(_StrictModel):
    slug: str = Field(min_length=1, max_length=255)
    build: Literal["per_person", "shared"] = "per_person"
    only_user_ids: list[int] | None = Field(default=None, max_length=500)
    dry_run: bool = False
    template: str | None = Field(default=None, max_length=512)
    in_sections: list[str] | None = Field(default=None, max_length=100)
    scope: str = Field(default="row.reconcile", min_length=1, max_length=120)


class _PosterPayload(_StrictModel):
    slug: str = Field(min_length=1, max_length=255)
    build: Literal["per_person", "shared"] = "per_person"
    scope: str = Field(min_length=1, max_length=120)


class _LogPayload(_StrictModel):
    level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"]


class _PrivacySyncStep(_StrictModel):
    kind: Literal["privacy.sync"]
    payload: _ReasonPayload


class _CleanupStep(_StrictModel):
    kind: Literal["user.cleanup"]
    payload: _PersonPayload


class _HideStep(_StrictModel):
    kind: Literal["user.hide"]
    payload: _PersonPayload


class _RestoreStep(_StrictModel):
    kind: Literal["user.restore"]
    payload: _PersonPayload


class _NicknameStep(_StrictModel):
    kind: Literal["user.nickname_rename"]
    payload: _NicknamePayload


class _VisibilityStep(_StrictModel):
    kind: Literal["rows.visibility"]
    payload: _VisibilityPayload


class _RenameStep(_StrictModel):
    kind: Literal["row.rename"]
    payload: _RenamePayload


class _ReconcileStep(_StrictModel):
    kind: Literal["row.reconcile"]
    payload: _ReconcilePayload


class _PosterStep(_StrictModel):
    kind: Literal["poster.reset"]
    payload: _PosterPayload


class _ScheduleStep(_StrictModel):
    kind: Literal["schedule.rebuild"]
    payload: _EmptyPayload


class _LogStep(_StrictModel):
    kind: Literal["log.configure"]
    payload: _LogPayload


class _CacheStep(_StrictModel):
    kind: Literal["cache.invalidate"]
    payload: _EmptyPayload


type ConvergenceStep = Annotated[
    _PrivacySyncStep
    | _CleanupStep
    | _HideStep
    | _RestoreStep
    | _NicknameStep
    | _VisibilityStep
    | _RenameStep
    | _ReconcileStep
    | _PosterStep
    | _ScheduleStep
    | _LogStep
    | _CacheStep,
    Field(discriminator="kind"),
]
_STEPS = TypeAdapter(list[ConvergenceStep])
_DOMAINS = {"settings", "people", "rows", "seasons", "themes"}


def validate_convergence_steps(steps: list[dict] | tuple[dict, ...]) -> list[dict]:
    """Return canonical JSON for the finite supported step union."""
    return [step.model_dump(mode="json") for step in _STEPS.validate_python(list(steps))]


def _step(kind: str, payload: dict) -> dict:
    return validate_convergence_steps([{"kind": kind, "payload": payload}])[0]


def privacy_sync_step(reason: str) -> dict:
    return _step("privacy.sync", {"reason": reason})


def cleanup_step(slug: str, *, dry_run: bool = False) -> dict:
    return _step("user.cleanup", {"slug": slug, "dry_run": dry_run})


def hide_step(slug: str, *, dry_run: bool = False) -> dict:
    return _step("user.hide", {"slug": slug, "dry_run": dry_run})


def restore_step(slug: str, *, dry_run: bool = False) -> dict:
    return _step("user.restore", {"slug": slug, "dry_run": dry_run})


def nickname_rename_step(was_called: dict[str, str]) -> dict:
    return _step("user.nickname_rename", {"was_called": was_called})


def visibility_step(slug: str | None = None, *, dry_run: bool = False) -> dict:
    return _step("rows.visibility", {"row": slug, "dry_run": dry_run})


def row_rename_step(slug: str, *, new_template: str, old_template: str, scope: str) -> dict:
    return _step(
        "row.rename",
        {"slug": slug, "new_template": new_template, "old_template": old_template, "scope": scope},
    )


def reconcile_step(
    slug: str,
    *,
    build: Literal["per_person", "shared"] = "per_person",
    only_user_ids: list[int] | None = None,
    dry_run: bool = False,
    template: str | None = None,
    in_sections: list[str] | None = None,
    scope: str = "row.reconcile",
) -> dict:
    return _step(
        "row.reconcile",
        {
            "slug": slug,
            "build": build,
            "only_user_ids": only_user_ids,
            "dry_run": dry_run,
            "template": template,
            "in_sections": in_sections,
            "scope": scope,
        },
    )


def poster_reset_step(slug: str, *, build: Literal["per_person", "shared"], scope: str) -> dict:
    return _step("poster.reset", {"slug": slug, "build": build, "scope": scope})


def schedule_rebuild_step() -> dict:
    return _step("schedule.rebuild", {})


def log_configure_step(level: str) -> dict:
    return _step("log.configure", {"level": level})


def cache_invalidate_step() -> dict:
    return _step("cache.invalidate", {})


def _payload(steps: list[dict] | tuple[dict, ...], domain: str) -> dict:
    if domain not in _DOMAINS:
        raise ValueError(f"unknown convergence domain {domain!r}")
    normalized = validate_convergence_steps(steps)
    if not normalized:
        raise ValueError("a convergence job requires at least one step")
    return {"domain": domain, "steps": normalized}


def convergence_effect(
    steps: list[dict] | tuple[dict, ...],
    *,
    domain: str,
    effect_key: str = "converge",
    max_attempts: int = 3,
) -> EffectIntent:
    """Describe one ordered durable job for a domain plan."""
    return EffectIntent("assistant.converge", _payload(steps, domain), effect_key, max_attempts)


def queue_convergence_in_session(
    session: Session,
    steps: list[dict] | tuple[dict, ...],
    *,
    domain: str,
    operation_id: str | None = None,
    effect_key: str = "converge",
    max_attempts: int = 3,
) -> Job:
    """Queue one convergence job without committing the caller's transaction."""
    correlation = {"operation_id": operation_id, "effect_key": effect_key} if operation_id else {}
    return jobs.enqueue_in_session(
        session,
        "assistant.converge",
        _payload(steps, domain),
        max_attempts=max_attempts,
        **correlation,
    )


def _checkpoint(state, job_id: int, index: int, result: dict) -> None:
    with state.sessions() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise RuntimeError(f"convergence job {job_id} disappeared")
        payload = dict(job.payload or {})
        completed = list(payload.get("completed_steps", []))
        if index not in completed:
            completed.append(index)
        results = list(payload.get("step_results", []))
        results.append({"index": index, "kind": payload["steps"][index]["kind"], "result": result})
        payload["completed_steps"] = sorted(completed)
        payload["step_results"] = results
        job.payload = payload
        session.commit()


async def _run_step(state, step: dict) -> dict:
    kind = step["kind"]
    payload = step["payload"]
    if kind in {"privacy.sync", "user.cleanup", "user.hide", "user.restore", "row.reconcile", "rows.visibility"}:
        return await jobs.run_handler_inline(state, kind, payload)
    if kind == "user.nickname_rename":
        from shortlist.server.services.user_sync import rename_after_nickname

        await rename_after_nickname(state, payload["was_called"], holds_writer_lock=True)
        return {"renamed": sorted(payload["was_called"])}
    if kind == "row.rename":
        from shortlist.server.services.collection_reconcile import run_row_rename_from_plex

        renamed, error = await run_row_rename_from_plex(state, **payload, holds_writer_lock=True)
        return {"renamed": renamed, "error": error}
    if kind == "poster.reset":
        from shortlist.server.services.collection_reconcile import run_poster_reset

        reset, error = await run_poster_reset(state, **payload)
        return {"reset": reset, "error": error}
    if kind == "schedule.rebuild":
        from types import SimpleNamespace

        from shortlist.server.scheduler import rebuild_schedule

        rebuild_schedule(SimpleNamespace(state=state))
        return {"rebuilt": True}
    if kind == "log.configure":
        from shortlist.logging_config import configure_logging

        configure_logging(payload["level"])
        return {"level": payload["level"]}
    if kind == "cache.invalidate":
        from shortlist.server.api.system import invalidate_plex_reads

        invalidate_plex_reads(state)
        return {"invalidated": True}
    raise AssertionError(f"validated unsupported convergence step {kind!r}")


@jobs.handler("assistant.converge")
async def _assistant_converge(state, payload: dict, *, job_id: int) -> dict:
    steps = validate_convergence_steps(payload.get("steps", []))
    domain = payload.get("domain")
    if domain not in _DOMAINS:
        raise ValueError(f"unknown convergence domain {domain!r}")
    completed = set(payload.get("completed_steps", []))
    for index, step in enumerate(steps):
        if index in completed:
            continue
        result = await _run_step(state, step)
        _checkpoint(state, job_id, index, result or {})
    return {"domain": domain, "completed_steps": len(steps), "detail": f"Applied {len(steps)} ordered step(s)"}


__all__ = [
    "ConvergenceStep",
    "cache_invalidate_step",
    "cleanup_step",
    "convergence_effect",
    "hide_step",
    "log_configure_step",
    "nickname_rename_step",
    "poster_reset_step",
    "privacy_sync_step",
    "queue_convergence_in_session",
    "reconcile_step",
    "restore_step",
    "row_rename_step",
    "schedule_rebuild_step",
    "validate_convergence_steps",
    "visibility_step",
]
