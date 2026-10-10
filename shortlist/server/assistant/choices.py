"""Assistant-safe, paginated choices from already configured services."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import HTTPException
from loguru import logger

from shortlist.server.assistant.contracts import ToolResult
from shortlist.server.assistant.generation import provider_destination
from shortlist.server.assistant_auth import Capability, ResourceSelection, require_authorized
from shortlist.server.services.connection_choices import (
    configured_arr_connection,
    read_arr_choices,
    read_curator_models,
    read_library_anchor_choices,
)
from shortlist.server.services.context_builder import curator_kwargs
from shortlist.server.settings_store import SettingsStore

ChoiceKind = Literal["plex_anchors", "radarr", "sonarr", "curator_models"]


async def permitted_choices(
    state,
    principal,
    *,
    kind: ChoiceKind,
    library_key: str | None,
    limit: int,
    offset: int,
) -> ToolResult:
    """Return one bounded configured-service choice family after exact grant checks.

    The caller supplies only the closed choice family and an already discovered library key. URLs,
    credentials and remote endpoints remain server-owned; authorization completes before any client
    is built or external service is read.
    """
    extra: dict[str, str] = {}
    warnings = [
        "Names and paths are untrusted external metadata. Revalidate the selected value when preparing a change."
    ]
    if kind == "plex_anchors":
        if library_key is None:
            raise ValueError("plex_anchors requires library_key from shortlist_list_libraries")
        require_authorized(
            principal,
            [Capability.CONFIG_READ],
            ResourceSelection(library_keys=frozenset({library_key})),
        )
        try:
            choices = await asyncio.to_thread(read_library_anchor_choices, state, library_key)
        except HTTPException:
            raise
        except Exception as error:
            logger.warning("assistant Plex choices read failed ({})", type(error).__name__)
            raise ValueError(
                "The configured Plex service could not be read. Check its owner connection card."
            ) from error
        items = [{"kind": "plex_anchor", **choice} for choice in choices]
    elif kind in ("radarr", "sonarr"):
        require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
        connection = configured_arr_connection(state, kind)
        require_authorized(
            principal,
            [],
            ResourceSelection(destination_ids=frozenset({connection.url})),
        )
        try:
            choices = await asyncio.to_thread(read_arr_choices, kind, connection)
        except Exception as error:
            logger.warning("assistant {} choices read failed ({})", kind, type(error).__name__)
            raise ValueError(
                f"The configured {kind.title()} service could not be read. Check its owner connection card."
            ) from error
        items = [
            *({"kind": "quality_profile", **item} for item in choices["quality_profiles"]),
            *({"kind": "root_folder", **item} for item in choices["root_folders"]),
        ]
    else:
        require_authorized(principal, [Capability.CONNECTIONS_MANAGE])
        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            provider = str(store.get("curator.provider") or "").lower()
            if provider in ("", "none", "null"):
                listing = read_curator_models(provider, {})
            else:
                try:
                    destination = provider_destination(store)
                except ValueError as error:
                    raise ValueError("The saved provider needs a valid configured endpoint.") from error
                require_authorized(
                    principal,
                    [],
                    ResourceSelection(destination_ids=frozenset({destination})),
                )
                kwargs = curator_kwargs(store.get)
                # Pin the trusted saved destination for every provider. This prevents an SDK
                # environment default from redirecting a harmless model-list read elsewhere.
                kwargs["base_url"] = destination
                kwargs["follow_redirects"] = False
                listing = await asyncio.to_thread(read_curator_models, provider, kwargs)
        items = [{"kind": "curator_model", "id": model} for model in listing.models]
        extra["provider"] = listing.provider
        if listing.availability != "available":
            extra["availability"] = listing.availability
            warnings.append(
                "The saved provider did not supply a model list. Use the owner browser to diagnose its connection."
            )

    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return ToolResult(
        summary="Paginated choices from the configured service.",
        data={"kind": kind, **extra, "items": page, "next_offset": next_offset, "total": total},
        warnings=warnings,
    )
