"""Pure person change projection and transaction-owned writes shared by REST and MCP."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from shortlist.server.db.models import User


@dataclass(frozen=True)
class PersonMutation:
    user_id: int
    values: dict
    changed: dict
    steps: tuple[dict, ...]


def prepare_person_in_session(session: Session, user_id: int, patch) -> PersonMutation:
    """Validate first, retaining the old names needed by durable Plex reconciliation."""
    from shortlist.server.api.users import _reject_display_name_clash, merged_prefs
    from shortlist.server.assistant.row_effects import (
        cleanup_step,
        hide_step,
        nickname_rename_step,
        privacy_sync_step,
        restore_step,
    )

    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    values = {}
    steps = []
    privacy = False
    if patch.enabled is not None and patch.enabled != user.enabled:
        values["enabled"] = patch.enabled
        if not patch.enabled:
            steps.append(cleanup_step(user.slug))
        privacy = True
    if patch.manage_sharing is not None and user.user_type != "owner" and patch.manage_sharing != user.manage_sharing:
        values["manage_sharing"] = patch.manage_sharing
        privacy = True
    if privacy:
        steps.append(privacy_sync_step("person visibility or sharing preferences changed"))
    if patch.nickname is not None:
        nickname = patch.nickname.strip()
        _reject_display_name_clash(session, user, nickname or user.friendly_name or user.username)
        if nickname != (user.nickname or ""):
            values["nickname"] = nickname
    for key in ("request_tag", "requested_by_tag"):
        value = getattr(patch, key)
        if value is not None and value.strip() != (getattr(user, key) or ""):
            values[key] = value.strip()
    if patch.prefs is not None:
        prefs = merged_prefs(user.prefs or {}, patch.prefs)
        if prefs != (user.prefs or {}):
            values["prefs"] = prefs
        was_paused = bool((user.prefs or {}).get("paused"))
        now_paused = bool(prefs.get("paused"))
        if was_paused != now_paused:
            steps.append(hide_step(user.slug) if now_paused else restore_step(user.slug))
    if "nickname" in values:
        steps.append(nickname_rename_step({user.slug: user.display_name}))
    changed = {key: {"before": getattr(user, key), "after": value} for key, value in values.items()}
    return PersonMutation(user_id, values, changed, tuple(steps))


def apply_person_in_session(session: Session, mutation: PersonMutation) -> User:
    """Apply under the caller's transaction; the caller also saves all owed effects."""
    user = session.get(User, mutation.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    for key, value in mutation.values.items():
        setattr(user, key, value)
    return user
