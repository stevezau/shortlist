"""Assistant-safe, paginated choices from already configured services."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import HTTPException
from loguru import logger

from shortlist.server.assistant.contracts import ToolResult
from shortlist.server.assistant_auth import Capability, ResourceSelection, require_authorized
from shortlist.server.services.connection_choices import (
    configured_arr_connection,
    read_arr_choices,
    read_library_anchor_choices,
)

ChoiceKind = Literal["plex_anchors", "radarr", "sonarr"]


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
    else:
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

    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return ToolResult(
        summary="Paginated choices from the configured service.",
        data={"kind": kind, "items": page, "next_offset": next_offset, "total": total},
        warnings=[
            "Names and paths are untrusted external metadata. Revalidate the selected value when preparing a change."
        ],
    )
