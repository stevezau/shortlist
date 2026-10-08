"""Owner-browser choices pinned to configured assistant effect destinations."""

from __future__ import annotations

from contextlib import suppress
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from shortlist.server.api.schemas import PassthroughModel


class ConfiguredDestination(PassthroughModel):
    """A credential-free, currently configured service endpoint."""

    service_id: str
    label: str
    purposes: list[str]
    destination_id: str
    host: str


class DestinationSelection(BaseModel):
    """The exact catalog choice the owner saw before saving."""

    model_config = ConfigDict(extra="forbid")

    service_id: str
    destination_id: str


class DestinationSelectionConflict(ValueError):
    """A selected service changed since the owner reviewed its endpoint."""


def _has_secret(session: Session, key: str) -> bool:
    """Check configured presence without decrypting or returning credential material."""
    from shortlist.server.db.models import Setting

    row = session.get(Setting, key)
    return bool(row and isinstance(row.value, dict) and row.value.get("v"))


def configured_destinations(session: Session) -> list[ConfiguredDestination]:
    """List only supported services with exact current authorization destinations."""
    from shortlist.server.assistant.generation import provider_destination
    from shortlist.server.services.request_actions import _destination
    from shortlist.server.settings_store import SettingsStore

    store = SettingsStore(session)
    choices: list[ConfiguredDestination] = []

    def add(service_id: str, label: str, purposes: list[str], destination: str) -> None:
        parsed = urlsplit(destination)
        host = parsed.hostname
        if (
            host
            and parsed.scheme in {"http", "https"}
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        ):
            choices.append(
                ConfiguredDestination(
                    service_id=service_id,
                    label=label,
                    purposes=purposes,
                    destination_id=destination,
                    host=host,
                )
            )

    provider = str(store.get("curator.provider") or "")
    if provider not in {"", "none", "null"} and (
        provider in {"ollama", "openai_compatible"} or _has_secret(session, "curator.api_key")
    ):
        with suppress(ValueError):
            add("curator", "AI provider", ["ai", "image", "search"], provider_destination(store))
    if _has_secret(session, "exa.apikey"):
        add("exa", "Exa search", ["search"], "https://api.exa.ai")
    if _has_secret(session, "tmdb.apikey"):
        add("tmdb", "TMDB", ["connection check"], "https://api.themoviedb.org/3")
    if _has_secret(session, "trakt.client_id"):
        add("trakt", "Trakt", ["connection check"], "https://api.trakt.tv")
    if _has_secret(session, "requests.mdblist.apikey"):
        add("mdblist", "MDBList", ["connection check"], "https://api.mdblist.com")
    for service_id, label in (("plex", "Plex"), ("tautulli", "Tautulli")):
        url = str(store.get(f"{service_id}.url") or "")
        credential = "plex.token" if service_id == "plex" else "tautulli.apikey"
        if url and _has_secret(session, credential):
            with suppress(ValueError):
                add(service_id, label, ["connection check"], _destination(url))
    if store.get("searxng.url"):
        with suppress(ValueError):
            add("searxng", "SearXNG search", ["search"], _destination(str(store.get("searxng.url"))))
    for service_id, label in (("radarr", "Radarr"), ("sonarr", "Sonarr"), ("overseerr", "Seerr")):
        prefix = f"requests.{service_id}"
        url = str(store.get(f"{prefix}.url") or "")
        if url and _has_secret(session, f"{prefix}.apikey"):
            with suppress(ValueError):
                add(service_id, label, ["requests"], _destination(url))
    return choices


def validate_selected_destinations(
    session: Session,
    selections: list[DestinationSelection],
    added_ids: set[str],
) -> None:
    """Reject only stale newly selected endpoints, never historical approvals."""
    if not selections:
        return
    current = {choice.service_id: choice.destination_id for choice in configured_destinations(session)}
    for selection in selections:
        if selection.destination_id not in added_ids or current.get(selection.service_id) != selection.destination_id:
            raise DestinationSelectionConflict("A selected service changed. Reload and review its current endpoint.")
