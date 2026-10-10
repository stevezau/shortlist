"""Pure person change projection and transaction-owned writes shared by REST and MCP."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from shortlist.server.db.models import User


def merged_prefs(stored: dict, sent: BaseModel) -> dict:
    """``stored`` with the fields ``sent`` actually mentioned applied, and nothing else touched.

    Read with ``model_fields_set``, never with an ``is not None`` filter. "The client did not mention
    this field" and "the client set it to null" are different instructions, and only the first means
    "leave it alone" — the None filter collapsed them, so a pref could be set but never CLEARED. It
    would also have started silently clobbering the day a ``UserPrefs`` field gained a non-``None``
    default, because ``model_dump()`` renders that default whether or not the client sent it, writing
    it into every user on every unrelated PATCH. ``PATCH /collections`` and ``PUT …/rows`` already
    read the request this way; this was the last partial write that did not.

    ``model_dump`` rather than ``getattr``, and that is not a style choice: ``prefs`` is a JSON
    column and ``blocked_seeds`` accepts objects, so reading the field off the model would hand
    SQLAlchemy ``BlockSeedBody`` instances instead of dicts. ``exclude_unset`` gives exactly the
    fields ``model_fields_set`` names, with the nested models already converted.

    Args:
        stored: The prefs mapping as it is on the user right now. Never mutated.
        sent: The parsed request body's prefs model.

    Returns:
        A new mapping. Keys the model knows nothing about (an install's accrued ``history_depth``,
        say) pass through untouched.
    """
    merged = dict(stored)
    for key, value in sent.model_dump(exclude_unset=True).items():
        if value is None:
            merged.pop(key, None)  # an explicit null clears the override
        else:
            merged[key] = value
    return merged


def reject_display_name_clash(session: Session, user: User, nickname: str) -> None:
    """Refuse a nickname that renders to the same row title as somebody else's.

    `{user}` renders `display_name` (nickname → Tautulli friendly name → username). Only the
    username is unique on Plex, so two people resolving to the same display name ask for two
    collections with one title in one library — which PMS refuses, leaving that person's row failing
    every night with an error that reads as a generic Plex fault. Privacy is unaffected either way
    (collections are matched on `shortlist_<slug>` before title), so this is about a legible failure,
    not a leak: say so at the point of entry rather than in tomorrow's run log.
    """
    wanted = nickname.casefold()
    for other in session.query(User).filter(User.id != user.id):
        theirs = other.nickname or other.friendly_name or other.username
        if theirs.casefold() == wanted:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{other.username} already shows up as “{theirs}” — pick a different name so their rows stay apart"
                ),
            )


@dataclass(frozen=True)
class PersonMutation:
    user_id: int
    values: dict
    changed: dict
    steps: tuple[dict, ...]


def prepare_person_in_session(session: Session, user_id: int, patch) -> PersonMutation:
    """Validate first, retaining the old names needed by durable Plex reconciliation."""
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
        reject_display_name_clash(session, user, nickname or user.friendly_name or user.username)
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
