"""Assemble an EngineContext (and the user profiles a run processes) from DB settings.

This is the server's translation layer: DB rows and typed settings in, engine dataclasses and
clients out. It holds no run state and writes no run rows — that is the run service's job. Kept
separate so the run service is only about orchestration (gate, execute, persist).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.clients.mdblist import MdbListClient
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.search import ExaClient, SearxngClient, WebSearchProvider
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.context import EngineContext
from shortlist.engine.curator import make_curator
from shortlist.engine.delivery import render_row_name
from shortlist.engine.history import ShareTokenWatchSource, distinct_recent, ratings_are_trustworthy
from shortlist.engine.models import (
    ArrTarget,
    EngineConfig,
    HubAnchor,
    MediaType,
    Pick,
    PosterSpec,
    RequestConfig,
    RequestOverrides,
    RowOverride,
    RowSpec,
    UserProfile,
    UserType,
    normalise_languages,
    row_language_mode_or_inherit,
    row_languages_or_inherit,
    row_monitor_or_inherit,
)
from shortlist.server.db.adapters import DbCache, DbSnapshotStore
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    Delivery,
    PickRow,
    RequestCandidate,
    Server,
    User,
    WatchedTitle,
    WatchSyncState,
    iso_utc,
    utcnow,
)
from shortlist.server.prefs import blocked_ids
from shortlist.server.services.poster_service import load_upload, make_studio
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SettingsStore


def curator_kwargs(get: Callable[[str], object]) -> dict:
    """Assemble ``make_curator`` kwargs from settings. A local/OpenAI-compatible server takes a
    base_url and an OPTIONAL key; every other provider takes an api_key; an optional model applies
    to all.

    The single source of truth the runtime context and the settings 'Test' probe both build from —
    so a change to how a provider is configured can't drift between them."""
    kwargs: dict = {}
    provider = get("curator.provider")
    if provider in ("openai_compatible", "ollama"):
        # A local server usually wants no key at all, but a hosted gateway (OpenRouter) does — so
        # the key is passed when set and the curator substitutes a placeholder when it isn't.
        # `curator.ollama_url` is read as a fallback for instances configured before the two
        # providers were merged, whose URL still lives under the old key.
        kwargs["base_url"] = get("curator.openai_base_url") or get("curator.ollama_url")
        if get("curator.api_key"):
            kwargs["api_key"] = get("curator.api_key")
    elif get("curator.api_key"):
        kwargs["api_key"] = get("curator.api_key")
    if get("curator.model"):
        kwargs["model"] = get("curator.model")
    return kwargs


def make_search_client(get: Callable[[str], object]) -> WebSearchProvider | None:
    """The external web-search backend for the ``llm_web`` source, or None when there isn't one.

    Deciding WHICH provider belongs here rather than in the engine: the engine only knows "an
    external backend" (``candidates.EXTERNAL_SEARCH_MODES``), so adding a provider never touches it.

    The backend is whatever the owner chose, and ONLY that: an unconfigured ``exa``/``searxng``
    yields None rather than falling through to the other. A fallback would be actively harmful in
    both directions — it would send a self-hoster's watch history to a paid vendor they didn't pick,
    or quietly downgrade a paying owner to a box that may be switched off.

    Args:
        get: A settings reader (``store.get``).

    Returns:
        A configured provider, or None when the backend is ``native`` or its own setup is missing.
    """
    mode = get("llm_web.search_provider") or "native"
    if mode == "exa":
        key = get("exa.apikey")
        return ExaClient(key) if key else None
    if mode == "searxng":
        url = (str(get("searxng.url") or "")).strip()
        if not url:
            return None
        return SearxngClient(
            url,
            username=str(get("searxng.username") or ""),
            password=str(get("searxng.password") or ""),
        )
    return None  # native: the provider searches for itself, so there is no external client


def _dislike_threshold(store: SettingsStore) -> float:
    """The 0..10 rating at or below which a title stops seeding.

    `or 2.0` would be wrong here and was: 0.0 is a legal, documented, validator-accepted value — it
    means "only a rating of exactly zero counts as dislike" — and it is falsy, so the owner's stored
    0 was silently replaced by the default. The settings screen read back 0 while every run used 2,
    which is the worst kind of disagreement: both screens are self-consistent and neither is right.
    """
    raw = store.get("recommendations.dislike_threshold")
    return 2.0 if raw is None else float(raw)


def _refuse_a_different_server(session, machine_id: str) -> None:
    """Abort before touching a Plex server that is not the one this instance is linked to.

    Every record Shortlist holds is scoped to one machine: the delivery ledger says which collection is
    whose, `restriction_snapshots` holds each account's filters as they were before we touched them,
    and the user table says who the owner is. Run any of that against a different server and the
    bookkeeping describes a machine nobody is talking to.

    The concrete danger is the privacy sync: a stranger's PMS enumerates ZERO Shortlist collections,
    which reads as "every row is gone" — and the merge would then rewrite share filters on plex.tv
    from that. Settings already refuses a repoint (`api/settings._reject_a_different_server`), but only
    when the new server ANSWERS at save time; a box that is down then, and up later, slips past. This
    is the check at the point of use, where it cannot be skipped.
    """
    server = session.query(Server).first()
    if server is None or not server.machine_id or server.machine_id == machine_id:
        return
    raise RuntimeError(
        f"Plex at this URL reports machine {machine_id}, but Shortlist is linked to {server.machine_id}. "
        "Refusing to run against a different server — re-link from setup if the move is intentional."
    )


