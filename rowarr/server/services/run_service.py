"""Run service — the server's adapter over the engine.

Builds an EngineContext from DB settings, executes runs in a worker thread (the engine is
sync), persists runs/run_users/picks/events rows, and emits SSE progress. A `runs` row is
inserted BEFORE execution so a container restart can see and abort orphaned runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tautulli import TautulliClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import make_curator
from rowarr.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource
from rowarr.engine.models import (
    ArrTarget,
    EngineConfig,
    FilterSnapshot,
    MediaType,
    PromptConfig,
    RequestConfig,
    RowOverride,
    RowSpec,
    RunReport,
    UserProfile,
    UserType,
    slugify,
)
from rowarr.engine.pipeline import EngineContext
from rowarr.engine.pipeline import run as engine_run
from rowarr.server.db.models import CacheRow, Event, PickRow, RestrictionSnapshotRow, Run, RunUser, User
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore


def unique_slug(session: Session, username: str) -> str:
    """A slug no other user already holds. Slugs are UNIQUE in the DB and are what row labels are
    built from, so two Plex display names that slugify alike (a real possibility — Plex names are
    free text) must not collide: the second user would fail to save, and their share filter would
    then never be written.
    """
    base = slugify(username)
    slug = base
    n = 2
    while session.query(User).filter_by(slug=slug).first() is not None:
        slug = f"{base}_{n}"
        n += 1
    return slug


class DbSnapshotStore:
    """Engine SnapshotStore over the restriction_snapshots table."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=plex_account_id).one_or_none()
            if user is None:
                return None
            row = (
                session.query(RestrictionSnapshotRow)
                .filter_by(user_id=user.id, reason="initial")
                .order_by(RestrictionSnapshotRow.id)
                .first()
            )
            if row is None:
                return None
            return FilterSnapshot(
                plex_account_id=plex_account_id,
                username=user.username,
                taken_at=row.taken_at,
                filters=row.filters_before,
            )

    def save(self, snapshot: FilterSnapshot) -> None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=snapshot.plex_account_id).one_or_none()
            if user is None:
                # An account that shares the server but that Rowarr has never seen — someone the
                # owner invited to Plex since the last time the Users page was opened. We still
                # have to write their share filter (a row is visible to anyone whose filter does
                # not exclude it), and rule 2 says we cannot write it without a snapshot first.
                # So record them: disabled (we build no row for them) but restorable, because
                # uninstall reaches snapshots through this table.
                user = User(
                    plex_account_id=snapshot.plex_account_id,
                    username=snapshot.username,
                    slug=unique_slug(session, snapshot.username),
                    user_type=UserType.SHARED.value,
                    enabled=False,
                )
                session.add(user)
                session.flush()
                logger.info("{}: first seen during a run — recorded so their filters can be restored", user.username)
            session.add(
                RestrictionSnapshotRow(
                    user_id=user.id,
                    taken_at=snapshot.taken_at,
                    reason="initial",
                    filters_before=snapshot.filters,
                    filters_after={},
                )
            )
            session.commit()


