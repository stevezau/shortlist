"""Readers for the JSON blobs on `users.prefs`.

Anything stored as free-form JSON eventually gets a second shape, and then every reader has to cope
with both. Keeping those readers here — rather than in whichever API module happened to write the
key first — means the engine adapter and the API share one definition, and neither imports the other.
"""

from __future__ import annotations


def blocked_entries(prefs: dict | None) -> list[dict]:
    """`prefs["blocked_seeds"]` as a list of records, whatever shape it is stored in.

    It shipped as a bare ``list[int]`` of TMDB ids, so the user-detail page could only ever show
    "tmdb 346648" — a number nobody recognises, which is most of why the feature went unused. Newer
    entries carry the title. Both shapes are read here, for ever: an old install's list is perfectly
    valid data and must not need a migration to keep working.
    """
    out: list[dict] = []
    for entry in (prefs or {}).get("blocked_seeds") or []:
        if isinstance(entry, bool):
            continue  # bool is an int subclass; a stray True is not a TMDB id
        if isinstance(entry, int):
            out.append({"tmdb_id": entry, "title": "", "media_type": "", "year": None})
        elif isinstance(entry, dict) and isinstance(entry.get("tmdb_id"), int):
            out.append(
                {
                    "tmdb_id": entry["tmdb_id"],
                    "title": entry.get("title") or "",
                    "media_type": entry.get("media_type") or "",
                    "year": entry.get("year"),
                }
            )
    return out


def blocked_ids(prefs: dict | None) -> set[int]:
    """Just the TMDB ids — what the engine filters on, unchanged by the richer storage shape."""
    return {entry["tmdb_id"] for entry in blocked_entries(prefs)}