def _optional_float(raw: object) -> float | None:
    """A stored number, or None when the setting is unset — never a silent 0.0.

    Used for the settings whose None is a MEANING ("derive this") rather than an absence, where the
    usual ``float(store.get(...) or default)`` would erase a deliberate 0.0.
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def row_request_overrides(collection: Collection) -> RequestOverrides | None:
    """This row's own request floors and target, or None when it overrides nothing.

    None rather than an all-None ``RequestOverrides`` so the engine can skip the resolve entirely
    for the overwhelmingly common case of a row that inherits everything.

    Shared rows never get one: a shared row is built from titles people have already WATCHED,
    which are by definition already on the server, so it surfaces nothing missing to request
    (`_shared_row` is passed no demand map at all). Handing it request settings would put controls
    in the editor that could not do anything.
    """
    if collection.build == "shared":
        return None
    overrides = RequestOverrides(
        min_rating=collection.req_min_rating,
        min_votes=collection.req_min_votes,
        min_demand=collection.req_min_demand,
        min_year=collection.req_min_year,
        max_year=collection.req_max_year,
        auto_send=collection.req_auto_send,
        auto_min_demand=collection.req_auto_min_demand,
        auto_min_rating=collection.req_auto_min_rating,
        max_per_row=collection.req_max_per_row,
        radarr_quality_profile_id=collection.req_radarr_quality_profile_id,
        radarr_root_folder=collection.req_radarr_root_folder or None,
        sonarr_quality_profile_id=collection.req_sonarr_quality_profile_id,
        sonarr_root_folder=collection.req_sonarr_root_folder or None,
        sonarr_monitor=row_monitor_or_inherit(collection.req_sonarr_monitor),
        language_mode=row_language_mode_or_inherit(collection.req_language_mode),
        preferred_languages=row_languages_or_inherit(collection.req_preferred_languages),
        min_rating_other=collection.req_min_rating_other,
    )
    return overrides if overrides != RequestOverrides() else None


class ContextBuilder:
    """Builds an EngineContext and user profiles from DB settings — the engine's server adapter."""

    def __init__(self, session_factory: sessionmaker[Session], secrets, bus: EventBus):
        self._sessions = session_factory
        self._secrets = secrets
        self._bus = bus

    def build(
        self,
        *,
        dry_run: bool,
        loop: asyncio.AbstractEventLoop | None = None,
        run_id: int | None = None,
        log_sink: Callable[[dict], None] | None = None,
        collection_ids: list[int] | None = None,
    ) -> EngineContext:
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            plex_url = store.get("plex.url")
            plex_token = store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet — finish setup first")
            # A large TV library's collection rebuild legitimately takes 15-20s+; the configured
            # per-call timeout (default 45s) gives those headroom instead of timing out + retrying.
            plex = PlexClient(plex_url, plex_token, timeout=int(store.get("plex.timeout_s") or 45))
            _refuse_a_different_server(session, plex.machine_id)
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
            trakt = (
                TraktClient(store.get("trakt.client_id"), cache=DbCache(self._sessions, kind="trakt"))
                if store.get("trakt.client_id")
                else None
            )
            # External web-search backend for the llm_web source; None when none is configured (the
            # native provider tools still work without it — only Ollama depends on it).
            search = make_search_client(store.get)
            history = ShareTokenWatchSource(plex, plextv, owner_token=plex_token)

            def _pms_for_user(profile, _history=history, _url=plex_url):
                """The server as ONE user sees it, for the privacy check on accounts Plex refuses a
                hide-list for.

                Reuses `ShareTokenWatchSource._token_for` rather than reading the shared-server list
                directly, for two reasons. It has the CANARY fallback: a managed Home profile that was
                never separately shared is absent from `shared_server_tokens()` — and that is exactly
                the archetype this check exists for, so a bare lookup returned None and the account was
                recorded as seeing nothing, which is the false all-clear the whole feature was written
                to stop. It also memoises the roster behind a lock, so this does not re-fetch plex.tv
                once per profiled account per run (rule 6).
                """
                token = _history._token_for(profile)
                return PlexClient(_url, token, timeout=int(store.get("plex.timeout_s") or 45)) if token else None

            provider = store.get("curator.provider")
            curator = make_curator(provider, **curator_kwargs(store.get))
            # Build the poster studio only if a row actually renders a poster from text (built-in or
            # AI) — a server that never uses posters never touches Pillow or the image SDK. The studio
            # always provides the text engine; its AI engine is None when the provider can't make images.
            render_modes = {"text", "ai", "generate"}
            wants_studio = any((c.poster or {}).get("mode") in render_modes for c in session.query(Collection).all())
            poster_artist = make_studio(store, self._sessions) if wants_studio else None
            config = self._engine_config(session, store, dry_run=dry_run, collection_ids=collection_ids)
            previous = self._previous_picks(session)
            previous_recipes = self._previous_recipes(previous)
            delivered_keys = self._delivered_keys(session)
            # Opted-out accounts: with hide_shared_from_disabled, even public shared rows are hidden
            # from them, so disabling a user removes them from Shortlist entirely.
            disabled_account_ids = {u.plex_account_id for u in session.query(User).filter_by(enabled=False).all()}
            # Accounts the owner has told Shortlist to leave alone: no excludes merged into their share
            # filters, and any we already added taken back out. Independent of `enabled` above — the
            # two answer different questions (does this person get a row / may we edit their sharing).
            unmanaged_account_ids = {
                u.plex_account_id for u in session.query(User).filter_by(manage_sharing=False).all()
            }
            concurrency = int(store.get("run.concurrency") or 1)
            # Every user Shortlist knows, enabled or not: the engine answers "whose row is this?"
            # by account id, because a name can change and two names can slugify alike.
            known_slugs = {u.plex_account_id: u.slug for u in session.query(User).all()}
            # People our records say are GONE — plex.tv stopped listing them (`departed_at`, set and
            # cleared by the roster sweep) or the owner filed them away (`removed_at`). This is the
            # ONLY thing that lets a private-row exclude be pruned, so it has to be an assertion we
            # made, not an inference from who happens to be in tonight's run.
            departed_slugs = {
                u.slug
                for u in session.query(User).filter((User.departed_at.isnot(None)) | (User.removed_at.isnot(None)))
            }
            # Whose rows are allowed on the owner's Home. Read from the DB rather than this run's
            # profiles, because the owner may be paused, disabled, or simply not in a scoped run —
            # and converge still has to know which single label is legitimately there.
            owner = session.query(User).filter_by(user_type=UserType.OWNER.value).first()
            owner_slug = owner.slug if owner else ""
            # Paused users never appear in a run, so converge is the only pass that can take their
            # rows down. Read from the DB rather than this run's profiles for exactly that reason.
            paused_slugs = {u.slug for u in session.query(User).all() if (u.prefs or {}).get("paused")}

            # INSIDE the `with`, deliberately. Two of the arguments below still touch `session`
            # (`_handled_requests`, and `_build_mdblist` via `SettingsStore.get`). Built after the
            # block closed, SQLAlchemy silently re-opened a transaction that nothing ever closed, so
            # every `build_context()` checked a connection out of the pool and kept it until GC —
            # and `build_context` is on the path of every run, job and reconcile. The pool is 5 + 10,
            # so the eleventh build blocked for 30s and surfaced as "Plex is unreachable".
            def progress(slug: str, stage: str, counts: dict, reason: str | None = None) -> None:
                # Runs in the engine's executor thread. One entry both STREAMS (SSE, live) and, via
                # log_sink, lands in the run's in-memory activity log so a page reload can replay it.
                # `reason` is kept OUT of `counts`, which is a map of numbers the UI renders as a
                # "113 history · 40 seeds" tally — a sentence in there would render as garbage.
                entry = {"ts": iso_utc(utcnow()), "run_id": run_id, "user": slug, "stage": stage, "counts": counts}
                if reason:
                    entry["reason"] = reason
                if log_sink is not None:
                    log_sink(entry)
                if loop is not None:
                    loop.call_soon_threadsafe(self._bus.publish, "run.user.stage", entry)

            return EngineContext(
                config=config,
                plex=plex,
                plextv=plextv,
                tmdb=tmdb,
                trakt=trakt,
                search=search,
                poster_artist=poster_artist,
                # The engine reads each user's COMPLETE watched set by reading the PMS AS them, with the
                # per-user server token plex.tv mints for every share. That set carries their own
                # viewCount/viewedLeafCount — so a mark-as-watched (which the playback-history API never
                # returns, and which capped at ~200 plays) is seen, with no PMS database mount.
                history_source=history,
                curator=curator,
                snapshots=DbSnapshotStore(self._sessions),
                index_cache=DbCache(self._sessions, kind="library_index"),
                web_search_cache=DbCache(self._sessions, kind="websearch"),
                mdblist=self._build_mdblist(store),
                concurrency=concurrency,
                previous_picks=previous,
                previous_recipes=previous_recipes,
                delivered_keys=delivered_keys,
                pms_for_user=_pms_for_user,
                # Same token `_pms_for_user` builds its client from — including the canary fallback
                # for a Home profile that was never separately shared.
                token_for_user=lambda profile, _history=history: _history._token_for(profile),
                disabled_account_ids=disabled_account_ids,
                unmanaged_account_ids=unmanaged_account_ids,
                known_slugs=known_slugs,
                departed_slugs=departed_slugs,
                owner_slug=owner_slug,
                paused_slugs=paused_slugs,
                # The DB read above succeeded, so `known_slugs` lists every user Shortlist has — the
                # complete picture converge needs before it may DELETE an unattributable collection.
                may_delete_orphans=True,
                handled_requests=self._handled_requests(session),
                progress=progress,
            )

    def _build_mdblist(self, store: SettingsStore) -> MdbListClient | None:
        """A cache-backed MDBList client when any feature needs a non-TMDB rating, else None. Shares
        the persistent DB cache so ratings are looked up at most once per title per week — the whole
        point of caching against MDBList's daily request cap.

        TWO settings can ask for one, and either alone is enough: `requests.rating_source` gates which
        missing titles are worth requesting, and `recommendations.rating_source` decides what a row
        ordered by "Highest rated" sorts on. Checking only the requests one left row ordering silently
        inert on every default install — the engine no-opped while the row editor said "Highest IMDb
        score first", which is the worst of both (nothing happens, and the UI says otherwise)."""
        wants = {
            store.get("requests.rating_source") or "tmdb",
            store.get("recommendations.rating_source") or "tmdb",
        }
        if wants == {"tmdb"}:
            return None
        key = store.get("requests.mdblist.apikey")
        if not key:
            return None
        return MdbListClient(key, cache=DbCache(self._sessions, kind="mdblist"))

    @staticmethod
    def _handled_requests(session: Session) -> set[tuple[int, str]]:
        """Titles the owner already sent or rejected in the inbox — the engine must not re-request them.

        Without this, a title still downloading was still "missing", so it out-ranked everything by
        demand and re-consumed a `max_per_run` slot every single night — the queue starved on the
        same five titles forever. And a rejected title could be auto-sent by a later run, so a "no"
        wasn't a no.
        """
        rows = session.query(RequestCandidate).filter(RequestCandidate.status.in_(("sent", "rejected"))).all()
        return {(row.tmdb_id, row.media_type) for row in rows}

    def build_plex_only(self, *, dry_run: bool) -> EngineContext:
        """A context with the PMS, plex.tv and the watch-history source — and nothing else.

        For the handlers that only ever walk collections under a label: removing a disabled user's
        rows, hiding a paused user's, the row reconciles, the poster reset, the rename, and the
        read-only watch-history sync. `build()` opens TMDB, Trakt, Exa and MDBList clients, constructs
        the LLM curator, and scans the whole `Collection` table to decide whether to build the poster
        studio — none of which any of those touch, and all of which couple them to the availability of
        services they never call. A watch sync failing because an LLM key is wrong is not a failure
        anyone can act on.

        The curator is the NullCurator and TMDB is unkeyed-but-real, because `EngineContext` requires
        both; nothing on these paths calls either. `_refuse_a_different_server` still runs — it is what
        stops a reconcile enumerating a stranger's PMS and concluding every row is gone.
        """
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            plex_url = store.get("plex.url")
            plex_token = store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet — finish setup first")
            plex = PlexClient(plex_url, plex_token, timeout=int(store.get("plex.timeout_s") or 45))
            _refuse_a_different_server(session, plex.machine_id)
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            return EngineContext(
                config=EngineConfig(dry_run=dry_run),
                plex=plex,
                plextv=plextv,
                tmdb=TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions)),
                history_source=ShareTokenWatchSource(plex, plextv, owner_token=plex_token),
                curator=make_curator(""),
                snapshots=DbSnapshotStore(self._sessions),
            )

    def build_requests_only(self) -> tuple[RequestConfig | None, TmdbClient]:
        """Just the pieces the approval inbox's manual send needs: the request config and a TMDB client.

        A request asks Sonarr/Radarr for a file — it touches no Plex object — so this deliberately does
        NOT build a full EngineContext, which would connect to the PMS and construct the LLM curator and
        thereby couple a manual send to Plex/LLM availability the send never uses.
        """
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
            return self._build_requests(store), tmdb

    def user_history(self, user_id: int, *, limit: int = 25) -> list[dict] | None:
        """Recent watches for one user, newest first — the same source that feeds recommendations.

        Returns None if the user doesn't exist. Raises RuntimeError if Plex isn't configured yet.
        """
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            user = session.get(User, user_id)
            if user is None:
                return None
            plex_url, plex_token = store.get("plex.url"), store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet")
            profile = UserProfile(
                username=user.username,
                plex_account_id=user.plex_account_id,
                user_type=UserType(user.user_type),
                slug=user.slug,
            )
            plex = PlexClient(plex_url, plex_token)
            plextv = PlexTvClient(plex_token, plex.machine_id)
            history = ShareTokenWatchSource(plex, plextv, owner_token=plex_token)
        # A lower completion bar than a run uses: this is "what they've been watching", not seeds.
        # Distinct titles, newest first: a show's episodes collapse to the one show (keeping its most
        # recent episode's detail), so a binge shows as one entry and the list reflects real variety —
        # looking back through the whole history to fill `limit` distinct titles.
        items = history.fetch(profile, min_completion=0.5)
        return [
            {
                "title": w.title,
                # Carried so the UI can block a seed straight from a watch. It is the ONLY identifier
                # a block can key on, and it is None for anything with no tmdb:// GUID — the caller
                # must treat "no id" as "not blockable from here" rather than inventing one.
                "tmdb_id": w.tmdb_id,
                "media_type": w.media_type.value,
                "watched_at": w.watched_at.isoformat(),
                "year": w.year,
                "season": w.season,
                "episode": w.episode,
                "episode_title": w.episode_title,
            }
            for w in distinct_recent(items, limit)
        ]

    def user_watched(
        self,
        user_id: int,
        *,
        q: str = "",
        media_type: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> dict | None:
        """One person's watched set, searchable — read from the `watched_titles` CACHE, not from Plex.

        This is deliberately a different source from `user_history`, which reads the PMS live. The
        cache is what every recommendation is filtered against, so showing it here means the page
        answers "why was this recommended when I've seen it?" instead of showing a second, unrelated
        list. The cost is honesty about staleness, which is why `last_full_sync_at` and
        `synced_titles` come back with the page and the UI states them.

        Args:
            user_id: The person to read.
            q: Case-insensitive substring of the title. Empty matches everything.
            media_type: "movie" or "show" to filter; empty for both.
            limit: Page size.
            offset: Rows to skip, for paging.

        Returns:
            ``{items, total, last_full_sync_at, synced_titles}``, or None if the user doesn't exist.
        """
        with self._sessions() as session:
            if session.get(User, user_id) is None:
                return None
            query = session.query(WatchedTitle).filter(WatchedTitle.user_id == user_id)
            if q.strip():
                # `ilike` over one person's rows only — the (user_id, viewed_at) index keeps the scan
                # inside their few thousand, so no separate title index is needed. `%`/`_` are escaped
                # so a title containing them searches literally rather than as a wildcard.
                pattern = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                query = query.filter(WatchedTitle.title.ilike(f"%{pattern}%", escape="\\"))
            if media_type in ("movie", "show"):
                query = query.filter(WatchedTitle.media_type == media_type)
            total = query.count()
            # The TRUE watch date, same as `WatchCache.watched_set` — a transferred history is dated
            # by when the person actually watched, not by the day the scrobbles were written.
            true_date = func.coalesce(WatchedTitle.source_viewed_at, WatchedTitle.viewed_at)
            rows = query.order_by(true_date.desc()).limit(limit).offset(offset).all()
            # Across ALL libraries: `watch_sync_state` is per (person, library), and the page's claim
            # is "your history is complete as of X". The OLDEST full read is the only honest X — one
            # library synced an hour ago says nothing about the one that hasn't synced since Tuesday.
            states = session.query(WatchSyncState).filter(WatchSyncState.user_id == user_id).all()
            fulls = [s.last_full_at for s in states if s.last_full_at is not None]
            oldest_full = min(fulls) if fulls and len(fulls) == len(states) else None
            return {
                "items": [
                    {
                        "title": row.title,
                        "tmdb_id": row.tmdb_id,
                        "media_type": row.media_type,
                        "watched_at": (row.source_viewed_at or row.viewed_at).isoformat(),
                        "year": row.year,
                        "watch_count": row.watch_count,
                        "viewed_leaf_count": row.viewed_leaf_count,
                        "leaf_count": row.leaf_count,
                        "user_rating": row.user_rating,
                    }
                    for row in rows
                ],
                "total": total,
                # None when ANY library has never had a full read — "synced 4h ago" would be a false
                # claim of completeness while a whole library is still missing from the set.
                "last_full_sync_at": oldest_full.isoformat() if oldest_full else None,
                "synced_titles": sum(s.item_count for s in states),
                # The two facts the page needs to say WHY a rating is or isn't acting, without
                # reimplementing the rule client-side. Both are properties of the whole set rather
                # than of a row: the threshold is a server setting, and trust is judged across all of
                # this person's ratings at once. Deciding it here also means the page can never
                # disagree with the engine about which titles are seeding.
                **self._rating_state(session, user_id),
            }

    def _rating_state(self, session: Session, user_id: int) -> dict:
        """Whether this person's Plex ratings are being acted on, and from what threshold.

        `trusted` is False when their ratings look tool-written rather than typed — the same
        account-level judgement `history.ratings_are_trustworthy` makes, over the same values, so the
        page and the run agree. Judged over EVERY rating they have, not just the page on screen: a
        page-sized sample would flip the verdict as the user paged around.
        """
        store = SettingsStore(session)
        threshold = _dislike_threshold(store)
        enabled = bool(store.get("recommendations.use_plex_ratings"))
        ratings = [
            r
            for (r,) in session.query(WatchedTitle.user_rating).filter(
                WatchedTitle.user_id == user_id, WatchedTitle.user_rating.isnot(None)
            )
        ]
        return {
            "dislike_threshold": threshold if enabled else None,
            "ratings_trusted": ratings_are_trustworthy(ratings),
            "rated_count": len(ratings),
        }

    def _delivered_keys(self, session: Session) -> dict[tuple[str, str, str], int]:
        """The delivery ledger as the engine wants it: (user_slug, row_slug, section_key) -> ratingKey.

        Delivery uses it to answer "is this collection mine, under a title I no longer render?" by
        IDENTITY rather than by counting rows — see `_deliver_one`. Same key shape as
        `_previous_picks`, so the two read alike at the call site.

        An ambiguous key (two rows naming one collection — reachable if a run died between the delete
        and the persist on the rebuild path) is dropped rather than arbitrated: delivery then falls back
        to the title, which is where it was before the ledger.
        """
        rows = list(session.query(Delivery).filter(Delivery.rating_key != 0))
        claims: dict[int, int] = {}
        for row in rows:
            claims[row.rating_key] = claims.get(row.rating_key, 0) + 1
        keys = {
            (row.user_slug, row.collection_slug, row.library_key): row.rating_key
            for row in rows
            if claims[row.rating_key] == 1
        }
        if len(keys) != len(rows):
            logger.warning(
                "delivery ledger: {} entr(ies) name a collection another row also claims — those fall "
                "back to matching by title",
                len(rows) - len(keys),
            )
        return keys

    def _previous_picks(self, session: Session) -> dict[tuple[str, str, str], list[Pick]]:
        """Each row+library's picks from the run that last built it, keyed (user_slug, row_slug, section_key).

        Carried into the engine so a row is REUSED unchanged on non-refresh nights instead of being
        re-curated (and re-written to Plex) from scratch every night — the fix for the nightly full-row
        churn. We take the picks from the MAX run_id per (user, row, library), i.e. the last time we
        delivered that exact row+library, which is the best proxy for what's on Plex now. Legacy rows
        with no row/library stamp (blank collection_slug/section_key) can't be mapped, so they're
        skipped and simply bootstrap by curating fresh.
        """
        latest = (
            session.query(
                PickRow.user_id.label("user_id"),
                PickRow.collection_slug.label("slug"),
                PickRow.section_key.label("section_key"),
                func.max(PickRow.run_id).label("mrun"),
            )
            .filter(PickRow.collection_slug != "", PickRow.section_key != "")
            .group_by(PickRow.user_id, PickRow.collection_slug, PickRow.section_key)
            .subquery()
        )
        rows = (
            session.query(PickRow)
            .join(
                latest,
                and_(
                    PickRow.user_id == latest.c.user_id,
                    PickRow.collection_slug == latest.c.slug,
                    PickRow.section_key == latest.c.section_key,
                    PickRow.run_id == latest.c.mrun,
                ),
            )
            .order_by(PickRow.rank)
            .all()
        )
        slug_by_id = {u.id: u.slug for u in session.query(User).all()}
        out: dict[tuple[str, str, str], list[Pick]] = {}
        for r in rows:
            slug = slug_by_id.get(r.user_id)
            if slug is None:
                continue
            out.setdefault((slug, r.collection_slug, r.section_key), []).append(
                Pick(
                    tmdb_id=r.tmdb_id,
                    rating_key=0,  # remapped to THIS library's ratingKey at delivery, via section_index
                    title=r.title,
                    rank=r.rank,
                    reason=r.reason,
                    media_type=MediaType(r.media_type),
                    # Carried, or provenance would survive exactly one night: on a non-refresh night
                    # the pick comes back through here, and rebuilding it without these would blank
                    # the UI's "suggested by …" line and re-persist it as "not recorded".
                    sources=[part for part in (r.sources or "").split(",") if part],
                    affinity=r.affinity,
                    seed_tmdb_id=r.seed_tmdb_id,
                    seed_title=r.seed_title,
                    # Same reason as the provenance above: a row ordered by rating or year re-sorts
                    # its carried-forward picks every run, so these have to survive the round trip.
                    rating=r.rating or 0.0,
                    year=r.year,
                    # The settings fingerprint this pick was built under. Carried so the engine can
                    # tell "the owner changed the recipe" from "nothing changed" and rebuild rather
                    # than wait out the refresh cadence.
                    recipe=r.recipe or "",
                    collection_slug=r.collection_slug,
                    section_key=r.section_key,
                    library=r.library,
                )
            )
        return out

    def _previous_recipes(self, previous: dict[tuple[str, str, str], list[Pick]]) -> dict[tuple[str, str, str], str]:
        """The recipe each stored row was built under, keyed like ``_previous_picks``.

        Read from the picks themselves rather than queried again — they are the same rows. A row
        whose picks disagree (a half-written run, or a mix of recipes after an upgrade) is reported
        as its FIRST pick's recipe, which is the one the row's leading titles were chosen under.
        """
        return {key: picks[0].recipe for key, picks in previous.items() if picks and picks[0].recipe}

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None) -> list[UserProfile]:
        """Enabled users, optionally narrowed to user_ids — never widened past enabled=True.

        The Danger Zone's "pause all" switch stops every run without disabling anyone, so the
        user list survives a pause/unpause round trip.
        """
        store = SettingsStore(session, self._secrets)
        if store.get("paused_all"):
            logger.info("all runs are paused (Settings → Danger Zone) — no users will be processed")
            return []
        query = session.query(User).filter_by(enabled=True)
        if user_ids is not None:
            if not user_ids:
                return []
            query = query.filter(User.id.in_(user_ids))
        overrides = self._row_overrides(session)
        profiles = []
        for user in query.all():
            # A parental PROFILE, not the `restricted` flag: plex.tv sets that for every Plex Home
            # account. Keying on it dropped ordinary managed users from every run — while the Users
            # page now lets you enable them and the docs promise they get a row. An account with a
            # profile still gets none: Plex usually hides collections from it, so a row is invisible.
            #
            # `restriction_profile` is "" until the next user sync backfills it, so immediately after
            # an upgrade a profiled account is briefly eligible. Harmless — Plex refuses its filter
            # either way, and the next sync settles it.
            #
            # BOTH flags, matching `privacy.py`'s skip exactly. They come from different endpoints
            # and nothing enforces a relationship between them, so a `restricted=False` account that
            # somehow reports a profile is a real cell — and privacy.py deliberately keeps writing
            # its excludes. Keying on the profile alone here denied that same account a row, so it
            # held excludes for rows it was never given: two modules disagreeing about whether one
            # person can see anything.
            if user.restricted and user.restriction_profile:
                continue
            prefs = user.prefs or {}
            if prefs.get("paused"):
                continue
            # The tag the owner typed on this person, if any. The AUTOMATIC alternative — their slug,
            # under `requests.auto_user_tag` — is applied in the engine, not here: it is overridable
            # per row, so it cannot be baked into one value that every row then shares.
            request_tag = (user.request_tag or "").strip()
            profiles.append(
                UserProfile(
                    username=user.username,
                    plex_account_id=user.plex_account_id,
                    user_type=UserType(user.user_type),
                    slug=user.slug,
                    nickname=user.nickname or user.friendly_name,
                    excluded_genres=set(prefs.get("excluded_genres") or []),
                    # Through the reader, not straight off prefs: the list holds bare ints on an
                    # older install and records on a newer one, and the engine only wants ids.
                    blocked_seeds=blocked_ids(prefs),
                    row_name_template=prefs.get("row_name_tpl"),
                    request_tag=request_tag,
                    row_overrides=overrides.get(user.id, {}),
                )
            )
        return profiles

    @staticmethod
    def _row_overrides(session: Session) -> dict[int, dict[str, RowOverride]]:
        """user id -> {collection slug -> RowOverride}, from the collection_user_overrides table."""
        slug_by_id = {c.id: c.slug for c in session.query(Collection).all()}
        out: dict[int, dict[str, RowOverride]] = {}
        for row in session.query(CollectionUserOverride).all():
            slug = slug_by_id.get(row.collection_id)
            if slug is None:
                continue
            out.setdefault(row.user_id, {})[slug] = RowOverride(
                muted=row.muted, size=row.row_size, recent_count=row.recent_count
            )
        return out

    @staticmethod
    def _audience_maps(session: Session) -> tuple[dict[int, int], dict[int, set[int]]]:
        """(user_id → plex_account_id, collection_id → {user_id}) — the two lookups both the build and
        retire passes need to resolve a 'subset' row's audience to the plex account ids the engine matches on."""
        account_by_user = {u.id: u.plex_account_id for u in session.query(User).all()}
        audience_by_collection: dict[int, set[int]] = {}
        for row in session.query(CollectionAudience).all():
            audience_by_collection.setdefault(row.collection_id, set()).add(row.user_id)
        return account_by_user, audience_by_collection

    @staticmethod
    def _subset_audience(collection, account_by_user: dict, audience_by_collection: dict) -> set[int] | None:
        """The plex account ids a 'subset' row is limited to; None for any other audience (= everyone)."""
        if collection.audience != "subset":
            return None
        return {
            account_by_user[uid] for uid in audience_by_collection.get(collection.id, set()) if uid in account_by_user
        }

    @staticmethod
    def _build_only_slugs(session: Session, collection_ids: list[int] | None) -> frozenset[str] | None:
        """The row slugs a scoped (per-row scheduled) run rebuilds; None = a full run builds every row.
        Intersected with ``enabled=True`` so a stale schedule for a since-disabled row rebuilds nothing."""
        if collection_ids is None:
            return None
        rows = session.query(Collection).filter(Collection.id.in_(collection_ids), Collection.enabled).all()
        return frozenset(row.slug for row in rows)

    def _engine_config(
        self,
        session: Session,
        store: SettingsStore,
        *,
        dry_run: bool = False,
        collection_ids: list[int] | None = None,
    ) -> EngineConfig:
        """Every stored setting, as the engine's config dataclass.

        Its own method rather than inline in ``build`` so the settings -> engine seam can be asserted
        without a live Plex server. Every line here is "read one setting, hand it to the engine", and
        a field forgotten here is invisible: the setting saves, the UI shows it, and the engine simply
        never sees it.
        """
        return EngineConfig(
            row_size=int(store.get("row.size")),
            row_name_template=store.get("row.name_template"),
            # Fallback matches the seeded default and the UI's, so a never-saved setting behaves
            # the same everywhere (gather_candidates still floors an explicit [] at tmdb_similar).
            candidate_sources=list(store.get("candidates.sources") or ["tmdb_similar", "tmdb_discover"]),
            blocked_shared_seeds={
                tid for tid in (store.get("recommendations.blocked_shared_seeds") or []) if isinstance(tid, int)
            },
            web_search_provider=store.get("llm_web.search_provider") or "native",
            hub_anchors=self._build_hub_anchors(store),
            manage_shelf_order=bool(store.get("rows.manage_shelf_order")),
            # The `or` fallbacks below are safe only because the validators exclude the falsy
            # value: `min_history` is bounded 1-100, `recent_count` 1-25, `max_seeds` 5-100
            # (api/settings.py). Where 0 IS a legal value — the two fractions, and `refresh_days`,
            # where it means "frozen, never rebuilt" — the fallback is that same 0, so the owner's
            # zero survives. Correct by coincidence of the bounds rather than by construction —
            # lower any of those floors to 0 and several settings start silently reading as their
            # default instead of as the zero the owner chose.
            watched_pct=float(store.get("recommendations.watched_pct") or 0.0),
            refresh_days=int(store.get("recommendations.refresh_days") or 0),
            recency=float(store.get("recommendations.recency") or 0.0),
            recent_count=int(store.get("recommendations.recent_count") or 10),
            max_seeds=int(store.get("recommendations.max_seeds") or 30),
            rating_source=store.get("recommendations.rating_source") or "tmdb",
            min_history=int(store.get("recommendations.min_history") or 10),
            cold_start=store.get("recommendations.cold_start") or "popular",
            # The switch collapses into the threshold rather than travelling beside it: the
            # engine's one question is "at or below what?", and None answers "never". Two fields
            # would let the config express "off, but with a threshold of 2", which is not a state
            # and would only ever be a bug waiting for someone to read the wrong one.
            dislike_threshold=(_dislike_threshold(store) if store.get("recommendations.use_plex_ratings") else None),
            hide_shared_from_disabled=bool(store.get("privacy.hide_shared_from_disabled")),
            dry_run=dry_run,
            rows=self._build_rows(session, store),
            # The server owns the row list: an empty one means every row is DISABLED, not
            # 'unconfigured' — so nothing new is delivered, rather than the legacy default row
            # being resurrected behind a Rows page that shows it switched off.
            rows_defined=True,
            # ...and a row switched off has its already-built collection removed from its owner's
            # Home on this run, so "off" means gone, not merely "not refreshed". Runs stay full
            # here even when scoped: retiring a DISABLED row on any run is always correct.
            retired_rows=self._retired_rows(session, store),
            # A per-row scheduled run rebuilds ONLY these rows (by slug); None = every row. Scopes
            # delivery only — classification/sync/sweep/promotion above still see the full list.
            build_only=self._build_only_slugs(session, collection_ids),
            requests=self._build_requests(store),
        )

    def _build_rows(self, session: Session, store: SettingsStore) -> list[RowSpec]:
        """Build the engine's row specs from the enabled collections.

        The default 'picked' row keeps an empty name_template here, so the per-user row-name on the
        profile still applies to it; other rows carry their own name. A subset audience is resolved
        from user ids to plex account ids (what the engine matches on).

        Always ALL enabled rows — never scoped. A per-row scheduled run limits which rows actually
        rebuild via ``EngineConfig.build_only``, not by hiding rows from this list, so privacy
        classification, the share-filter sync, the sweep, and promotion all still see every row.
        """
        account_by_user, audience_by_collection = self._audience_maps(session)

        specs: list[RowSpec] = []
        collections = (
            session.query(Collection).filter_by(enabled=True).order_by(Collection.sort_order, Collection.id).all()
        )
        for collection in collections:
            shared = collection.build == "shared"
            audience = self._subset_audience(collection, account_by_user, audience_by_collection)
            is_default = collection.slug == DEFAULT_SLUG
            specs.append(
                RowSpec(
                    slug=collection.slug,
                    # The default row's name and size follow the global Settings > Defaults values
                    # (row.name_template / row.size) — that's what the wizard and Settings edit — so
                    # they stay in sync; other rows use their own.
                    name_template="" if is_default else (collection.name_template or collection.name),
                    size=int(store.get("row.size")) if is_default else collection.size,
                    media=collection.media,
                    shared=shared,
                    audience=audience,
                    min_watchers=collection.min_watchers,
                    request_tag=(collection.request_tag or "").strip(),
                    auto_user_tag=collection.req_auto_user_tag,  # None -> inherit the global switch
                    candidate_sources=list(collection.candidate_sources or []),
                    watched_pct=collection.watched_pct,  # None -> inherit the global watched cap
                    rewatch=bool(collection.rewatch),
                    unstarted_only=bool(collection.unstarted_only),
                    refresh_days=collection.refresh_days,  # None -> inherit the global cadence
                    recency=collection.recency,  # None -> inherit the global recency
                    recent_count=collection.recent_count,  # None -> inherit the global recent_count
                    max_seeds=collection.max_seeds,  # None -> inherit the global recommendations.max_seeds
                    cold_start=collection.cold_start,  # None -> inherit the global recommendations.cold_start
                    # "" means this row has no name for someone who cannot be named, and is therefore
                    # not built for them — the engine never invents one (issue #84).
                    fallback_name=collection.fallback_name or "",
                    seed_window=int(collection.seed_window or 1),  # 1 -> always their most recent watch
                    pick_order=collection.pick_order or "best",
                    placement=collection.placement or "both",
                    placement_friends=collection.placement_friends or "both",
                    pin_top=bool(collection.pin_top),
                    hub_anchors=self._row_hub_anchors(collection),
                    library_keys=[str(k) for k in (collection.library_keys or [])],
                    poster=self._build_poster(session, collection),
                    request_overrides=row_request_overrides(collection),
                )
            )
        return specs

    @staticmethod
    def _build_poster(session: Session, collection) -> PosterSpec | None:
        """This row's custom-poster spec, or None to leave Plex's own artwork alone.

        Upload mode carries the stored image bytes so the engine (which must not touch the DB or
        filesystem) can hand them straight to ``uploadPoster``; a configured-but-not-yet-uploaded row
        yields None. Generate mode carries only the text/style — the injected artist renders it.
        """
        cfg = collection.poster or {}
        mode = (cfg.get("mode") or "").strip()
        if mode == "upload":
            stored = load_upload(session, collection.id)
            return PosterSpec(mode="upload", image=stored[0]) if stored else None
        # "text" (built-in Pillow) and "ai" (image provider) both render from title/subtitle/style;
        # "generate" is the pre-rename name for "ai". apply_poster maps the mode to a render engine.
        # (Bug 2026-07-21: only "generate" was handled here, so the renamed "text"/"ai" modes silently
        # yielded None and no poster was ever applied.)
        if mode in ("text", "ai", "generate"):
            return PosterSpec(
                mode=mode,
                title=cfg.get("title") or "",
                subtitle=cfg.get("subtitle") or "",
                style=cfg.get("style") or "",
            )
        return None

    @classmethod
    def _row_hub_anchors(cls, collection) -> dict[str, HubAnchor]:
        """This row's per-library Recommended-shelf overrides (`collection.hub_anchor`). A library not
        overridden here falls back to the global default (legacy `pin_top` still pins in promote)."""
        return cls._parse_hub_anchors(collection.hub_anchor or {})

    def _retired_rows(self, session: Session, store: SettingsStore) -> list[RowSpec]:
        """Per-person rows that are DISABLED — their collections must be removed from Plex.

        Only enough of each spec to find and delete the collection (its rendered title, media and
        libraries); the recipe/size/sources are irrelevant to removal. A row DELETED from the DB
        can't be rebuilt here, so this covers disabling; a mute already covers per-user removal.

        STATIC-TITLED ROWS ONLY. Per-person rows share one label and are told apart solely by title,
        and a ``{top_seed}`` template with no picks renders to the DEFAULT row's title — so retiring
        such a row would match and DELETE the user's live default row. Those are skipped (left for a
        full rebuild), exactly as the mute path leaves them.
        """
        account_by_user, audience_by_collection = self._audience_maps(session)

        global_name = store.get("row.name_template") or ""
        # A stub whose only job is to let render_row_name resolve {user}; a non-empty username keeps a
        # "{user}" template from collapsing to empty.
        probe = UserProfile(username="_probe_", plex_account_id=0, user_type=UserType.SHARED)
        retired: list[RowSpec] = []
        disabled = session.query(Collection).filter_by(enabled=False, build="per_person").all()
        for collection in disabled:
            is_default = collection.slug == DEFAULT_SLUG
            # The template this row's title actually renders from — the global one for the default
            # row, its own for a custom row. Skip any that RENDERS to the default title with no picks:
            # per-person rows share one label and are told apart by title, so removing such a row would
            # match and DELETE the user's live default row. That's {top_seed} (no seed) AND anything
            # blank/whitespace — so test the rendered result, not a substring, or a "   " template slips
            # through and re-opens the collision.
            effective_template = global_name if is_default else (collection.name_template or collection.name)
            if not render_row_name(effective_template, probe, [], fallback_name=collection.fallback_name or ""):
                logger.debug("retired row '{}' would render to the default title — left for a rebuild", collection.slug)
                continue
            audience = self._subset_audience(collection, account_by_user, audience_by_collection)
            retired.append(
                RowSpec(
                    # The gate above renders WITH the fallback, so the spec must carry it too — or
                    # removal renders a different title from the one that decided this row was safe
                    # to retire.
                    fallback_name=collection.fallback_name or "",
                    slug=collection.slug,
                    name_template="" if is_default else (collection.name_template or collection.name),
                    size=collection.size,
                    media=collection.media,
                    shared=False,
                    audience=audience,
                    library_keys=[str(k) for k in (collection.library_keys or [])],
                )
            )
        return retired

    @staticmethod
    def _parse_hub_anchors(raw: object) -> dict[str, HubAnchor]:
        """`{sectionKey: {"top": true} | {"row": slug, "before": bool} | {"anchor": title, "before": bool}}`
        -> section key -> HubAnchor.

        A `top` entry moves the row to the very top; a `row` entry places it relative to another
        Shortlist ROW (by slug — a per-person row is one collection per person, so no single title
        names it); otherwise a non-empty `anchor` places it relative to that foreign collection.
        `row` is read before `anchor` so a saved foreign title left behind by an earlier setting can
        never override the row the owner actually chose. Blank/invalid entries are dropped, so the
        engine only moves real placements."""
        anchors: dict[str, HubAnchor] = {}
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("top"):
                    anchors[str(key)] = HubAnchor(to_top=True)
                elif str(entry.get("row") or "").strip():
                    anchors[str(key)] = HubAnchor(
                        anchor_row=str(entry["row"]).strip(),
                        before=bool(entry.get("before", False)),
                    )
                elif str(entry.get("anchor") or "").strip():
                    anchors[str(key)] = HubAnchor(
                        anchor_title=str(entry["anchor"]).strip(),
                        before=bool(entry.get("before", False)),
                    )
        return anchors

    @classmethod
    def _build_hub_anchors(cls, store: SettingsStore) -> dict[str, HubAnchor]:
        """The GLOBAL per-library Recommended-shelf default from `rows.hub_anchor`.

        A ROW anchor is dropped here, not honoured. The global default applies to every row, so "all
        rows go after row X" includes X itself; and the paths that use this default pass no
        `anchor_keys`, so a row anchor would reach the client's FOREIGN branch with an empty title and
        match any hub whose title is empty. `_hub_anchors` in the settings API rejects it on the way
        in — this is the second guard, so a future relaxation there cannot open that door silently.
        """
        anchors = cls._parse_hub_anchors(store.get("rows.hub_anchor") or {})
        dropped = [key for key, anchor in anchors.items() if anchor.anchor_row]
        for key in dropped:
            logger.warning("rows.hub_anchor[{}] names a row — only a per-ROW placement can do that; ignoring it", key)
            del anchors[key]
        return anchors

    @staticmethod
    def _build_requests(store: SettingsStore) -> RequestConfig | None:
        """Build the Sonarr/Radarr request config, or None when the feature is off.

        A target (Radarr for movies, Sonarr for shows) is only built when BOTH its URL and its API
        key are set; a half-configured app is left as None so that media type is simply skipped
        rather than erroring mid-run.
        """
        if not store.get("requests.enabled"):
            return None

        incomplete: list[str] = []

        def target(prefix: str) -> ArrTarget | None:
            url = (store.get(f"{prefix}.url") or "").strip()
            api_key = store.get(f"{prefix}.apikey") or ""
            if not url or not api_key:
                return None
            quality_profile_id = int(store.get(f"{prefix}.quality_profile_id") or 0)
            root_folder = (store.get(f"{prefix}.root_folder") or "").strip()
            if not quality_profile_id or not root_folder:
                app = prefix.split(".")[-1].title()  # "requests.radarr" -> "Radarr"
                missing = []
                if not quality_profile_id:
                    missing.append("quality profile")
                if not root_folder:
                    missing.append("root folder")
                msg = f"{app} connected but {' and '.join(missing)} not selected"
                logger.warning("{} — requests for that media type will be skipped", msg)
                incomplete.append(msg)
                return None
            return ArrTarget(
                url=url,
                api_key=api_key,
                quality_profile_id=quality_profile_id,
                root_folder=root_folder,
                tag=(store.get("requests.tag") or "").strip(),
            )

        return RequestConfig(
            enabled=True,
            radarr=target("requests.radarr"),
            sonarr=target("requests.sonarr"),
            incomplete_targets=incomplete,
            rating_source=store.get("requests.rating_source") or "tmdb",
            mdblist_api_key=store.get("requests.mdblist.apikey") or "",
            min_rating=float(store.get("requests.min_rating")),
            min_votes=int(store.get("requests.min_votes")),
            min_demand=int(store.get("requests.min_demand")),
            min_year=int(store.get("requests.min_year")),
            max_year=int(store.get("requests.max_year")),
            max_per_run=int(store.get("requests.max_per_run")),
            auto_send=bool(store.get("requests.auto_send")),
            auto_min_demand=int(store.get("requests.auto_min_demand")),
            auto_min_rating=float(store.get("requests.auto_min_rating")),
            auto_user_tag=bool(store.get("requests.auto_user_tag")),
            sonarr_monitor=store.get("requests.sonarr.monitor") or "all",
            language_mode=store.get("requests.language_mode") or "any",
            preferred_languages=normalise_languages(store.get("requests.preferred_languages")),
            # Read WITHOUT `or`: None means "follow min_rating + the gap" and 0.0 is a real bar, so
            # `x or default` would turn an owner's deliberate 0.0 into the derived 8.5.
            min_rating_other=_optional_float(store.get("requests.min_rating_other")),
        )
