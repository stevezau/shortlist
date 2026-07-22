"""Assemble an EngineContext (and the user profiles a run processes) from DB settings.

This is the server's translation layer: DB rows and typed settings in, engine dataclasses and
clients out. It holds no run state and writes no run rows — that is the run service's job. Kept
separate so the run service is only about orchestration (gate, execute, persist).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.clients.mdblist import MdbListClient
from shortlist.engine.clients.plex_db import PlexDbReader
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.search import ExaClient
from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.curator import make_curator
from shortlist.engine.delivery import DEFAULT_ROW_NAME, render_row_name
from shortlist.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource, distinct_recent
from shortlist.engine.models import (
    ArrTarget,
    EngineConfig,
    HubAnchor,
    MediaType,
    Pick,
    PosterSpec,
    PromptConfig,
    RequestConfig,
    RowOverride,
    RowSpec,
    UserProfile,
    UserType,
    overlay_prompt,
)
from shortlist.engine.pipeline import EngineContext
from shortlist.server.db.adapters import DbCache, DbSnapshotStore
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    PickRow,
    RequestCandidate,
    User,
    iso_utc,
    utcnow,
)
from shortlist.server.services.poster_service import load_upload, make_studio
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_history import StoreHistorySource
from shortlist.server.settings_store import SettingsStore


def _prompt_from_recipe(recipe: dict) -> PromptConfig:
    """A stored recipe dict → PromptConfig, blank fields preserved. Empty means "inherit": the engine
    overlays this on the layer below field by field, so defaulting any field here (e.g. tone →
    "balanced") would let an empty recipe silently beat the global/row recipe it should inherit."""
    return PromptConfig(
        tone=(recipe.get("tone") or "").strip(),
        guidance=(recipe.get("guidance") or "").strip(),
        template=(recipe.get("template") or "").strip(),
    )


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


def _pms_account_resolver(plex: PlexClient, session: Session) -> Callable[[UserProfile], int]:
    """UserProfile -> the id PMS knows them by.

    `metadata_item_settings.account_id` is the PMS-LOCAL account space, the same one
    `history?accountID=` uses — and the owner is not in it under their plex.tv id (their local row
    is id=1). Resolving that is exactly what `PlexHistorySource` already does; without it the owner
    reads zero watched flags and nothing says why.
    """
    roster = frozenset(row[0] for row in session.query(User.plex_account_id).all())

    def resolve(user: UserProfile) -> int:
        if user.user_type is not UserType.OWNER:
            return user.plex_account_id
        return plex.system_account_id(user.plex_account_id, user.username, exclude_ids=roster - {user.plex_account_id})

    return resolve


# Where the docs tell people to mount Plex's database. Bind-mounting a database into a container is
# not something anyone does by accident, so the MOUNT is the deliberate opt-in — making the owner
# then type the path they just mounted is a second hoop that buys no extra consent. Nothing is
# auto-discovered: if this exact path isn't mounted, the feature stays off.
DEFAULT_PLEX_DB_MOUNT = Path("/plexdb")


def _flag_reader(store: SettingsStore) -> PlexDbReader | None:
    """The PMS-database watched-flag reader, or None when it isn't set up.

    An explicit `plex.db_path` always wins, so an unusual layout stays possible.
    """
    path = (store.get("plex.db_path") or "").strip()
    if path:
        return PlexDbReader(path)
    default = DEFAULT_PLEX_DB_MOUNT / PlexDbReader.FILENAME
    if default.is_file():
        logger.info("watched flags: using the Plex database mounted at {}", DEFAULT_PLEX_DB_MOUNT)
        return PlexDbReader(DEFAULT_PLEX_DB_MOUNT)
    return None


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
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
            trakt = (
                TraktClient(store.get("trakt.client_id"), cache=DbCache(self._sessions, kind="trakt"))
                if store.get("trakt.client_id")
                else None
            )
            # External web-search backend for the llm_web source; None when no Exa key is set (the
            # native provider tools still work without it — only Ollama depends on it).
            exa_key = store.get("exa.apikey")
            search = ExaClient(exa_key) if exa_key else None
            history = self._history_source(store, plex, session)
            provider = store.get("curator.provider")
            curator = make_curator(provider, **curator_kwargs(store.get))
            # Build the poster studio only if a row actually renders a poster from text (built-in or
            # AI) — a server that never uses posters never touches Pillow or the image SDK. The studio
            # always provides the text engine; its AI engine is None when the provider can't make images.
            render_modes = {"text", "ai", "generate"}
            wants_studio = any((c.poster or {}).get("mode") in render_modes for c in session.query(Collection).all())
            poster_artist = make_studio(store, self._sessions) if wants_studio else None
            config = EngineConfig(
                row_size=int(store.get("row.size")),
                row_name_template=store.get("row.name_template"),
                # Fallback matches the seeded default and the UI's, so a never-saved setting behaves
                # the same everywhere (gather_candidates still floors an explicit [] at tmdb_similar).
                candidate_sources=list(store.get("candidates.sources") or ["tmdb_similar", "tmdb_discover"]),
                web_search_provider=store.get("llm_web.search_provider") or "auto",
                hub_anchors=self._build_hub_anchors(store),
                manage_shelf_order=bool(store.get("rows.manage_shelf_order")),
                watched_pct=float(store.get("recommendations.watched_pct") or 0.0),
                freshness=float(store.get("recommendations.freshness") or 0.0),
                recent_count=int(store.get("recommendations.recent_count") or 10),
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
            previous = self._previous_picks(session)
            # Opted-out accounts: with hide_shared_from_disabled, even public shared rows are hidden
            # from them, so disabling a user removes them from Shortlist entirely.
            disabled_account_ids = {u.plex_account_id for u in session.query(User).filter_by(enabled=False).all()}
            concurrency = int(store.get("run.concurrency") or 1)
            # Every user Shortlist knows, enabled or not: the engine answers "whose row is this?"
            # by account id, because a name can change and two names can slugify alike.
            known_slugs = {u.plex_account_id: u.slug for u in session.query(User).all()}

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
            # The engine reads the COMPLETE watch history from a local store, synced incrementally from
            # `history` (Plex/Tautulli) — Plex's API only returns the most recent ~200 plays, which hid
            # a heavy watcher's older watches from the already-watched filter. StoreHistorySource.fetch
            # syncs-then-reads, so it drops into the existing history_source slot unchanged.
            history_source=StoreHistorySource(
                self._sessions,
                history,
                min_completion=config.min_completion,
                # Optional second source: watched FLAGS from the PMS database, which is the only
                # place a mark-as-watched is visible. Off unless the owner sets `plex.db_path`.
                flags=_flag_reader(store),
                flag_account_id=_pms_account_resolver(plex, session),
            ),
            curator=curator,
            snapshots=DbSnapshotStore(self._sessions),
            index_cache=DbCache(self._sessions, kind="library_index"),
            web_search_cache=DbCache(self._sessions, kind="websearch"),
            mdblist=self._build_mdblist(store),
            concurrency=concurrency,
            previous_picks=previous,
            disabled_account_ids=disabled_account_ids,
            known_slugs=known_slugs,
            handled_requests=self._handled_requests(session),
            progress=progress,
        )

    def _build_mdblist(self, store: SettingsStore) -> MdbListClient | None:
        """A cache-backed MDBList client when the chosen rating source needs it (any non-TMDB source
        with a key set), else None. Shares the persistent DB cache so ratings are looked up at most
        once per title per week — the whole point of caching against MDBList's daily request cap."""
        if (store.get("requests.rating_source") or "tmdb") == "tmdb":
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

    @staticmethod
    def _history_source(store: SettingsStore, plex: PlexClient, session: Session):
        """The watch-history source: Tautulli-with-Plex-fallback when Tautulli is set, else Plex.

        The roster's account ids go to the Plex source so it can resolve the OWNER's local PMS
        account without ever landing on somebody else's — see PlexClient.system_account_id.
        """
        roster = frozenset(row[0] for row in session.query(User.plex_account_id).all())
        if store.get("tautulli.url"):
            return FallbackHistorySource(
                TautulliSource(TautulliClient(store.get("tautulli.url"), store.get("tautulli.apikey"))),
                PlexHistorySource(plex, roster_account_ids=roster),
            )
        return PlexHistorySource(plex, roster_account_ids=roster)

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
            history = self._history_source(store, PlexClient(plex_url, plex_token), session)
        # A lower completion bar than a run uses: this is "what they've been watching", not seeds.
        # Distinct titles, newest first: a show's episodes collapse to the one show (keeping its most
        # recent episode's detail), so a binge shows as one entry and the list reflects real variety —
        # looking back through the whole history to fill `limit` distinct titles.
        items = history.fetch(profile, min_completion=0.5)
        return [
            {
                "title": w.title,
                "media_type": w.media_type.value,
                "watched_at": w.watched_at.isoformat(),
                "year": w.year,
                "season": w.season,
                "episode": w.episode,
                "episode_title": w.episode_title,
            }
            for w in distinct_recent(items, limit)
        ]

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
                    collection_slug=r.collection_slug,
                    section_key=r.section_key,
                    library=r.library,
                )
            )
        return out

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
            prefs = user.prefs or {}
            if prefs.get("paused"):
                continue
            # Only an EXPLICIT per-user tag adds a per-person tag in Sonarr/Radarr. Automatic
            # username-tagging was removed (owner decision 2026-07-20): who wanted a title is already
            # shown in the Requests inbox why-line, so tagging every title with a username just
            # cluttered the Arr.
            request_tag = (user.request_tag or "").strip()
            profiles.append(
                UserProfile(
                    username=user.username,
                    plex_account_id=user.plex_account_id,
                    user_type=UserType(user.user_type),
                    slug=user.slug,
                    # The owner's own nickname wins; Tautulli's friendly name is only the default.
                    nickname=user.nickname or user.friendly_name,
                    excluded_genres=set(prefs.get("excluded_genres") or []),
                    row_name_template=prefs.get("row_name_tpl"),
                    prompt=self._resolve_prompt(store, prefs),
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
            recipe = row.prompt or {}
            prompt = None
            if recipe.get("tone") or recipe.get("guidance") or recipe.get("template"):
                prompt = _prompt_from_recipe(recipe)
            out.setdefault(row.user_id, {})[slug] = RowOverride(muted=row.muted, size=row.row_size, prompt=prompt)
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

    def _build_rows(self, session: Session, store: SettingsStore) -> list[RowSpec]:
        """Build the engine's row specs from the enabled collections.

        The default 'picked' row keeps an empty name_template and no recipe here, so the per-user
        row-name and Phase-A prompt on the profile still apply to it; other rows carry their own
        name and recipe. A subset audience is resolved from user ids to plex account ids (what the
        engine matches on).

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
            prompt: PromptConfig | None = None
            if not is_default:
                # A custom row's recipe is the GLOBAL one with this row's fields laid over it. Built
                # unconditionally before, an empty row recipe produced a bare `balanced` PromptConfig
                # that beat the global one downstream — so Settings -> Curation style applied to the
                # default row and NOTHING else, while its own copy claimed it wrote "everyone's rows".
                row_recipe = _prompt_from_recipe(collection.prompt or {})
                merged = overlay_prompt(self._resolve_prompt(store, {}), row_recipe)
                prompt = replace(merged, shared=shared)
            elif shared:
                # The default row's style comes from global Settings. A PER-PERSON one inherits that
                # via the user's own resolved prompt (prompt=None lets it through — rows.py), but a
                # SHARED row has no user profile to inherit from: leaving it None would curate it
                # with a bare default and silently ignore Settings -> Curation style. So pass the
                # global recipe explicitly.
                prompt = replace(self._resolve_prompt(store, {}), shared=True)
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
                    prompt=prompt,
                    min_watchers=collection.min_watchers,
                    request_tag=(collection.request_tag or "").strip(),
                    candidate_sources=list(collection.candidate_sources or []),
                    watched_pct=collection.watched_pct,  # None -> inherit the global watched cap
                    freshness=collection.freshness,  # None -> inherit the global freshness
                    recent_count=collection.recent_count,  # None -> inherit the global recent_count
                    placement=collection.placement or "both",
                    pin_top=bool(collection.pin_top),
                    hub_anchors=self._row_hub_anchors(collection),
                    library_keys=[str(k) for k in (collection.library_keys or [])],
                    poster=self._build_poster(session, collection),
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
            if render_row_name(effective_template, probe, []) == DEFAULT_ROW_NAME:
                logger.debug("retired row '{}' would render to the default title — left for a rebuild", collection.slug)
                continue
            audience = self._subset_audience(collection, account_by_user, audience_by_collection)
            retired.append(
                RowSpec(
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
        """`{sectionKey: {"top": true} | {"anchor": title, "before": bool}}` -> section key -> HubAnchor.
        A `top` entry moves the row to the very top; otherwise a non-empty `anchor` places it relative
        to that collection. Blank/invalid entries are dropped, so the engine only moves real placements."""
        anchors: dict[str, HubAnchor] = {}
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("top"):
                    anchors[str(key)] = HubAnchor(to_top=True)
                elif str(entry.get("anchor") or "").strip():
                    anchors[str(key)] = HubAnchor(
                        anchor_title=str(entry["anchor"]).strip(),
                        before=bool(entry.get("before", False)),
                    )
        return anchors

    @classmethod
    def _build_hub_anchors(cls, store: SettingsStore) -> dict[str, HubAnchor]:
        """The GLOBAL per-library Recommended-shelf default from `rows.hub_anchor`."""
        return cls._parse_hub_anchors(store.get("rows.hub_anchor") or {})

    @staticmethod
    def _build_requests(store: SettingsStore) -> RequestConfig | None:
        """Build the Sonarr/Radarr request config, or None when the feature is off.

        A target (Radarr for movies, Sonarr for shows) is only built when BOTH its URL and its API
        key are set; a half-configured app is left as None so that media type is simply skipped
        rather than erroring mid-run.
        """
        if not store.get("requests.enabled"):
            return None

        def target(prefix: str) -> ArrTarget | None:
            url = (store.get(f"{prefix}.url") or "").strip()
            api_key = store.get(f"{prefix}.apikey") or ""
            if not url or not api_key:
                return None
            return ArrTarget(
                url=url,
                api_key=api_key,
                quality_profile_id=int(store.get(f"{prefix}.quality_profile_id") or 0),
                root_folder=(store.get(f"{prefix}.root_folder") or "").strip(),
                tag=(store.get("requests.tag") or "").strip(),
            )

        return RequestConfig(
            enabled=True,
            radarr=target("requests.radarr"),
            sonarr=target("requests.sonarr"),
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
        )

    @staticmethod
    def _resolve_prompt(store: SettingsStore, prefs: dict) -> PromptConfig:
        """Merge the global curation recipe with this user's per-person overrides.

        tone/template: the user's value wins if set, else the global default. guidance is additive —
        the house guidance plus the per-person note. Empty string means "inherit" everywhere.
        """
        global_tone = store.get("curator.prompt_tone") or "balanced"
        global_guidance = (store.get("curator.prompt_guidance") or "").strip()
        global_template = (store.get("curator.prompt_template") or "").strip()
        user_tone = (prefs.get("prompt_tone") or "").strip()
        user_guidance = (prefs.get("prompt_guidance") or "").strip()
        user_template = (prefs.get("prompt_template") or "").strip()
        guidance = "\n".join(part for part in (global_guidance, user_guidance) if part)
        return PromptConfig(
            tone=user_tone or global_tone,
            guidance=guidance,
            template=user_template or global_template,
        )
