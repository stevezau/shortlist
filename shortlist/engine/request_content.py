"""Opt-in movie request policy using TMDB's structured genres and keywords.

Music alone also describes fictional musicals; Documentary alone describes many unrelated films.
Keep both unless metadata positively identifies music nonfiction. Unavailable metadata holds the
individual movie for a later retry rather than bypassing an enabled restriction.
"""

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import MediaType, MissingTitle

CONTENT_REASON_PREFIX = "music content filter:"
_MUSIC = 10402
_DOCUMENTARY = 99
_FICTION_GENRES = {28, 12, 16, 35, 80, 18, 14, 27, 9648, 10749, 878, 53, 10752, 37}
_CONCERT_KEYWORDS = {6029, 11634, 156205, 162066}
_MUSIC_DOCUMENTARY_KEYWORDS = {246377, 156205}


def music_nonfiction_reason(tmdb: TmdbClient, title: MissingTitle) -> str:
    """A hold reason, or an empty string when this movie policy allows the title.

    No text matching: a fictional film can mention a concert without being concert footage.
    TV requests are outside this movie policy. Calls use the TMDB client's existing cache.
    """
    if title.media_type is not MediaType.MOVIE:
        return ""
    unknown = f"{CONTENT_REASON_PREFIX} metadata unavailable — retry after TMDB recovers"
    try:
        raw_genres = tmdb.details(title.tmdb_id, title.media_type).get("genres")
        if (
            not isinstance(raw_genres, list)
            or not raw_genres
            or any(not isinstance(g, dict) or type(g.get("id")) is not int for g in raw_genres)
        ):
            return unknown
        genres = {g["id"] for g in raw_genres}
        if {_MUSIC, _DOCUMENTARY} <= genres:
            return f"{CONTENT_REASON_PREFIX} music documentary"
        if _DOCUMENTARY not in genres and (_MUSIC not in genres or genres & _FICTION_GENRES):
            return ""
        keywords = tmdb.keyword_ids_for(title.tmdb_id)
        if _DOCUMENTARY in genres and keywords & _MUSIC_DOCUMENTARY_KEYWORDS:
            return f"{CONTENT_REASON_PREFIX} music documentary or concert film"
        if _MUSIC in genres and not genres & _FICTION_GENRES and keywords & _CONCERT_KEYWORDS:
            return f"{CONTENT_REASON_PREFIX} concert or live music performance"
    except Exception:
        # Exceptions can contain authenticated URLs. The inbox needs the cause, not their payload.
        return unknown
    return ""
