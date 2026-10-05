"""Fake Overseerr and fake Radarr/Sonarr: the request sources a "Your requests" row reads (issue #127).

Three FastAPI apps on loopback, served the way ``tests/e2e/conftest.py`` serves the fake TMDB. Every
response mirrors a recorded fixture — ``overseerr_requests_page.json``, ``overseerr_users_page.json``,
``overseerr_arr_settings.json``, ``radarr_request_tags.json``, ``sonarr_request_tags.json`` — with the
ids rewritten onto the fake Plex roster, so a request's ``requestedBy.plexId`` IS a seeded person's
``plex_account_id`` and its ``media.tmdbId`` IS a seeded title's TMDB id.

No easier than the real servers (tests/rules): every route but Overseerr's ``/status`` (declared
``security: []`` upstream) demands ``X-Api-Key``; the paged endpoints honour ``take``/``skip`` and
report ``pageInfo.results``, with Overseerr's own default page of 20 when ``take`` is absent, so a
client that stops after the first batch reads a short list.

The story the data tells: **sarah** (plex 201, Overseerr user 10) asked for one movie and one show.
The movie is an Overseerr request that Radarr also carries under her requester tag; the show is
known only by its Sonarr tag (its request was tidied away), so its arrival date has to come from
Overseerr's media table. Nobody else asked for anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Request

from tests.fakes.fake_plex import FakePlexState

SEERR_API_KEY = "fake-seerr-key"
ARR_API_KEY = "fake-arr-key"

#: The requester: sarah on the fake Plex roster, user 10 in the fake Overseerr.
REQUESTER_PLEX_ID = 201
REQUESTER_SEERR_ID = 10
REQUESTER_USERNAME = "sarah"
#: What she asked for — rating keys in the seeded catalogue (`seed_state`). Both are outside every
#: seeded watch list, so they count as unwatched for her.
REQUESTED_MOVIE_KEY = 120
REQUESTED_SHOW_KEY = 320
#: Seerr's requester tag as Radarr/Sonarr store it: ``<seerrUserId>-<username>``.
REQUESTER_TAG = f"{REQUESTER_SEERR_ID}-{REQUESTER_USERNAME}"
REQUESTER_TAG_ID = 50
#: Overseerr's default page size when a caller sends no ``take`` (server/routes/request.ts).
_DEFAULT_TAKE = 20

# Seerr's enums (server/constants/media.ts): request COMPLETED, media AVAILABLE.
_REQ_COMPLETED = 5
_MEDIA_AVAILABLE = 5


def _iso(days_ago: int) -> str:
    """A UTC stamp `days_ago` days back, in the ``2026-09-27T01:45:37.000Z`` form the fixtures carry."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _require_key(request: Request, key: str) -> None:
    if request.headers.get("X-Api-Key") != key:
        raise HTTPException(status_code=401, detail="API key required")


