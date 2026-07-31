"""Typed access to the settings table; env vars are one-time seeds migrated on first boot.

MPG's proven pattern: `PLEX_URL`-style env vars are read ONCE into the DB and thereafter
ignored — the DB is the source of truth. Infrastructure vars (PORT, TZ, PUID/PGID,
APP_BASE_PATH) stay live and are never persisted.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from shortlist.server.db.models import Setting

DEFAULTS: dict[str, Any] = {
    "plex.url": "",
    "tautulli.url": "",
    "tmdb.apikey": "",
    "curator.provider": "none",
    "curator.model": "",
    "curator.ollama_url": "http://localhost:11434",  # Ollama needs a URL, not a key
    # Any server speaking the OpenAI API (llama.cpp, LM Studio, vLLM, LocalAI, OpenRouter): its
    # root, usually ending in /v1. Used only when curator.provider is openai_compatible.
    "curator.openai_base_url": "",
    "row.name_template": "✨ {library_name} Picked for You",  # {library_name} -> each library's own name
    "row.size": 15,
    # Requests (Sonarr/Radarr): ask for picks the library doesn't have yet. Off by default and
    # gated so it can never balloon a library — a title must clear BOTH thresholds, and only the
    # top N per run are ever requested. API keys live in SECRET_KEYS below (encrypted at rest).
    "requests.enabled": False,
    "requests.radarr.url": "",
    "requests.radarr.quality_profile_id": 0,
    "requests.radarr.root_folder": "",
    "requests.sonarr.url": "",
    "requests.sonarr.quality_profile_id": 0,
    "requests.sonarr.root_folder": "",
    "requests.rating_source": "tmdb",  # tmdb (no setup) | imdb | trakt | tomatoes | metacritic (via MDBList)
    "requests.min_rating": 7.0,  # rating floor on the chosen source
    "requests.min_votes": 100,  # vote-count floor on the chosen source
    "requests.min_demand": 1,  # a title must be wanted by at least this many distinct people
    "requests.min_year": 0,  # 0 = no lower bound; else only titles from >= this year (shows: first-air year)
    "requests.max_year": 0,  # 0 = no upper bound; else only titles from <= this year (shows: first-air year)
    "requests.max_per_run": 5,  # hard cap on titles auto-requested per run, total
    # Hybrid tier: titles clearing these higher bars auto-send; the rest queue for manual approval.
    "requests.auto_send": True,  # False = fully manual (every qualifying title waits for approval)
    "requests.auto_min_demand": 3,  # auto-send only titles wanted by at least this many people
    "requests.auto_min_rating": 8.0,  # ...and rated at least this high on the chosen source
    "requests.tag": "shortlist",  # tag applied to every title Shortlist adds ("" = no tag)
    # (per-row schedules replaced the old global `schedule.cron` — each row carries its own cron on
    # the collections table; see Collection.schedule and shortlist/server/scheduler.py)
    # Where Shortlist's rows sit in each library's Plex "Recommended" shelf, keyed by library (section)
    # key: {"anchor": "<collection title>", "before": false}. Empty = leave Plex's default order (rows
    # land last, under any co-managing tool like Kometa). Re-applied at end of each run; anchor is
    # read-only, only our rows move.
    "rows.hub_anchor": {},
    # Master switch for Shortlist touching the Recommended-shelf ORDER. False -> never reorder the
    # shelf (a co-managing tool like agregarr/Kometa owns the order). Default on.
    "rows.manage_shelf_order": True,
    # How many months of run history to keep. After each run, anything older is auto-pruned (runs +
    # per-user traces are deleted; picks are kept so the dashboard's lifetime metrics survive).
    # 0 = keep everything forever.
    "runs.retention": 3,
    # How many months of the audit trail (`events`) to keep. 0 = forever, and that is the default:
    # "what changed on whose share at 03:31" (plex-safety rule 10) is the one record an operator may
    # want long after the run detail around it is gone.
    "events.retention": 0,
    # The cron expression for the daily watch-history sync. Blank = the default (04:17 daily).
    "sync.watch_cron": "",
    # Read only what changed since the last sync (Plex's `lastViewedAt>=` filter) instead of every
    # watched title, every night, per user, per library. An incremental read cannot see an un-watch
    # or a deletion, so a COMPLETE read still runs every `sync.watch_full_days` regardless — this
    # switch only decides whether the nights in between are cheap. Off = always read everything.
    "sync.watch_incremental": True,
    # How often the complete re-read happens, in days. It is the only thing that can notice a title
    # being un-watched or removed, so it is not optional — only its frequency is.
    "sync.watch_full_days": 7,
    # The cron expression for the user-list sync (plex.tv + Tautulli). Blank = the default (daily 04:47).
    "sync.users_cron": "",
    # The cron for the nightly privacy sync — a re-merge of every account's share filter.
    #
    # It exists because the automatic Privacy Check + write gate was removed on 2026-07-16 at the
    # owner's request: nothing verifies hiding after the fact any more, so leak-safe write ORDERING
    # is the only remaining guarantee. This is the cheapest safety net against drift, and it can only
    # ever make the server more private (it builds, delivers and promotes nothing).
    "privacy.sync_cron": "15 5 * * *",
    # The cron for the drift check. Blank = off; it is a converge-to-desired-state pass that is safe
    # to schedule, but it WRITES to Plex, so turning it on is a deliberate choice.
    # Nightly at 05:45 — see scheduler._SYNC_CHECK_CRON. Clearing it in the UI stores an empty
    # row, which the scheduler reads as an explicit "off" rather than "inherit this".
    "sync.check_cron": "45 5 * * *",
    # Backup schedule. Blank cron = the default (daily 03:00). max_keep = number of backups to retain.
    "backup.cron": "",
    "backup.max_keep": 10,
    # How many read-only jobs may run at once. Jobs that WRITE to Plex/plex.tv are always exclusive
    # and never overlap a run — share-filter writes are read-modify-write merges, so two at once lose
    # one of them (plex-safety rule 3). Dial to 1 if a PMS complains about the concurrency.
    "jobs.max_parallel_readonly": 3,
    # Notification ids the owner dismissed. Each id encodes its state (run id / version), so the same
    # alert stays hidden but a new failure or a newer release surfaces again. Capped to the newest 100.
    "notifications.dismissed": [],
    # Which candidate sources feed recommendations (engine/candidates.py). More = wider recall.
    "candidates.sources": ["tmdb_similar", "tmdb_discover"],
    # How the "AI — web search" (llm_web) source searches the web: 'native' (the curator provider's
    # own web-search tool — Claude/GPT/Gemini), 'exa' (the Exa search API — works for every provider,
    # the only path for local Ollama), or 'auto' (native where the provider supports it, else Exa).
    "llm_web.search_provider": "auto",
    # Cap on already-finished titles in a row, as a fraction: 0.0 = all fresh (default), 1.0 = no
    # filtering, in between = at most that share of the row may be things already watched. Per-row.
    "recommendations.watched_pct": 0.0,
    # Freshness is the REFRESH CADENCE, not a nightly shuffle: 0.0 = never refresh once built (a
    # frozen, pinned row), 1.0 = rebuild every night, in between = every N days (0.5 ≈ weekly). On a
    # refresh night the strongest ~two-thirds stay and the weakest third is swapped for new picks; on
    # every other night the row is reused unchanged (no re-curation, no Plex write). Default weekly so
    # rows feel curated and stable instead of churning completely every night. Per-row overridable.
    "recommendations.freshness": 0.5,
    # How many of a person's most recent watches the web-search source searches per row (one cached
    # Exa search each). Row-overridable. Fewer = tighter/cheaper; the DbCache dedups shared titles.
    "recommendations.recent_count": 10,
    # How many watched titles SEED a row — what every source searches from, not just the web one.
    # Row-overridable, and the row is where a deliberately narrow value belongs: the floor here is 5
    # because a server-wide 1 or 2 would starve every movies-and-TV row of one of its media types.
    "recommendations.max_seeds": 30,
    # TMDB ids that must never seed a SHARED row. Separate from each person's own blocked seeds on
    # purpose: a shared row is public, so letting one person's block reshape what everyone sees would
    # make an individual preference into a server-wide edit nobody else can see or undo.
    "recommendations.blocked_shared_seeds": [],
    # When on (default), disabling a user hides EVERY shared row from them too (even public "Popular on
    # this server" rows), so a disabled user sees nothing from Shortlist. Off = disabled users still see
    # public shared rows like any other account with library access.
    "privacy.hide_shared_from_disabled": True,
    # How long (seconds) to wait on a single PMS call before giving up and retrying. Reads are near-
    # instant on a LAN, but rebuilding a big library's collection (a TV row on a large server) legitimately
    # takes 15-20s+, so too low a value times those out and forces a wasteful retry. 20 proved too tight
    # for large TV libraries (SFLIX 2026-07-20: legit writes at 19.9s, many ERR at 20.0s then retried); 45
    # gives headroom while still failing a truly-stalled call. Raise it if big writes still time out. Advanced.
    "plex.timeout_s": 45,
    "plextv.throttle_s": 0.0,  # FLOOR between plex.tv writes; 0 = as fast as plex.tv accepts (adaptive 429 backoff)
    # How many users a run processes concurrently. Only their reads + AI curation overlap; every Plex
    # and plex.tv write stays strictly serial. 1 = fully sequential; higher = faster big runs at the
    # cost of more concurrent load on Plex + the AI provider. 4 is a safe default.
    "run.concurrency": 4,
    # Console/file log verbosity for the container. DEBUG (the default, so `docker logs` narrates a
    # run in full out of the box) surfaces per-source candidate counts, AI request/response with
    # tokens+latency, HTTP call timing, cache hits and throttle waits; TRACE adds the full prompts;
    # INFO trims to the stage narration. Live-changeable in Settings → Diagnostics.
    "log.level": "DEBUG",
    "paused_all": False,  # Danger zone: stop all scheduled + manual runs without disabling users
    "setup.completed": False,
    "setup.step": 0,
    "setup.state": {},
}

# Secrets are stored Fernet-encrypted under these keys (never in the clear, never logged).
SECRET_KEYS = {
    "plex.token",
    "tautulli.apikey",
    "tmdb.apikey",  # was the ONE api key stored in the clear — and returned unredacted by all_public()
    "curator.api_key",
    "requests.radarr.apikey",
    "requests.sonarr.apikey",
    "requests.mdblist.apikey",  # MDBList key for IMDb/Trakt/RT/Metacritic rating gating
    "trakt.client_id",
    "exa.apikey",  # Exa web-search API key for the llm_web source
    "api.token",  # our own programmatic API token (encrypted at rest so the owner can reveal it)
}

# Keys stored server-side but NEVER returned by all_public() and never writable via the generic
# settings PUT — they are managed only through their own dedicated endpoints (here: the API token and
# its metadata). `api.token` is ALSO in SECRET_KEYS so it's encrypted at rest; being private keeps it
# out of the general settings response and the PUT allowlist regardless.
# `api.token_hash`/`api.token_hint` are tombstones: an earlier hash-only version persisted them as
# NON-secret keys, so they must stay listed here or an upgraded DB would leak them via all_public()
# (they're also deleted on boot — see LEGACY_KEYS).
PRIVATE_KEYS = {"api.token", "api.token_created_at", "api.token_hash", "api.token_hint"}

# Dropped keys purged from the settings table on boot, so stale rows don't linger.
LEGACY_KEYS = {"api.token_hash", "api.token_hint", "requests.omdb.apikey", "staleness_runs", "requests.auto_user_tag"}

ENV_SEEDS = {
    "PLEX_URL": "plex.url",
    "PLEX_TOKEN": "plex.token",
    "TAUTULLI_URL": "tautulli.url",
    "TAUTULLI_APIKEY": "tautulli.apikey",
    "TMDB_APIKEY": "tmdb.apikey",
    "LOG_LEVEL": "log.level",
}


class SettingsStore:
    def __init__(self, session: Session, secret_box=None):
        self._session = session
        self._secrets = secret_box

    def get(self, key: str, default: Any = None) -> Any:
        row = self._session.get(Setting, key)
        if row is None:
            return DEFAULTS.get(key, default)
        value = row.value["v"]
        if key in SECRET_KEYS and value and self._secrets:
            return self._secrets.decrypt(value)
        return value

    def set(self, key: str, value: Any) -> None:
        if key in SECRET_KEYS and value and self._secrets:
            value = self._secrets.encrypt(str(value))
        row = self._session.get(Setting, key)
        if row is None:
            self._session.add(Setting(key=key, value={"v": value}))
        else:
            row.value = {"v": value}
        self._session.commit()

    def all_public(self) -> dict[str, Any]:
        """Everything except secrets; secrets appear redacted when set (UI contract)."""
        out = dict(DEFAULTS)
        for row in self._session.query(Setting).all():
            if row.key in PRIVATE_KEYS:
                continue  # never surfaced to any client — managed via dedicated endpoints only
            if row.key in SECRET_KEYS:
                out[row.key] = "•••••" if row.value.get("v") else ""
            else:
                out[row.key] = row.value["v"]
        return out

    def encrypt_plaintext_secrets(self) -> list[str]:
        """Re-store any SECRET_KEY still sitting in the clear, encrypted. Returns the keys healed.

        `tmdb.apikey` was the one API key missing from SECRET_KEYS, so it was plaintext at rest AND
        returned unredacted by `all_public()` — visible to anything with a session, and to anyone
        handed a `/config` backup. Simply adding it to the set is not enough: `get()` would then try to
        Fernet-decrypt the existing plaintext and raise, breaking TMDB (and so every recommendation)
        on every existing install.

        Runs at boot, idempotent, and covers any key added to SECRET_KEYS in future — an encrypted
        value round-trips, a plaintext one is re-written. Detection is by decryptability rather than a
        prefix check, so it cannot be fooled by a key that merely looks Fernet-shaped.
        """
        if not self._secrets:
            return []
        healed = []
        for key in sorted(SECRET_KEYS):
            row = self._session.get(Setting, key)
            value = (row.value or {}).get("v") if row else None
            if not value or not isinstance(value, str):
                continue
            try:
                self._secrets.decrypt(value)
            except Exception:  # any decrypt failure means the value is not encrypted
                row.value = {"v": self._secrets.encrypt(value)}
                healed.append(key)
        if healed:
            self._session.commit()
        return healed

    def purge_legacy(self) -> None:
        """Delete rows for keys we no longer use (e.g. the old hash-only API-token fields), so stale
        data doesn't linger in the settings table across an upgrade."""
        rows = self._session.query(Setting).filter(Setting.key.in_(LEGACY_KEYS)).all()
        if rows:
            for row in rows:
                self._session.delete(row)
            self._session.commit()

    def seed_from_env(self, env: dict[str, str]) -> None:
        """One-time env → DB migration on first boot; env is ignored afterwards."""
        if self.get("setup.env_seeded", False):
            return
        for env_key, setting_key in ENV_SEEDS.items():
            if env.get(env_key):
                self.set(setting_key, env[env_key])
                logger.info("seeded {} from env {} (env ignored from now on)", setting_key, env_key)
        self.set("setup.env_seeded", True)
