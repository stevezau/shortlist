"""Tautulli API client — friendly names + a connectivity probe.

Watch history itself comes from the per-user share-token PMS read (see ``history.py``), which
superseded Tautulli as the watched-titles source. What's left here: ``friendly_names()`` (a nicer
default row title than the bare Plex username) and ``ping()`` (the settings "Test" button).
"""

from __future__ import annotations

from shortlist.engine.clients import http_retry


class TautulliClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        # A self-hosted Tautulli instance can be slow to answer under load; the shared default gives
        # it the same room as the other read APIs.
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _cmd(self, cmd: str, **params) -> dict | list:
        # Returns the response `data`, whose shape is per-command: get_users → list of user dicts.
        # Some Tautulli commands (get_history) nest ANOTHER `{data: [...], recordsFiltered: ...}`
        # envelope under this `data` — callers must know their command's shape.
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
        nickname always wins.
        """
        # get_users returns the user list AS the response `data` — unlike get_history, whose `data`
        # is a {data: [...], recordsFiltered: ...} envelope. `_cmd` already unwraps `data`, so this
        # is the list; calling `.get("data")` on it raised AttributeError, which sync_users swallowed
        # (0 friendly names for everyone — SFLIX shipped that way).
        rows = self._cmd("get_users")
        names: dict[int, str] = {}
        for row in rows:
            try:
                account_id = int(row.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            friendly = (row.get("friendly_name") or "").strip()
            # Include ALL friendly names, even if they match the username — Tautulli might have
            # capitalization/formatting differences (e.g., "john" vs "John"), and the UI can
            # decide whether to show it. Empty strings are still dropped.
            if account_id and friendly:
                names[account_id] = friendly
        return names