def _paged(rows: list[dict], request: Request) -> dict:
    """The ``{pageInfo, results}`` envelope every Overseerr list endpoint answers with.

    Shape from ``overseerr_requests_page.json``: ``pageInfo`` carries the TOTAL (``results``), not the
    batch size, which is what lets a client notice a capped ``take``.
    """
    take = int(request.query_params.get("take", _DEFAULT_TAKE))
    skip = int(request.query_params.get("skip", 0))
    pages = max(1, -(-len(rows) // take)) if take > 0 else 1
    return {
        "pageInfo": {"pages": pages, "pageSize": take, "results": len(rows), "page": skip // take + 1},
        "results": rows[skip : skip + take],
    }


def _seerr_user(uid: int, plex_id: int | None, username: str, *, permissions: int = 4194464) -> dict:
    """A user row as ``overseerr_users_page.json`` records one (``plexId`` null for a local account)."""
    return {
        "id": uid,
        "email": f"{username}@example.test",
        "username": None if plex_id else username,
        "plexUsername": username if plex_id else None,
        "displayName": username,
        "plexId": plex_id,
        "userType": 1 if plex_id else 2,
        "permissions": permissions,
        "requestCount": 1 if uid == REQUESTER_SEERR_ID else 0,
        "avatar": "https://plex.tv/users/0000000000000000/avatar?c=0",
        "movieQuotaLimit": None,
        "movieQuotaDays": None,
        "tvQuotaLimit": None,
        "tvQuotaDays": None,
        "createdAt": "2026-01-02T10:00:27.000Z",
        "updatedAt": "2026-08-30T10:00:27.000Z",
    }


def _seerr_users(state: FakePlexState) -> list[dict]:
    return [
        _seerr_user(1, state.owner_account_id, state.owner_username, permissions=2),
        _seerr_user(REQUESTER_SEERR_ID, REQUESTER_PLEX_ID, REQUESTER_USERNAME),
        # A local account that never signed in with Plex: `plexId` null, which the client must skip.
        _seerr_user(11, None, "local-only"),
    ]


def _seerr_media(state: FakePlexState) -> list[dict]:
    """Seerr's media table (``/media``): the arrival dates a tag-only title is dated from.

    Both of sarah's titles are here, as they would be on a real instance — the movie's row is what
    its request embeds, the show's row outlives the deleted request.
    """
    movie = state.movies[REQUESTED_MOVIE_KEY]
    show = state.shows[REQUESTED_SHOW_KEY]
    return [
        {
            "id": 25970,
            "mediaType": "movie",
            "tmdbId": movie.tmdb_id,
            "tvdbId": None,
            "status": _MEDIA_AVAILABLE,
            "status4k": 1,
            "createdAt": _iso(12),
            "updatedAt": _iso(9),
            "lastSeasonChange": _iso(9),
            "mediaAddedAt": _iso(9),
            "downloadStatus": [],
            "downloadStatus4k": [],
            "ratingKey": str(movie.rating_key),
            "ratingKey4k": None,
        },
        {
            "id": 25971,
            "mediaType": "tv",
            "tmdbId": show.tmdb_id,
            "tvdbId": 422752,
            "status": _MEDIA_AVAILABLE,
            "status4k": 1,
            "createdAt": _iso(20),
            "updatedAt": _iso(6),
            "lastSeasonChange": _iso(6),
            "mediaAddedAt": _iso(7),
            "downloadStatus": [],
            "downloadStatus4k": [],
            "ratingKey": str(show.rating_key),
            "ratingKey4k": None,
        },
    ]


def _seerr_requests(state: FakePlexState) -> list[dict]:
    """One completed movie request from sarah, shaped like ``overseerr_requests_page.json``."""
    requester = _seerr_user(REQUESTER_SEERR_ID, REQUESTER_PLEX_ID, REQUESTER_USERNAME)
    return [
        {
            "id": 753,
            "status": _REQ_COMPLETED,
            "createdAt": _iso(12),
            "updatedAt": _iso(9),
            "type": "movie",
            "is4k": False,
            "serverId": None,
            "profileId": None,
            "rootFolder": None,
            "languageProfileId": None,
            "tags": None,
            "isAutoRequest": False,
            "ignoreQuota": False,
            "media": _seerr_media(state)[0],
            "seasons": [],
            "modifiedBy": requester,
            "requestedBy": requester,
        }
    ]


def _seerr_arr_server(kind: str) -> dict:
    """One Radarr/Sonarr entry as ``overseerr_arr_settings.json`` records it — ``tagRequests`` on."""
    return {
        "name": kind.capitalize(),
        "hostname": "host.example.test",
        "port": 443,
        "apiKey": "REDACTED",
        "useSsl": True,
        "baseUrl": "",
        "activeProfileId": 10,
        "activeProfileName": "example",
        "activeDirectory": "example",
        "is4k": False,
        "minimumAvailability": "released",
        "tags": [3],
        "isDefault": True,
        "syncEnabled": True,
        "preventSearch": False,
        "tagRequests": True,
        "id": 0,
        "externalUrl": None,
    }


def make_fake_seerr(state: FakePlexState, *, api_key: str = SEERR_API_KEY) -> FastAPI:
    """A fake Overseerr: ``/api/v1`` routes the requests row and its setup check read.

    Rows are built from ``state`` on every call, so a re-seeded catalogue (the e2e harness re-seeds
    between tests) is reflected without rebuilding the server.
    """
    app = FastAPI()

    @app.get("/api/v1/status")
    def status() -> dict:
        # Unauthenticated on a real instance too (`security: []`), which is why the client's ping
        # deliberately reads `/auth/me` instead.
        return {
            "version": "1.34.0",
            "commitTag": "fakefake",
            "updateAvailable": False,
            "commitsBehind": 0,
            "restartRequired": False,
        }

    @app.get("/api/v1/auth/me")
    def auth_me(request: Request) -> dict:
        _require_key(request, api_key)
        return _seerr_users(state)[0]

    @app.get("/api/v1/user")
    def users(request: Request) -> dict:
        _require_key(request, api_key)
        return _paged(_seerr_users(state), request)

    @app.get("/api/v1/request")
    def requests(request: Request) -> dict:
        _require_key(request, api_key)
        return _paged(_seerr_requests(state), request)

    @app.get("/api/v1/media")
    def media(request: Request) -> dict:
        _require_key(request, api_key)
        return _paged(_seerr_media(state), request)

    @app.get("/api/v1/settings/radarr")
    def settings_radarr(request: Request) -> list[dict]:
        _require_key(request, api_key)
        return [_seerr_arr_server("radarr")]

    @app.get("/api/v1/settings/sonarr")
    def settings_sonarr(request: Request) -> list[dict]:
        _require_key(request, api_key)
        return [_seerr_arr_server("sonarr")]

    return app


def _tags() -> list[dict]:
    """``/api/v3/tag`` as ``radarr_request_tags.json`` records it: requester tags beside unrelated ones."""
    return [
        {"id": REQUESTER_TAG_ID, "label": REQUESTER_TAG},
        {"id": 57, "label": "17-person17"},
        {"id": 43, "label": "other-tag"},
    ]


def _radarr_movies(state: FakePlexState) -> list[dict]:
    """Two movies: sarah's requested one under her tag and on disk, and an untagged one."""
    wanted = state.movies[REQUESTED_MOVIE_KEY]
    other = state.movies[REQUESTED_MOVIE_KEY + 1]

    def row(movie, tags: list[int], *, movie_id: int) -> dict:
        return {
            "id": movie_id,
            "title": movie.title,
            "year": movie.year,
            "tmdbId": movie.tmdb_id,
            "imdbId": f"tt{movie.tmdb_id:07d}",
            "tags": tags,
            "added": _iso(12),
            "hasFile": True,
            "monitored": True,
            "status": "released",
            "path": f"/media/movie/{movie_id}",
            "rootFolderPath": "/media/movie",
            "movieFile": {"id": movie_id, "dateAdded": _iso(9)},
            "statistics": {"movieFileCount": 1, "sizeOnDisk": 1, "releaseGroups": [], "movieFileQualities": []},
        }

    return [row(wanted, [3, REQUESTER_TAG_ID], movie_id=10302), row(other, [3], movie_id=10303)]


def _sonarr_series(state: FakePlexState) -> list[dict]:
    """Two series: sarah's tagged show with every episode on disk, and an untagged one."""
    wanted = state.shows[REQUESTED_SHOW_KEY]
    other = state.shows[REQUESTED_SHOW_KEY + 1]

    def row(show, tags: list[int], *, series_id: int) -> dict:
        return {
            "id": series_id,
            "title": show.title,
            "year": show.year,
            "tmdbId": show.tmdb_id,
            "tvdbId": 422752 if show is wanted else 344280,
            "imdbId": f"tt{show.tmdb_id:07d}",
            "tags": tags,
            "added": _iso(20),
            "monitored": True,
            "status": "continuing",
            "path": f"/media/series/{series_id}",
            "rootFolderPath": "/media/series",
            "statistics": {
                "seasonCount": 1,
                "episodeFileCount": show.leaf_count,
                "episodeCount": show.leaf_count,
                "totalEpisodeCount": show.leaf_count,
                "sizeOnDisk": 1,
                "releaseGroups": [],
                "percentOfEpisodes": 100,
            },
            "seasons": [{"seasonNumber": 1, "monitored": True}],
        }

    return [row(wanted, [33, REQUESTER_TAG_ID], series_id=5153), row(other, [33], series_id=5154)]


def make_fake_arr(kind: str, state: FakePlexState, *, api_key: str = ARR_API_KEY) -> FastAPI:
    """A fake Radarr (``kind="radarr"``) or Sonarr (``"sonarr"``): the ``/api/v3`` routes a requests row reads.

    ``/tag`` and ``/movie`` (or ``/series``) mirror ``radarr_request_tags.json`` /
    ``sonarr_request_tags.json`` — the requester tag on one item, plus tags and items that are nobody's.
    """
    if kind not in ("radarr", "sonarr"):
        raise ValueError(f"kind must be radarr or sonarr, not {kind!r}")
    app = FastAPI()

    @app.get("/api/v3/system/status")
    def system_status(request: Request) -> dict:
        _require_key(request, api_key)
        return {"appName": kind.capitalize(), "version": "5.14.0.9383" if kind == "radarr" else "4.0.10.2544"}

    @app.get("/api/v3/tag")
    def tags(request: Request) -> list[dict]:
        _require_key(request, api_key)
        return _tags()

    if kind == "radarr":

        @app.get("/api/v3/movie")
        def movies(request: Request) -> list[dict]:
            _require_key(request, api_key)
            return _radarr_movies(state)

    else:

        @app.get("/api/v3/series")
        def series(request: Request) -> list[dict]:
            _require_key(request, api_key)
            return _sonarr_series(state)

    return app
