"""Validated setup completion shared by the owner wizard and assistant plans.

The setup flag gates the owner SPA, so it is never a stand-in for the facts
that make Shortlist usable.  This module keeps those facts in one bounded,
credential-safe projection.  Callers receive booleans and safe guidance only.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.server.assistant.changes import ChangeError
from shortlist.server.db.models import Server, Setting, User
from shortlist.server.settings_store import SettingsStore


@dataclass(frozen=True)
class SetupReadiness:
    """Non-secret setup facts required before the wizard can complete."""

    plex_ownership: bool
    metadata: bool
    libraries: bool
    people: bool
    wizard_completion: bool

    @property
    def credentials_ready(self) -> bool:
        return self.plex_ownership and self.metadata

    @property
    def discovery_ready(self) -> bool:
        return self.libraries and self.people

    @property
    def ready(self) -> bool:
        return self.credentials_ready and self.discovery_ready and self.wizard_completion

    def fingerprint_data(self) -> dict[str, bool]:
        """Return exactly the durable and bounded readiness projection."""
        return {
            "plex_ownership": self.plex_ownership,
            "metadata": self.metadata,
            "libraries": self.libraries,
            "people": self.people,
            "wizard_completion": self.wizard_completion,
        }


def _saved_secret_present(session: Session, key: str) -> bool:
    row = session.get(Setting, key)
    return bool(row and isinstance(row.value, dict) and row.value.get("v"))


def setup_readiness_in_session(session: Session, secrets) -> SetupReadiness:
    """Read bounded setup readiness without returning a connection address or secret.

    A successful library read proves the currently saved server credential still
    reaches at least one movie or show library.  Its titles, keys and failure
    details stay inside this service.
    """
    server = session.scalar(select(Server).limit(1))
    plex_ownership = bool(server and server.owner_account_id)
    metadata = _saved_secret_present(session, "tmdb.apikey")
    libraries = False
    if plex_ownership and _saved_secret_present(session, "plex.token"):
        store = SettingsStore(session, secrets)
        try:
            plex = PlexClient(str(store.get("plex.url") or ""), str(store.get("plex.token") or ""))
            libraries = any(getattr(section, "type", None) in {"movie", "show"} for section in plex.sections())
        except Exception:
            # Transport failures can contain tokened URLs.  Readiness is a safe
            # false state; detailed diagnostics remain in the owner browser.
            libraries = False
    people = session.scalar(select(User.id).where(User.removed_at.is_(None)).limit(1)) is not None
    store = SettingsStore(session, secrets)
    return SetupReadiness(
        plex_ownership=plex_ownership,
        metadata=metadata,
        libraries=libraries,
        people=people,
        wizard_completion=bool(store.get("setup.completed")),
    )


def require_completion_ready_in_session(session: Session, secrets) -> SetupReadiness:
    """Return readiness or reject completion without exposing a failed remote detail."""
    readiness = setup_readiness_in_session(session, secrets)
    if not readiness.plex_ownership:
        raise ChangeError("invalid_selection", "Link an owned Plex server in the setup browser first.")
    if not readiness.metadata:
        raise ChangeError("invalid_selection", "Enter and test the TMDB key in the setup browser first.")
    if not readiness.libraries:
        raise ChangeError("invalid_selection", "Make at least one movie or show library available to Shortlist first.")
    if not readiness.people:
        raise ChangeError("invalid_selection", "Sync people from Plex before completing setup.")
    return readiness


def complete_setup_in_session(session: Session, secrets) -> SetupReadiness:
    """Validate live prerequisites, then persist only the completion flag."""
    readiness = require_completion_ready_in_session(session, secrets)
    SettingsStore(session, secrets).set_in_transaction("setup.completed", True)
    return readiness
