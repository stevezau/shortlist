"""Bounded external discovery using configured services, never caller-supplied URLs."""

from __future__ import annotations

import asyncio
import json

from shortlist.engine.models import MediaType
from shortlist.server.assistant.contracts import ToolResult
from shortlist.server.assistant_auth import Capability, require_authorized
from shortlist.server.db.adapters import DbCache


async def permitted_libraries(state, principal) -> ToolResult:
    from shortlist.server.services.connection_choices import read_libraries

    require_authorized(principal, [Capability.CONFIG_READ])
    libraries = await asyncio.to_thread(read_libraries, state)
    items = [
        item
        for item in libraries
        if principal.constraints.include_future_libraries or item["key"] in principal.constraints.library_keys
    ]
    return ToolResult(
        summary="Permitted movie and show libraries on the configured Plex server.",
        data={"items": items},
        warnings=[
            "Library names may come from a short-lived cache; mutation preflight must revalidate delivery targets."
        ],
    )


def search_titles(state, *, query: str, media: str, year: int | None, limit: int) -> ToolResult:
    tmdb = state.run_service.build_tmdb_only()
    if tmdb is None:
        return ToolResult(
            summary="TMDB metadata is not configured.",
            data={"items": []},
            next_action="Enter the TMDB credential directly in Shortlist Settings.",
        )
    found = tmdb.search_all(query, MediaType(media))
    items = []
    for hit in found:
        release = str(hit.get("release_date") or hit.get("first_air_date") or "")[:4]
        release_year = int(release) if release.isdigit() else None
        if year is not None and release_year != year:
            continue
        title = str(hit.get("title") or hit.get("name") or "")
        identifier = hit.get("id")
        if type(identifier) is not int or identifier <= 0 or not title:
            continue
        items.append({"tmdb_id": identifier, "media": media, "title": title, "year": release_year})
        if len(items) >= limit:
            break
    cache = DbCache(state.sessions, kind="assistant_titles")
    for item in items:
        cache.set(f"{media}:{item['tmdb_id']}", json.dumps(item), ttl_s=3600)
    return ToolResult(
        summary=f"Resolved {len(items)} matching title candidates.",
        data={"items": items, "verification_lifetime_seconds": 3600, "ambiguous": len(items) > 1},
        warnings=["Choose the intended title and year; multiple candidates must not be resolved by guessing."]
        if len(items) > 1
        else [],
    )
