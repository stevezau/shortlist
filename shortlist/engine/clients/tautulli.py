"""Tautulli API client — the preferred watch-history source."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.clients import http_retry


class TautulliClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _cmd(self, cmd: str, **params) -> dict:
        r = http_retry.get(
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

    def friendly_names(self) -> dict[int, str]:
        """plex account id -> the friendly name Tautulli shows for them, for accounts that have one.

        Tautulli is where most people have already renamed "mrjohnpoz" to something human, so it's a
        better default row title than the Plex username — but only a DEFAULT: Shortlist's own
        nickname always wins. Entries whose friendly name is just the username again are dropped, so
        an untouched Tautulli install contributes nothing.
        """
        rows = self._cmd("get_users").get("data", [])
        names: dict[int, str] = {}
        for row in rows:
            try:
                account_id = int(row.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            friendly = (row.get("friendly_name") or "").strip()
            if account_id and friendly and friendly != (row.get("username") or "").strip():
                names[account_id] = friendly
        return names

    def get_history(self, plex_account_id: int, *, since_ts: int | None = None, page_size: int = 1000) -> list[dict]:
        """History rows for one user, most recent first — fully paginated, optionally only since a time.

        Was a single 200-row page, which hid a heavy watcher's older watches from the already-watched
        filter. Pages through the whole history; when ``since_ts`` (a unix timestamp) is given it stops
        as soon as it reaches rows at or before it, so the incremental sync pulls only new plays.
        """
        rows: list[dict] = []
        start = 0
        while True:
            data = self._cmd(
                "get_history",
                user_id=plex_account_id,
                start=start,
                length=page_size,
                order_column="date",
                order_dir="desc",
            )
            page = data.get("data", [])
            if not page:
                break
            if since_ts is not None:
                fresh = [r for r in page if int(r.get("date") or 0) > since_ts]
                rows.extend(fresh)
                if len(fresh) < len(page):  # crossed the watermark within this page — done
                    break
            else:
                rows.extend(page)
            if len(page) < page_size:
                break
            start += page_size
        logger.debug("Tautulli history for account {}: {} rows", plex_account_id, len(rows))
        return rows
