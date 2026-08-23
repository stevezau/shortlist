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
from shortlist.server.scheduler import DEFAULT_CRONS as _DEFAULT_CRONS

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
    # Also tag each request with the WANTING PERSON'S slug, so the owner can tell in Sonarr/Radarr
    # who a title was added for. Off by default; a row may override it either way.
    "requests.auto_user_tag": False,
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
    # Read only what changed since the last sync instead of every watched title, every night, per
    # user, per library. An incremental read notices an un-watch inside the window it covered, but
    # nothing further back and no deletion, so a COMPLETE read still runs every `sync.watch_full_days`
    # regardless — this switch only decides whether the nights in between are cheap. Off = always
    # read everything.
    "sync.watch_incremental": True,
    # How often the complete re-read happens, in days. It is the only thing that can notice a title
    # un-watched or removed longer ago than the nightly read reaches back, so it is not optional —
    # only its frequency is.
    "sync.watch_full_days": 7,
    # (the schedulable crons are added below, derived from scheduler.DEFAULT_CRONS)
    "backup.max_keep": 10,  # how many backups to retain
    # How many read-only jobs may run at once. Jobs that WRITE to Plex/plex.tv are always exclusive
    # and never overlap a run — share-filter writes are read-modify-write merges, so two at once lose
    # one of them (plex-safety rule 3). Dial to 1 if a PMS complains about the concurrency.
    "jobs.max_parallel_readonly": 3,
    # Notification ids the owner dismissed. Each id encodes its state (run id / version), so the same
    # alert stays hidden but a new failure or a newer release surfaces again. Capped to the newest 100.
    "notifications.dismissed": [],
    # Which candidate sources feed recommendations (engine/candidates.py). More = wider recall.
    "candidates.sources": ["tmdb_similar", "tmdb_discover"],
    # Which backend the "AI — web search" (llm_web) source searches with. Exactly one, always:
    #   'native'  — the curator provider's own web-search tool (Claude/GPT/Gemini only)
    #   'exa'     — the hosted Exa search API
    #   'searxng' — the owner's own SearXNG instance. Self-hosted metasearch: no vendor account, key
    #               or per-search bill, though it still FORWARDS each query to real engines
    #               (Google/Brave/DDG), so it is not an air-gapped path.
    # Either external works with every provider and is the only kind a local Ollama model can use.
    # There was a fourth value, 'auto' (native UNIONED with whichever external was configured). It
    # was the default and it was removed in 1.3 — the name described nothing, and owners could not
    # tell what it was doing. Migration 0063 pins every existing install to what it was really using.
    "llm_web.search_provider": "native",
    # Self-hosted SearXNG for the llm_web source. Its JSON API must be enabled — a stock instance
    # ships `search.formats: [html]` and answers `format=json` with a 403. Username/password are for
    # a reverse proxy in front of it (SearXNG itself has no auth); the password is a SECRET_KEY.
    "searxng.url": "",
    "searxng.username": "",
    # Cap on already-finished titles in a row, as a fraction: 0.0 = all fresh (default), 1.0 = no
    # filtering, in between = at most that share of the row may be things already watched. Per-row.
    "recommendations.watched_pct": 0.0,
    # The REFRESH CADENCE in days, not a nightly shuffle: 0 = never refresh once built (a frozen,
    # pinned row), 1 = rebuild every night, N = every N days. On a refresh night the strongest
    # ~two-thirds stay and the weakest third is swapped for new picks; on every other night the row is
    # reused unchanged (no re-curation, no Plex write). Per-row overridable.
    #
    # 8 rather than 7 because 8 is exactly what the old `recommendations.freshness` default of 0.5
    # resolved to, and migration 0065 must not shift the cadence of a server that never set it.
    "recommendations.refresh_days": 8,
    # How much a title's RELEASE DATE counts when ranking it: 0.0 = ignore age, 1.0 = every ~8 years
    # of age halves a title's weight. A weight, never a filter — an old title is only asked to be a
    # better match. Distinct from the cadence above, which is HOW OFTEN a row rebuilds, not which
    # titles win when it does.
    # 0.5 = "leans towards recent releases", the phrasing the UI uses for this value. NOT 0.0:
    # age-blind is not neutral, it is the status quo, and the status quo is a pool of mostly-old
    # candidates deciding the row. This default applies to EVERY install, existing servers
    # included — the pin migrations were dropped deliberately (owner decision, 2026-08-11), because
    # a default that means one thing on a new server and another on an old one is two products.
    # Nothing is rewritten at upgrade: a row adopts it on its next refresh night.
    "recommendations.recency": 0.5,
    # How many of a person's most recent watches the web-search source searches per row (one cached
    # Exa search each). Row-overridable. Fewer = tighter/cheaper; the DbCache dedups shared titles.
    "recommendations.recent_count": 10,
    # How many watched titles SEED a row — what every source searches from, not just the web one.
    # Row-overridable, and the row is where a deliberately narrow value belongs: the floor here is 5
    # because a server-wide 1 or 2 would starve every movies-and-TV row of one of its media types.
    "recommendations.max_seeds": 30,
    # Which service's score a row ordered by "Highest rated" sorts on. "tmdb" needs no setup and is
    # already on every candidate; the rest come from MDBList (one cached lookup per title, shared by
    # every row and user) and need `requests.mdblist.apikey` — without it, ordering falls back to TMDB.
    "recommendations.rating_source": "tmdb",
    # How many watched titles someone needs before Shortlist recommends FROM their taste rather than
    # falling back to the server's top-rated titles. Was welded to the engine default (10) and
    # unreachable; owners of small or new servers legitimately want it lower.
    "recommendations.min_history": 10,
    # What someone below that threshold gets: "popular" (a row of the server's highest-rated titles)
    # or "skip" (no row at all, and any row they already have is removed). Row-overridable — a
    # `{top_seed}` row is the one most worth skipping, since it has no seed to name itself after.
    # Default "popular" is the pre-1.1 behaviour, so upgrading never silently removes a row.
    "recommendations.cold_start": "popular",
    # TMDB ids that must never seed a SHARED row. Separate from each person's own blocked seeds on
    # purpose: a shared row is public, so letting one person's block reshape what everyone sees would
    # make an individual preference into a server-wide edit nobody else can see or undo.
    "recommendations.blocked_shared_seeds": [],
    # When on (default), a title someone rated low in Plex stops being used to find similar things
    # for them. It rides on the watched read the sync already makes, so it costs nothing, and a title
    # nobody rated is untouched — which on a real server is 99.7% of watches. Deliberately NOT
    # row-overridable: a rating is a fact about a person, not about one of their rows.
    "recommendations.use_plex_ratings": True,
    # The 0..10 Plex rating at or below which that happens. 2 = one star, which is also where a
    # thumbs-down lands. Capped at 6 by the validator: above three stars "disliked" stops being a
    # fair reading, and 10 would suppress every rated title at once.
    "recommendations.dislike_threshold": 2.0,
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
    # Support Mode's expiry, as an ISO timestamp ("" = off). The diagnostics surface refuses every
    # tool until this is in the future, and it lapses on its own.
    #
    # Listed here for the DEFAULT and for documentation only — it is also in PRIVATE_KEYS, which
    # keeps it out of `KNOWN_KEYS` and therefore out of `PUT /api/settings`. Without that, the mode
    # was settable as an ordinary setting: a write of a far-future timestamp switched every tool on
    # with no `support.enable` event and no 24h lapse, defeating both halves of the boundary. A
    # settings restore or a stray Settings-page save could do it by accident.
    "support.enabled_until": "",
}

