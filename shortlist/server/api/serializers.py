"""JSON shapes rendered by more than one router.

These lived in ``api/users.py`` as private helpers, which left ``api/user_rows.py`` importing
``users._pick_dict`` — one router reaching for another's underscore name, which is a statement that
the helper was in the wrong place rather than that the rule should be bent. Anything two routers
render belongs here.
"""

from __future__ import annotations

from shortlist.server.db.models import DEFAULT_SLUG, PickRow, User, iso_utc


def pick_dict(pick: PickRow) -> dict:
    """One delivered pick, as the Users and per-user-rows pages render it.

    Args:
        pick: The stored pick row.

    Returns:
        The pick's rank, title, reason, row/library placement and provenance.
    """
    return {
        "rank": pick.rank,
        "title": pick.title,
        "reason": pick.reason,
        "media_type": pick.media_type,
        "collection_slug": pick.collection_slug or DEFAULT_SLUG,  # legacy blank rows are the default row
        "library": pick.library or "",
        "section_key": pick.section_key or "",
        "seed_title": pick.seed_title,
        # Provenance: which source(s) surfaced it, and how strongly they vouched. Blank/1.0 on rows
        # written before 0035, which the UI renders as "not recorded" rather than "a perfect match".
        "sources": [s for s in (pick.sources or "").split(",") if s],
        "affinity": pick.affinity,
    }


def user_dict(
    user: User,
    history_depth: int,
    last_run_at,
    hit_rate: float | None,
    preview_titles: list[str] | None = None,
) -> dict:
    """One person, as the Users list and the user-detail page render them.

    Args:
        user: The user row.
        history_depth: How many distinct watched titles we last read for them.
        last_run_at: When the most recent run that included them finished, or None.
        hit_rate: Watched-over-delivered across their whole history, or None with no picks yet.
        preview_titles: A few of their most recent pick titles, for the dashboard card.

    Returns:
        The serialized user.
    """
    return {
        "id": user.id,
        "plex_account_id": user.plex_account_id,
        "username": user.username,
        "slug": user.slug,
        "nickname": user.nickname or "",
        # What Tautulli calls them, when it has its own name for them — the default a blank
        # nickname falls back to, shown in the UI so the field's placeholder can be honest.
        "friendly_name": user.friendly_name or "",
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "user_type": user.user_type,
        "restricted": user.restricted,
        # "" when no parental preset is set. A managed account WITHOUT one is an ordinary user that
        # gets rows and privacy filters; one WITH a preset sees no collections at all and Plex refuses
        # to filter it. The UI needs the difference to say anything true (#20).
        "restriction_profile": user.restriction_profile or "",
        "enabled": user.enabled,
        "cold_start": user.cold_start,
        "request_tag": user.request_tag or "",
        "prefs": user.prefs or {},
        "history_depth": history_depth,
        "last_run_at": iso_utc(last_run_at),
        "hit_rate": hit_rate,
        # A few of their most recent pick titles, for a real preview strip on the dashboard card.
        "preview_titles": preview_titles or [],
    }
