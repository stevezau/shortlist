"""The run context, and the one progress emitter every phase narrates through.

Both halves of a run need these two — ``pipeline.py`` for the ordering, ``rows.py`` for row
construction — which is why they live in neither. Importing them from here is what stops those two
modules from importing each other.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from shortlist.engine.clients.mdblist import MdbListClient
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.poster import PosterArtist
from shortlist.engine.clients.search import WebSearchProvider
from shortlist.engine.clients.tmdb import Cache, NullCache, TmdbClient
from shortlist.engine.clients.trakt import TraktClient
from shortlist.engine.curator import Curator
from shortlist.engine.history import HistorySource
from shortlist.engine.models import EngineConfig, Pick, UserProfile, UserRunReport
from shortlist.engine.privacy import SnapshotStore


@dataclass
class EngineContext:
    """Everything one run needs; the server adapter builds this once."""

    config: EngineConfig
    plex: PlexClient
    plextv: PlexTvClient
    tmdb: TmdbClient
    history_source: HistorySource
    curator: Curator
    snapshots: SnapshotStore
    # Optional 'related titles' candidate source; None when no Trakt key is configured.
    trakt: TraktClient | None = None
    # Optional external web-search backend for the llm_web source (Exa); None when no key is
    # configured. Native provider web-search tools don't need it; a local Ollama model does.
    search: WebSearchProvider | None = None
    # Optional image-generation backend for generate-mode row posters, built from the AI curator's
    # provider/key. None when the curator provider can't make images (Anthropic, Ollama) or none is set.
    poster_artist: PosterArtist | None = None
    # (owner_slug, row_slug, section_key) -> last run's delivered picks for that row+library, newest
    # first. Carried forward so a row is REUSED unchanged on non-refresh nights (`refresh_days` is the
    # refresh CADENCE) instead of re-curated from scratch every night — the fix for the nightly
    # full-row churn that staleness_runs=3 used to force (SFLIX 2026-07-20). Empty -> every row
    # bootstraps by curating fresh, exactly like a first run.
    previous_picks: dict[tuple[str, str, str], list[Pick]] = field(default_factory=dict)
    # (user_slug, row_slug, section_key) -> the Plex ratingKey that row last delivered there, from the
    # delivery ledger. Delivery's ONE identity question is "is the collection in front of me this
    # row's, under a title it no longer renders to?" — a rename in place versus a fresh build. It used
    # to answer that by COUNTING ("if this user has one row, the one collection here must be it"),
    # which is a guess that was wrong in three separate ways (see jobs-and-runs-design.md §17).
    # A ratingKey answers it outright, and works for a multi-row user, which counting never could.
    # Empty for direct engine runs and for rows delivered before the ledger existed — the count-based
    # fallback still covers those.
    delivered_keys: dict[tuple[str, str, str], int] = field(default_factory=dict)
    # Build a PMS client that sees the server AS one user, or None when no token can be had. Used to
    # CHECK what an account Plex refuses a hide-list for can actually see, rather than assume. None on
    # direct engine runs, where the check is simply skipped.
    pms_for_user: Callable[[UserProfile], object] | None = None
    # That same account's server TOKEN, for reads that go through the owner's client rather than a
    # per-user one — `PlexClient.user_hubs` takes a token, because Home is read from the owner's URL
    # as somebody else. None on direct engine runs, where the enforcement canary is skipped.
    token_for_user: Callable[[UserProfile], str | None] | None = None
    #: Slugs of people our own database says are GONE from Plex — plex.tv stopped listing them, or the
    #: owner removed them. The second of the two guards that let a private-row exclude be pruned (see
    #: `privacy.sync_user_restrictions`). Positive evidence on purpose: it must be something a partial
    #: PMS read cannot manufacture, which "absent from tonight's user list" is not — a `privacy.sync`
    #: runs with NO users at all. None means the adapter could not say, and nothing is pruned.
    departed_slugs: set[str] | None = None
    # plex_account_ids of DISABLED (opted-out) Shortlist users. With config.hide_shared_from_disabled,
    # the privacy sync hides even public shared rows from these accounts, so disabling a user removes
    # them from Shortlist entirely. A non-Shortlist account that merely shares the server is NOT here,
    # so it still sees public shared rows.
    disabled_account_ids: set[int] = field(default_factory=set)
    # plex_account_ids the owner has told Shortlist to LEAVE ALONE: their share filters are never
    # merged into, and any exclude we previously added is taken back out (`privacy.clear_our_excludes`).
    # Separate from `disabled_account_ids` on purpose — disabling someone means "no row for them" and
    # still hides everyone else's rows FROM them; this means "do not touch their Plex sharing at all",
    # and the account can therefore see other people's rows unless their own Plex restrictions stop it.
    # The two combine freely: an account can have a row and untouched sharing.
    unmanaged_account_ids: set[int] = field(default_factory=set)
    # section key -> {tmdb_id: ratingKey}: per-library index so a row delivered into a specific
    # library uses that library's ratingKeys. Built by _build_indexes each run.
    section_index: dict[str, dict[int, int]] = field(default_factory=dict)
    # Every library rows may be delivered to (all movie + show sections), for resolving a row's
    # library_keys to real sections. Built by _build_indexes each run.
    delivery_sections: list = field(default_factory=list)
    # plex account id -> the slug Shortlist assigned that account, for EVERY user it knows (not just
    # tonight's). This is how "whose row is this?" is answered. It cannot be answered from a name:
    # people rename themselves, and two display names can slugify to the same string — either
    # would silently hand one account another's row.
    known_slugs: dict[int, str] = field(default_factory=dict)
    # The server OWNER's slug — the ONE person whose rows may sit on the owner's Home. The converge
    # phase needs it to tell "this row belongs on Home" from "this row is stranded there", and it
    # cannot be derived from `known_slugs` (which carries no type) or from the plex.tv roster (which
    # never returns the owner). Empty = unknown, and converge then does nothing rather than guess.
    owner_slug: str = ""
    # Slugs of PAUSED users. Pause means "stop showing their row", not "delete it": their collection
    # and label stay on the server so everyone else's exclude still matches, and unpausing is a
    # re-promote rather than a full LLM rebuild. They are absent from every run by definition, so
    # converge is the only thing that can act on them.
    paused_slugs: set[str] = field(default_factory=set)
    # May converge DELETE a collection it cannot attribute to anyone, or only hide it? True is set by
    # the server adapter, which knows its DB read succeeded and that `known_slugs` therefore lists
    # every user Shortlist has. Deleting on a partial picture would wipe live rows, so the default is
    # False and every other caller (direct engine runs, tests) only ever demotes.
    may_delete_orphans: bool = False
    # (tmdb_id, media_type) the owner has already actioned in the Requests inbox — sent or rejected.
    # Keeps a slow download from re-winning a request slot every night, and a "no" from being undone
    # by a later auto-send. Empty for direct engine runs, which have no inbox.
    handled_requests: set[tuple[int, str]] = field(default_factory=set)
    # MDBList client (cache-backed) for the chosen non-TMDB rating source; None when neither the
    # request gate nor row ordering asks for one, or no MDBList key is set. Built by the server
    # adapter so it shares the persistent cache.
    mdblist: MdbListClient | None = None
    # Latched by row ordering the first time MDBList answers 429, so the rest of the run orders on
    # TMDB instead of re-attempting once per rating-ordered row PER USER — each attempt is retried
    # three times honouring Retry-After (up to 60s), so an unlatched quota failure costs minutes of
    # stall for results that are discarded anyway.
    mdblist_rate_limited: bool = False
    # (user_slug, stage, counts, reason) -> None. `reason` explains a non-failing outcome (a
    # skipped user) in plain English; None for every stage that needs no explaining.
    progress: Callable[[str, str, dict, str | None], None] | None = None
    # Called the moment one user finishes (before their terminal progress event), with their profile
    # and finished report — so the server can persist that user's results INCREMENTALLY and the UI
    # shows them as each person completes, instead of the whole roster appearing only at run's end.
    # Must be resilient: it runs on the worker threads, and any error is swallowed (never sinks a run).
    on_user_done: Callable[[UserProfile, UserRunReport], None] | None = None
    # Cross-run cache for the per-library tmdb_id -> ratingKey index, keyed by a cheap change signal
    # (item count + last-updated). An unchanged library skips its full scan next run. NullCache (the
    # default) disables it — safe, since a stale/missing entry only ever means a re-scan.
    index_cache: Cache = field(default_factory=NullCache)
    # Cross-run cache for per-title web-search (Exa) results, keyed (media, tmdb_id). A title many
    # users watched is searched ONCE server-wide (Exa bills per search). NullCache disables it — safe,
    # since a miss just re-searches.
    web_search_cache: Cache = field(default_factory=NullCache)
    # (user_slug, row_slug, section_key) -> the `row_recipe` the stored picks were built under.
    # Absent for a row never built, and for picks written before recipes were recorded — both read
    # as "unknown", which deliberately does NOT force a rebuild: a one-off rebuild of every row on
    # every server at upgrade is exactly the churn the refresh cadence exists to prevent.
    previous_recipes: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Day number of this run (date.toordinal()), the phase for refresh rotation so a row shifts
    # day to day but is reproducible within a day. Set at the start of run(); 0 disables rotation.
    run_day: int = 0
    # When this run started, the clock the idle hold measures against (`rows._held_for_idle`): is
    # this row older than its ceiling, and has its owner watched anything since it was built. Set at
    # the start of run() beside `run_day`; None on direct engine calls, which — exactly like
    # `run_day = 0` — means "never hold", so a library caller keeps the plain cadence.
    run_at: datetime | None = None
    # How many users to process concurrently. 1 = fully sequential (the safe engine/test default).
    # The server sets this from `run.concurrency`. Only the READ + LLM work overlaps; every Plex and
    # plex.tv write is serialized by ``write_lock``, so the leak-safe ordering is preserved exactly.
    concurrency: int = 1
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    # Cooperative cancel check — returns True once the run has been asked to stop. The deliver phase
    # checks it before each user and skips the rest; an in-flight user finishes (per-user
    # transactional, rule 6), so a cancel never leaves a half-applied user. The privacy merge +
    # promote still run for the users already delivered, so the server stays consistent. Default:
    # never cancels (direct engine runs and tests can't be cancelled).
    cancelled: Callable[[], bool] = lambda: False


def _emit(ctx: EngineContext, slug: str, stage: str, counts: dict, reason: str | None = None) -> None:
    # Mirror every stage to the container log too, so `docker logs` narrates a run in real time —
    # the same story the UI's activity feed tells, for anyone watching the console.
    logger.info("run · {} · {}{}{}", slug, stage, f" {counts}" if counts else "", f" — {reason}" if reason else "")
    if ctx.progress is not None:
        try:
            ctx.progress(slug, stage, counts, reason)
        except Exception:  # a broken progress listener must never fail a run
            logger.exception("progress callback failed")