# Every schedulable cron ships BLANK, meaning "use the built-in default". The expressions themselves
# live in exactly one place — `scheduler.DEFAULT_CRONS` — and are NOT copied here: a second copy is
# what let the drift check be documented as off-by-default for months while it ran nightly. Deriving
# the keys also means a cron added there is automatically accepted by `PUT /api/settings`, whose
# allowlist is built from this dict.
#
# The scheduler resolves a cron from the RAW settings row, never through here, because a stored blank
# and an absent row must stay distinguishable: for `sync.check_cron` — the one schedule the UI offers
# to switch off — a stored blank means OFF, while an absent row means "nightly at 05:45". So the
# effective schedule is `scheduler.effective_cron`, not this default.
DEFAULTS.update({key: "" for key in _DEFAULT_CRONS})

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
    "searxng.password",  # reverse-proxy password guarding a self-hosted SearXNG
    "api.token",  # our own programmatic API token (encrypted at rest so the owner can reveal it)
}

# Keys stored server-side but NEVER returned by all_public() and never writable via the generic
# settings PUT — they are managed only through their own dedicated endpoints (here: the API token and
# its metadata). `api.token` is ALSO in SECRET_KEYS so it's encrypted at rest; being private keeps it
# out of the general settings response and the PUT allowlist regardless.
# `api.token_hash`/`api.token_hint` are tombstones: an earlier hash-only version persisted them as
# NON-secret keys, so they must stay listed here or an upgraded DB would leak them via all_public()
# (they're also deleted on boot — see LEGACY_KEYS).
PRIVATE_KEYS = {
    "api.token",
    "api.token_created_at",
    "api.token_hash",
    "api.token_hint",
    # Not a secret, but must never be writable through the generic settings PUT: it is Support Mode's
    # own gate, and the whole point is that switching it on is audited and self-reversing. Its three
    # dedicated endpoints (`/support/status|enable|disable`) are the only way in.
    "support.enabled_until",
    # The playback listener's own health. Facts it observes about itself, not preferences anyone sets:
    # `watch.stream_down_since` is what raises a NON-dismissable alert, so a settings write that could
    # clear it would be a way to silence exactly the warning that must not be silenceable.
    "watch.stream_connected_at",
    "watch.stream_down_since",
}