class DbCache:
    """Engine TMDB cache over the caches table."""

    def __init__(self, session_factory: sessionmaker[Session], kind: str = "tmdb"):
        self._sessions = session_factory
        self._kind = kind

    def get(self, key: str) -> str | None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            if row and row.expires_at > time.time():
                return json.dumps(row.value)
            return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            payload = json.loads(value)
            if row is None:
                session.add(CacheRow(kind=self._kind, key=key, value=payload, expires_at=time.time() + ttl_s))
            else:
                row.value = payload
                row.expires_at = time.time() + ttl_s
            session.commit()


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd

    # -- context assembly ----------------------------------------------------------------

    def build_context(self, *, dry_run: bool, loop: asyncio.AbstractEventLoop | None = None) -> EngineContext:
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            plex_url = store.get("plex.url")
            plex_token = store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet — finish setup first")
            plex = PlexClient(plex_url, plex_token)
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
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
                dry_run=dry_run,
                rows=self._build_rows(session, store),
                requests=self._build_requests(store),
            )
            recent = self._recent_picks(session, config)
            # Every user Rowarr knows, enabled or not: the engine answers "whose row is this?"
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
            history_source=history,
            curator=curator,
            snapshots=DbSnapshotStore(self._sessions),
            recent_picks=recent,
            known_slugs=known_slugs,
            progress=progress,
        )

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
                    max_rating=prefs.get("max_rating"),
                    row_size=prefs.get("row_size"),
                    row_name_template=prefs.get("row_name_tpl"),
                    prompt=self._resolve_prompt(store, prefs),
                    row_overrides=overrides.get(user.id, {}),
                )
            )
        return profiles

    @staticmethod
    def _row_overrides(session: Session) -> dict[int, dict[str, RowOverride]]:
        """user id -> {collection slug -> RowOverride}, from the collection_user_overrides table."""
        from rowarr.server.db.models import Collection, CollectionUserOverride

        slug_by_id = {c.id: c.slug for c in session.query(Collection).all()}
        out: dict[int, dict[str, RowOverride]] = {}
        for row in session.query(CollectionUserOverride).all():
            slug = slug_by_id.get(row.collection_id)
            if slug is None:
                continue
            recipe = row.prompt or {}
            prompt = None
            if recipe.get("tone") or recipe.get("guidance") or recipe.get("template"):
                prompt = PromptConfig(
                    tone=recipe.get("tone", "balanced"),
                    guidance=recipe.get("guidance", ""),
                    template=recipe.get("template", ""),
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
        from rowarr.server.db.models import Collection, CollectionAudience

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
            prompt: PromptConfig | None = None
            if collection.slug != "picked":
                recipe = collection.prompt or {}
                prompt = PromptConfig(
                    tone=recipe.get("tone", "balanced"),
                    guidance=recipe.get("guidance", ""),
                    template=recipe.get("template", ""),
                    shared=shared,
                )
            is_default = collection.slug == "picked"
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
                )
            )
        return specs

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

    # -- execution -----------------------------------------------------------------------

    async def start_run(self, *, trigger: str, dry_run: bool, user_ids: list[int] | None = None) -> int:
        """Insert the runs row and launch execution as a background task; returns run id."""
        with self._sessions() as session:
            run = Run(trigger=trigger, dry_run=dry_run, status="queued", stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        task = asyncio.create_task(self._execute(run_id, dry_run, user_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    def _privacy_gate_error(self) -> str | None:
        """plex-safety rule 1, server-side: real writes need a fresh passing Privacy Check.

        Uses the same latest-per-tier definition as the dashboard's privacy badge, so the two
        can never disagree (a stale T2 failure must not be masked by a newer T1-only pass).
        """
        from rowarr.server.db.models import Server
        from rowarr.server.services.privacy_state import gate_error

        with self._sessions() as session:
            server = session.query(Server).first()
            return gate_error(session, server.version if server else None)

    def _remedy_only(self) -> RunReport:
        """Everything that makes the server MORE private, and nothing else.

        Running the engine with no users does exactly that: it sweeps rows Plex cannot hide, then
        merges the excludes for every row that exists into every account's share filter. Nothing
        is created, nothing is promoted — deletion and merge-only excludes cannot expose anything.

        This is what a closed gate must still allow, or the gate deadlocks itself: a missing
        exclude FAILS the Privacy Check, the failed check closes the gate, and a closed gate that
        blocked the sync would stop the only thing that writes the exclude. The check could never
        pass again. (Live server, SFLIX, 2026-07-13: T1 failed for 45 accounts, and the run that
        would have fixed them was refused because T1 failed.)
        """
        ctx = self.build_context(dry_run=False)
        return engine_run(ctx, [])

    async def _execute(self, run_id: int, dry_run: bool, user_ids: list[int] | None) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not dry_run and (gate_error := self._privacy_gate_error()):
                logger.warning("run {} refused by privacy gate: {}", run_id, gate_error)
                # ...but still remove any row Plex cannot hide.
                #
                # The gate exists to stop Rowarr CREATING rows it cannot prove are private. A row
                # of the wrong type for its library is already visible to every account on the
                # server, right now — removing it is the remedy, not a new risk, and it is the one
                # thing here that makes the server strictly more private.
                #
                # Gating it would be a trap: such a row FAILS the Privacy Check, a failed check
                # closes the gate, and the closed gate would then block the very sweep that removes
                # it — so the leak could never heal. That is precisely the state a live server was
                # left in (SFLIX, 2026-07-12).
                remedy_error = None
                try:
                    report = await loop.run_in_executor(None, self._remedy_only)
                    remedy_error = report.error  # the remedy can degrade without raising
                    # `status` is forced: nothing was BUILT, so the run is an error whatever the
                    # remedy did. Passing it here means the run is never momentarily recorded as a
                    # success — a restart in that window would have left a refused run saying "ok".
                    self._persist_report(run_id, report, status="error", error=f"privacy gate: {gate_error}")
                except Exception as e:
                    # A failing remedy must never leave the run stuck: the gate refusal is the
                    # headline, and this is the footnote.
                    remedy_error = f"{type(e).__name__}: {e}"
                    logger.exception("the remedy pass failed while the privacy gate was closed")
                    with self._sessions() as session:
                        run = session.get(Run, run_id)
                        run.status = "error"
                        run.finished_at = datetime.now(UTC)
                        run.stats = {"error": f"privacy gate: {gate_error}", "remedy_error": remedy_error}
                        session.commit()
                if remedy_error:
                    with self._sessions() as session:
                        run = session.get(Run, run_id)
                        run.stats = {**(run.stats or {}), "remedy_error": remedy_error}
                        session.commit()
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})
                return
            self._bus.publish("run.progress", {"run_id": run_id, "status": "running"})
            with self._sessions() as session:
                run = session.get(Run, run_id)
                run.status = "running"
                session.commit()
                profiles = self.enabled_profiles(session, user_ids)
            try:
                ctx = self.build_context(dry_run=dry_run, loop=loop)
                report = await loop.run_in_executor(None, engine_run, ctx, profiles)
                self._persist_report(run_id, report)
                status = "ok" if report.ok else "error"
            except Exception as e:
                logger.exception("run {} failed", run_id)
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.status = "error"
                    run.finished_at = datetime.now(UTC)
                    run.stats = {"error": f"{type(e).__name__}: {e}"}
                    session.commit()
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})
                return
            self._bus.publish("run.finished", {"run_id": run_id, "status": status})

    def _persist_report(self, run_id: int, report, *, status: str | None = None, error: str | None = None) -> None:
        """Persist a run's outcome. `status`/`error` override what the report says — the gated
        path uses them so a refused run is never even momentarily written as a success."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            users_by_slug = {u.slug: u for u in session.query(User).all()}
            ok = errors = 0
            for user_report in report.users:
                user = users_by_slug.get(user_report.slug)
                if user is None:
                    continue
                if user_report.status == "error":
                    errors += 1
                else:
                    ok += 1
                user.cold_start = user_report.status == "cold_start"
                session.add(
                    RunUser(
                        run_id=run_id,
                        user_id=user.id,
                        status=user_report.status,
                        error=user_report.error,
                        duration_ms=int(user_report.duration_s * 1000),
                        llm_tokens=user_report.llm_tokens,
                        diff=user_report.diff.__dict__ if user_report.diff else {},
                    )
                )
                if not report.dry_run:
                    for pick in user_report.picks:
                        session.add(
                            PickRow(
                                run_id=run_id,
                                user_id=user.id,
                                tmdb_id=pick.tmdb_id,
                                media_type=pick.media_type.value,
                                rating_key=pick.rating_key,
                                rank=pick.rank,
                                collection_slug=pick.collection_slug,
                                title=pick.title,
                                reason=pick.reason,
                                seed_tmdb_id=pick.seed_tmdb_id,
                                seed_title=pick.seed_title,
                            )
                        )
                session.add(
                    Event(
                        scope="run.user",
                        level="error" if user_report.status == "error" else "info",
                        message={
                            "run_id": run_id,
                            "user": user_report.slug,
                            "status": user_report.status,
                            "dry_run": report.dry_run,
                            "diff": user_report.diff.__dict__ if user_report.diff else {},
                            "privacy_synced": user_report.privacy_synced,
                            "error": user_report.error,
                        },
                    )
                )
            # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
            # can touch users who were not in this run at all (paused, disabled) — those have no
            # RunUser row to carry the audit, and deleting someone's row is the most destructive
            # thing a run does. It gets its own event (plex-safety rule 10).
            if report.swept_rows:
                session.add(
                    Event(
                        scope="run.sweep",
                        level="warning",
                        message={
                            "run_id": run_id,
                            "dry_run": report.dry_run,
                            "reason": "row was broken beyond repair-in-place — either no share "
                            "filter could hide it (wrong type for its library), or it shared a "
                            "collection tag with other users' rows and held their picks",
                            "deleted": report.swept_rows,
                        },
                    )
                )
            # Share-filter writes. Most of these accounts are NOT in this run's user list — they
            # are simply people the server is shared with — so they have no RunUser row to carry
            # the audit. Changing someone's Plex share permissions is the most sensitive thing
            # Rowarr does; "what changed on whose share at 03:31" has to be answerable for every
            # one of them (plex-safety rule 10).
            for account_id, write in report.filter_writes.items():
                session.add(
                    Event(
                        scope="run.privacy_sync",
                        level="info",
                        message={
                            "run_id": run_id,
                            "dry_run": report.dry_run,
                            "plex_account_id": account_id,
                            "username": write["username"],
                            "fields": {
                                field: {"before": before, "after": after}
                                for field, (before, after) in write["fields"].items()
                            },
                        },
                    )
                )
            # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
            # write (it consumes disk and bandwidth), so every request — and every skip — is audited
            # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
            if report.requests is not None and report.requests.outcomes:
                session.add(
                    Event(
                        scope="run.requests",
                        level="info",
                        message={
                            "run_id": run_id,
                            "dry_run": report.dry_run,
                            "considered": report.requests.considered,
                            "outcomes": [
                                {
                                    "tmdb_id": o.tmdb_id,
                                    "title": o.title,
                                    "media_type": o.media_type.value,
                                    "status": o.status,
                                    "detail": o.detail,
                                }
                                for o in report.requests.outcomes
                            ],
                        },
                    )
                )
            if report.error:
                session.add(Event(scope="run", level="error", message={"run_id": run_id, "error": report.error}))

            # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
            # refused to write) has no per-user error to count, and must never report success.
            run.status = status or ("ok" if report.ok else "error")
            run.finished_at = datetime.now(UTC)
            run.stats = {
                "users_ok": ok,
                "users_error": errors,
                "dry_run": report.dry_run,
                "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
                "shares_updated": len(report.filter_writes),
                "titles_requested": report.requests.requested if report.requests else 0,
                "error": error or report.error,
            }
            session.commit()
