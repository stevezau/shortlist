"""Transaction-owned settings mutations shared by the browser and assistants.

Validation and the consequence list are computed before writes. Callers persist
that list in the jobs outbox and commit once; no network I/O belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import event
from sqlalchemy.orm import Session

from shortlist.server.db.models import DEFAULT_SLUG, Collection
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services.plex_reachability import describe_address
from shortlist.server.services.settings_validation import (
    FETCHED_URL_KEYS,
    KNOWN_KEYS,
    REDACTED_PLACEHOLDER,
    reject_blocked_urls,
    settings_diff,
    validate_values,
)
from shortlist.server.settings_store import DEFAULTS, SECRET_KEYS, SettingsStore


@dataclass(frozen=True)
class SettingsMutation:
    values: dict
    resets: tuple[str, ...]
    changed: dict
    steps: tuple[dict, ...]


def prepare_settings_in_session(
    session: Session, secrets, values: dict, *, resets: tuple[str, ...] = ()
) -> SettingsMutation:
    """Validate a complete update and derive ordered effects without changing any row."""
    # These validators also serve the existing browser's connection/probe forms.
    from shortlist.server.assistant.row_effects import (
        cache_invalidate_step,
        log_configure_step,
        privacy_sync_step,
        row_rename_step,
        schedule_rebuild_step,
    )
    from shortlist.server.scheduler import DEFAULT_CRONS

    unknown = (set(values) | set(resets)) - KNOWN_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown settings: {sorted(unknown)}")
    if set(values) & set(resets):
        raise HTTPException(status_code=422, detail="A setting cannot be assigned and reset in the same change.")
    validate_values(values)
    reject_blocked_urls(values)
    store = SettingsStore(session, secrets)
    normalized = {}
    reset_keys = set(resets)
    for key, value in values.items():
        if key in SECRET_KEYS and value == REDACTED_PLACEHOLDER:
            continue
        if value is None and key in DEFAULT_CRONS:
            reset_keys.add(key)
        else:
            normalized[key] = value.strip() if key in FETCHED_URL_KEYS and isinstance(value, str) else value
    changed = settings_diff(store, normalized)
    for key in reset_keys:
        if store.has_row(key):
            changed[key] = {
                "before": "<redacted>" if key in SECRET_KEYS else store.get(key),
                "after": "<redacted>" if key in SECRET_KEYS else DEFAULTS.get(key),
                "reset": True,
            }

    def after(key: str):
        return DEFAULTS.get(key) if key in reset_keys else normalized.get(key, store.get(key))

    old_name = str(store.get("row.name_template") or "")
    new_name = str(after("row.name_template") or "")
    if new_name and new_name != old_name:
        default_row = session.query(Collection).filter_by(slug=DEFAULT_SLUG).first()
        clashes = reconcile.rows_titled_from(
            session,
            new_name,
            secrets=secrets,
            exclude_slug=DEFAULT_SLUG,
            build="per_person",
            media=default_row.media if default_row else "both",
            library_keys=default_row.library_keys if default_row else [],
        )
        if clashes:
            raise HTTPException(
                status_code=422, detail="The proposed row name conflicts with another row in the same library."
            )

    steps = []
    if bool(after("privacy.hide_shared_from_disabled")) != bool(store.get("privacy.hide_shared_from_disabled")):
        steps.append(privacy_sync_step("the shared-row privacy setting changed"))
    if new_name != old_name:
        steps.append(
            row_rename_step(DEFAULT_SLUG, new_template=new_name, old_template=old_name, scope="settings.rename")
        )
    if set(changed) & (set(DEFAULT_CRONS) | {"backup.max_keep"}):
        steps.append(schedule_rebuild_step())
    if "log.level" in changed:
        steps.append(log_configure_step(after("log.level")))
    if set(changed) & {"plex.url", "plex.token"}:
        steps.append(cache_invalidate_step())
    return SettingsMutation(normalized, tuple(sorted(reset_keys)), changed, tuple(steps))


def apply_settings_in_session(session: Session, secrets, mutation: SettingsMutation) -> None:
    """Apply a validated change, leaving commit and durable consequences to the caller."""
    store = SettingsStore(session, secrets)
    for key, value in mutation.values.items():
        store.set_in_transaction(key, value)
    for key in mutation.resets:
        store.unset_in_transaction(key)
    if "plex.url" in mutation.changed:
        description = describe_address(str(store.get("plex.url") or ""))
        pending = True

        def after_commit(_session: Session) -> None:
            if pending:
                logger.info("settings: the Plex address is now {}", description)

        def after_rollback(_session: Session) -> None:
            nonlocal pending
            pending = False

        # The caller owns the transaction; a rolled-back proposal must not announce a new address.
        event.listen(session, "after_commit", after_commit, once=True)
        event.listen(session, "after_rollback", after_rollback, once=True)