# Dropped keys purged from the settings table on boot, so stale rows don't linger.
#
# A dropped SECRET has to stay listed here even after a migration deletes it. `all_public()` iterates
# the settings ROWS and redacts by looking each key up in SECRET_KEYS, so a key removed from that set
# while a row survives is returned in the CLEAR — the exact opposite of what dropping it intended
# (rule 9). Migration 0067 deletes the two agregarr rows, but a DB restored from an older backup, or
# one that downgraded, still carries them; this purge is what makes the guarantee independent of
# whether any particular migration ran.
LEGACY_KEYS = {
    "api.token_hash",
    "api.token_hint",
    "requests.omdb.apikey",
    "staleness_runs",
    "agregarr.url",
    "agregarr.apikey",
}

ENV_SEEDS = {
    "PLEX_URL": "plex.url",
    "PLEX_TOKEN": "plex.token",
    "TAUTULLI_URL": "tautulli.url",
    "TAUTULLI_APIKEY": "tautulli.apikey",
    "TMDB_APIKEY": "tmdb.apikey",
    "LOG_LEVEL": "log.level",
}


_UNSET = object()


class SettingsStore:
    def __init__(self, session: Session, secret_box=None):
        self._session = session
        self._secrets = secret_box

    @staticmethod
    def _unwrap(row: Setting, key: str) -> Any:
        """The stored value, or `_UNSET` when the row is not the `{"v": ...}` shape every write uses.

        A row that is somehow not that shape — a hand-edited database, a half-written migration, a
        future format — must read as "unset" and fall back, not raise. This is read during boot (the
        scheduler resolves every cron here), so an exception is a crash loop rather than one broken
        setting; and `all_public()` backs the whole Settings page, where one bad row used to mean a
        500 recoverable only by hand-editing SQLite.
        """
        if not isinstance(row.value, dict) or "v" not in row.value:
            logger.warning("setting {!r} has an unreadable value — using the default", key)
            return _UNSET
        return row.value["v"]

    def _require_box(self, key: str) -> None:
        """A secret key may only be read or written through a store that can encrypt it.

        Without this the crypto silently short-circuits and `set("plex.token", …)` writes the owner's
        token to the DB in the clear (plex-safety rule 9). Failing loudly is the only safe direction:
        a caller that reached a SECRET_KEY without a box is wired wrong, not merely unlucky.
        """
        if key in SECRET_KEYS and self._secrets is None:
            raise RuntimeError(
                f"settings key {key!r} is a secret — SettingsStore needs a SecretBox to read or write it"
            )

    def get(self, key: str, default: Any = None) -> Any:
        self._require_box(key)
        row = self._session.get(Setting, key)
        if row is None:
            return DEFAULTS.get(key, default)
        value = self._unwrap(row, key)
        if value is _UNSET:
            return DEFAULTS.get(key, default)
        if key in SECRET_KEYS and value:
            return self._secrets.decrypt(value)
        return value

    def has_row(self, key: str) -> bool:
        """Whether this key has been WRITTEN, as opposed to falling back to its default.

        `get` deliberately hides that distinction — a caller wants the effective value. For the
        off-able crons it matters: an absent row means "run at the built-in default", a stored blank
        means OFF, and both come back from `get` as "". Anything that must tell "never set" from
        "set to empty" — the settings audit, for one — asks here.
        """
        return self._session.get(Setting, key) is not None

    def set(self, key: str, value: Any) -> None:
        self._require_box(key)
        if key in SECRET_KEYS and value:
            value = self._secrets.encrypt(str(value))
        row = self._session.get(Setting, key)
        if row is None:
            self._session.add(Setting(key=key, value={"v": value}))
        else:
            row.value = {"v": value}
        self._session.commit()

    def unset(self, key: str) -> bool:
        """Delete this key's row, putting it back to "never written". Returns whether a row went.

        The counterpart to `has_row`, and the only way to express "use the built-in default" for a
        cron the UI can switch off: writing "" there means OFF (`scheduler._OFF_ABLE`), so the
        default is reachable ONLY by removing the row. Storing a blank and deleting the row are
        different states — see `scheduler._resolve_cron`.
        """
        row = self._session.get(Setting, key)
        if row is None:
            return False
        self._session.delete(row)
        self._session.commit()
        return True

    def all_public(self) -> dict[str, Any]:
        """Everything except secrets; secrets appear redacted when set (UI contract).

        Reads secret rows without decrypting them — only their truthiness decides the redaction — so
        this stays callable from a store with no SecretBox.
        """
        # The DEFAULTS seed is filtered too, not just the stored rows. Skipping private keys only on
        # the way out of the DB still surfaced any private key that HAS a default, with its default
        # value — which contradicts the promise directly above. It went unnoticed while every private
        # key was an `api.token*` field with no DEFAULTS entry; `support.enabled_until` is the first
        # that has one.
        out = {k: v for k, v in DEFAULTS.items() if k not in PRIVATE_KEYS}
        for row in self._session.query(Setting).all():
            if row.key in PRIVATE_KEYS:
                continue  # never surfaced to any client — managed via dedicated endpoints only
            value = self._unwrap(row, row.key)
            if value is _UNSET:
                continue  # leave the default in place
            out[row.key] = ("•••••" if value else "") if row.key in SECRET_KEYS else value
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
