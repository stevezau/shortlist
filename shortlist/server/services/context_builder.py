"""Assemble an EngineContext (and the user profiles a run processes) from DB settings.

This is the server's translation layer: DB rows and typed settings in, engine dataclasses and
clients out. It holds no run state and writes no run rows — that is the run service's job. Kept
separate so the run service is only about orchestration (gate, execute, persist).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.curator import make_curator
from shortlist.engine.delivery import DEFAULT_ROW_NAME, render_row_name
from shortlist.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource
from shortlist.engine.models import (
    ArrTarget,
    EngineConfig,
    MediaType,
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
)
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SettingsStore


class ContextBuilder:
    """Builds an EngineContext and user profiles from DB settings — the engine's server adapter."""

    def __init__(self, session_factory: sessionmaker[Session], secrets, bus: EventBus):
        self._sessions = session_factory
        self._secrets = secrets
        self._bus = bus

    def build(self, *, dry_run: bool, loop: asyncio.AbstractEventLoop | None = None) -> EngineContext:
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            plex_url = store.get("plex.url")
            plex_token = store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet — finish setup first")
            plex = PlexClient(plex_url, plex_token)
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
            trakt = TraktClient(store.get("trakt.client_id")) if store.get("trakt.client_id") else None
            history = self._history_source(store, plex)
            provider = store.get("curator.provider")
            curator_kwargs = {}
            if provider == "ollama":
                # Ollama takes a base URL and no key — a key would be rejected by its ctor.
                curator_kwargs["base_url"] = store.get("curator.ollama_url")
            elif store.get("curator.api_key"):
                curator_kwargs["api_key"] = store.get("curator.api_key")
            if store.get("curator.model"):
                curator_kwargs["model"] = store.get("curator.model")
            curator = make_curator(provider, **curator_kwargs)
            config = EngineConfig(
                row_size=int(store.get("row.size")),
                row_name_template=store.get("row.name_template"),
                staleness_runs=int(store.get("staleness_runs")),
                # Fallback matches the seeded default and the UI's, so a never-saved setting behaves
                # the same everywhere (gather_candidates still floors an explicit [] at tmdb_similar).
                candidate_sources=list(store.get("candidates.sources") or ["tmdb_similar", "tmdb_discover"]),
                dry_run=dry_run,
                rows=self._build_rows(session, store),
                # The server owns the row list: an empty one means every row is DISABLED, not
                # 'unconfigured' — so nothing new is delivered, rather than the legacy default row
                # being resurrected behind a Rows page that shows it switched off.
                rows_defined=True,
                # ...and a row switched off has its already-built collection removed from its owner's
                # Home on this run, so "off" means gone, not merely "not refreshed".
                retired_rows=self._retired_rows(session, store),
                requests=self._build_requests(store),
            )
            recent = self._recent_picks(session, config)
            # Every user Shortlist knows, enabled or not: the engine answers "whose row is this?"
            # by account id, because a name can change and two names can slugify alike.
            known_slugs = {u.plex_account_id: u.slug for u in session.query(User).all()}

        def progress(slug: str, stage: str, counts: dict) -> None:
            if loop is not None:
                loop.call_soon_threadsafe(
                    self._bus.publish, "run.user.stage", {"user": slug, "stage": stage, "counts": counts}
                )

        return EngineContext(
            config=config,
            plex=plex,
            plextv=plextv,
            tmdb=tmdb,
            trakt=trakt,
            history_source=history,
            curator=curator,
            snapshots=DbSnapshotStore(self._sessions),
            recent_picks=recent,
            known_slugs=known_slugs,
            handled_requests=self._handled_requests(session),
            progress=progress,
        )

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
    def _history_source(store: SettingsStore, plex: PlexClient):
        """The watch-history source: Tautulli-with-Plex-fallback when Tautulli is set, else Plex."""
        if store.get("tautulli.url"):
            return FallbackHistorySource(
                TautulliSource(TautulliClient(store.get("tautulli.url"), store.get("tautulli.apikey"))),
                PlexHistorySource(plex),
            )
        return PlexHistorySource(plex)

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
            history = self._history_source(store, PlexClient(plex_url, plex_token))
        # A lower completion bar than a run uses: this is "what they've been watching", not seeds.
        items = history.fetch(profile, min_completion=0.5)
        items.sort(key=lambda w: w.watched_at, reverse=True)
        return [
            {
                "title": w.title,
                "media_type": w.media_type.value,
                "watched_at": w.watched_at.isoformat(),
                "year": w.year,
            }
            for w in items[:limit]
        ]

    def _recent_picks(self, session: Session, config: EngineConfig) -> dict[str, set[tuple[int, MediaType]]]:
        """Titles picked in the last N runs, keyed on (tmdb_id, media_type).

        The pair, not the bare id: TMDB ids are unique only within a namespace, so keying on the
        id alone lets a recently-picked film suppress the show that shares its number.
        """
        recent: dict[str, set[tuple[int, MediaType]]] = {}
        window = config.staleness_runs * config.row_size
        for user in session.query(User).filter_by(enabled=True).all():
            rows = (
                session.query(PickRow.tmdb_id, PickRow.media_type)
                .filter_by(user_id=user.id)
                .order_by(PickRow.id.desc())
                .limit(window)
                .all()
            )
            recent[user.slug] = {(r.tmdb_id, MediaType(r.media_type)) for r in rows}
        return recent

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
            profiles.append(
                UserProfile(
                    username=user.username,
                    plex_account_id=user.plex_account_id,
                    user_type=UserType(user.user_type),
                    slug=user.slug,
                    excluded_genres=set(prefs.get("excluded_genres") or []),
                    row_name_template=prefs.get("row_name_tpl"),
                    prompt=self._resolve_prompt(store, prefs),
                    request_tag=(user.request_tag or "").strip(),
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
                # Blank stays blank — the engine overlays this on the row's recipe field by field, so
                # an empty field means "inherit". Defaulting tone to "balanced" here meant setting
                # ONLY the tone for one person silently wiped that row's guidance and custom prompt.
                prompt = PromptConfig(
                    tone=(recipe.get("tone") or "").strip(),
                    guidance=(recipe.get("guidance") or "").strip(),
                    template=(recipe.get("template") or "").strip(),
                )
            out.setdefault(row.user_id, {})[slug] = RowOverride(muted=row.muted, size=row.row_size, prompt=prompt)
        return out

    def _build_rows(self, session: Session, store: SettingsStore) -> list[RowSpec]:
        """Build the engine's row specs from the enabled collections.

        The default 'picked' row keeps an empty name_template and no recipe here, so the per-user
        row-name and Phase-A prompt on the profile still apply to it; other rows carry their own
        name and recipe. A subset audience is resolved from user ids to plex account ids (what the
        engine matches on).
        """
        account_by_user = {u.id: u.plex_account_id for u in session.query(User).all()}
        audience_by_collection: dict[int, set[int]] = {}
        for row in session.query(CollectionAudience).all():
            audience_by_collection.setdefault(row.collection_id, set()).add(row.user_id)

        specs: list[RowSpec] = []
        collections = (
            session.query(Collection).filter_by(enabled=True).order_by(Collection.sort_order, Collection.id).all()
        )
        for collection in collections:
            shared = collection.build == "shared"
            audience: set[int] | None = None
            if collection.audience == "subset":
                audience = {
                    account_by_user[uid]
                    for uid in audience_by_collection.get(collection.id, set())
                    if uid in account_by_user
                }
            is_default = collection.slug == DEFAULT_SLUG
            prompt: PromptConfig | None = None
            if not is_default:
                # A custom row's recipe is the GLOBAL one with this row's fields laid over it. Built
                # unconditionally before, an empty row recipe produced a bare `balanced` PromptConfig
                # that beat the global one downstream — so Settings -> Curation style applied to the
                # default row and NOTHING else, while its own copy claimed it wrote "everyone's rows".
                recipe = collection.prompt or {}
                row_recipe = PromptConfig(
                    tone=(recipe.get("tone") or "").strip(),
                    guidance=(recipe.get("guidance") or "").strip(),
                    template=(recipe.get("template") or "").strip(),
                )
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
                    library_keys=[str(k) for k in (collection.library_keys or [])],
                )
            )
        return specs

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
        account_by_user = {u.id: u.plex_account_id for u in session.query(User).all()}
        audience_by_collection: dict[int, set[int]] = {}
        for row in session.query(CollectionAudience).all():
            audience_by_collection.setdefault(row.collection_id, set()).add(row.user_id)

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
            audience: set[int] | None = None
            if collection.audience == "subset":
                audience = {
                    account_by_user[uid]
                    for uid in audience_by_collection.get(collection.id, set())
                    if uid in account_by_user
                }
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
            omdb_api_key=store.get("requests.omdb.apikey") or "",
            min_rating=float(store.get("requests.min_rating")),
            min_votes=int(store.get("requests.min_votes")),
            min_demand=int(store.get("requests.min_demand")),
            min_year=int(store.get("requests.min_year")),
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
