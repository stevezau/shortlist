"""Tautulli API client — the preferred watch-history source."""

from __future__ import annotations

import httpx
from loguru import logger


class TautulliClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _cmd(self, cmd: str, **params) -> dict:
        r = httpx.get(
            f"{self._base_url}/api/v2",
            params={"apikey": self._api_key, "cmd": cmd, **params},
            timeout=self._timeout,
        )
        if r.status_code != 200:
            # Never raise_for_status(): its message embeds the full URL, apikey included
            # (plex-safety rule 9 — secrets never in exception messages).
            raise RuntimeError(f"Tautulli API error HTTP {r.status_code} for cmd={cmd}")
        payload = r.json()["response"]
        if payload.get("result") != "success":
            raise RuntimeError(f"Tautulli {cmd} failed: {payload.get('message')}")
        return payload["data"]

    def ping(self) -> bool:
        self._cmd("status")
        return True

    def get_history(self, plex_account_id: int, *, length: int = 200) -> list[dict]:
        """Raw history rows for one user, most recent first."""
        data = self._cmd(
            "get_history",
            user_id=plex_account_id,
            length=length,
            order_column="date",
            order_dir="desc",
        )
        rows = data.get("data", [])
        logger.debug("Tautulli history for account {}: {} rows", plex_account_id, len(rows))
        return rows
