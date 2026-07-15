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
    # Global curation recipe (the LLM prompt), overridable per user via prefs.
    "curator.prompt_tone": "balanced",
    "curator.prompt_guidance": "",
    "curator.prompt_template": "",
    "row.name_template": "✨ Picked for You",
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
    "requests.rating_source": "tmdb",  # "tmdb" (no setup) | "imdb" (needs an OMDb key)
    "requests.min_rating": 7.0,  # rating floor on the chosen source
    "requests.min_votes": 100,  # vote-count floor on the chosen source
    "requests.min_demand": 1,  # a title must be wanted by at least this many distinct people
    "requests.min_year": 0,  # 0 = any; else only titles released in >= this year
    "requests.max_per_run": 5,  # hard cap on titles auto-requested per run, total
    # Hybrid tier: titles clearing these higher bars auto-send; the rest queue for manual approval.
    "requests.auto_send": True,  # False = fully manual (every qualifying title waits for approval)
    "requests.auto_min_demand": 3,  # auto-send only titles wanted by at least this many people
    "requests.auto_min_rating": 8.0,  # ...and rated at least this high on the chosen source
    "requests.tag": "shortlist",  # tag applied to every title Shortlist adds ("" = no tag)
    "schedule.cron": "30 3 * * *",
    "staleness_runs": 3,
    # Which candidate sources feed recommendations (engine/candidates.py). More = wider recall.
    "candidates.sources": ["tmdb_similar", "tmdb_discover"],
    # Cap on already-finished titles in a row, as a fraction: 0.0 = all fresh (default), 1.0 = no
    # filtering, in between = at most that share of the row may be things already watched. Per-row.
    "recommendations.watched_pct": 0.0,
    "plextv.throttle_s": 1.0,
    "paused_all": False,  # Danger zone: stop all scheduled + manual runs without disabling users
    "setup.completed": False,
    "setup.step": 0,
    "setup.state": {},
}

# Secrets are stored Fernet-encrypted under these keys (never in the clear, never logged).
SECRET_KEYS = {
    "plex.token",
    "tautulli.apikey",
    "curator.api_key",
    "requests.radarr.apikey",
    "requests.sonarr.apikey",
    "requests.omdb.apikey",
    "trakt.client_id",
}

ENV_SEEDS = {
    "PLEX_URL": "plex.url",
    "PLEX_TOKEN": "plex.token",
    "TAUTULLI_URL": "tautulli.url",
    "TAUTULLI_APIKEY": "tautulli.apikey",
    "TMDB_APIKEY": "tmdb.apikey",
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
            if row.key in SECRET_KEYS:
                out[row.key] = "•••••" if row.value.get("v") else ""
            else:
                out[row.key] = row.value["v"]
        return out

    def seed_from_env(self, env: dict[str, str]) -> None:
        """One-time env → DB migration on first boot; env is ignored afterwards."""
        if self.get("setup.env_seeded", False):
            return
        for env_key, setting_key in ENV_SEEDS.items():
            if env.get(env_key):
                self.set(setting_key, env[env_key])
                logger.info("seeded {} from env {} (env ignored from now on)", setting_key, env_key)
        self.set("setup.env_seeded", True)
