"""Agregarr client — read the home-shelf ordering it has stored, and write a new one.

Agregarr (https://github.com/agregarr/agregarr) manages Plex's Recommended shelf too, and both tools
write ABSOLUTE hub positions, so whoever writes last wins. Left alone the two fight: agregarr
re-applies its stored order every 30 minutes (its `plex-collections-quick-sync` and
`plex-randomize-home-order` jobs), Shortlist repairs the shelf on its next run, and around it goes.

The fix is not to out-write it but to agree with it: agregarr PERSISTS the `sortOrderHome` it has
stored rather than recomputing one, so once its stored order matches what Shortlist put on the
shelf, its next sync is a no-op. See ``shortlist.engine.shelf_mirror`` for the ordering itself; this
module is only the transport.

Verified against agregarr 2.4.2. Two facts about its model that the payloads below depend on:

* `sortOrderHome` is a RELATIVE sort key, not a position. Agregarr sorts every item in a library by
  it and compacts the result to 1..N, so only relative order matters.
* `0` is a "void" value meaning *unplaced*, and void items are appended at the END, not the front.
  ``/api/v1/reorder`` assigns `index + 1` for exactly this reason.

Writes go through ``/api/v1/reorder`` — agregarr's own drag-and-drop endpoint — rather than by
computing `sortOrderHome` values ourselves and PUTting each row. One call per library, using
semantics agregarr supports, instead of ~120 writes reproducing arithmetic we reverse-engineered.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry

# Shorter than the shared default, and fewer tries. This runs at the very END of a run, after every
# row is delivered, hidden and promoted, and it only changes what a third-party tool will do to the
# shelf LATER — so a hung agregarr must cost seconds, not minutes. At the shared 30s/3-attempts an
# instance that accepts connections but never answers would add ~3.5 minutes PER LIBRARY to a run
# (two reads x three attempts, plus the write) for something entirely optional. Giving up early is
# nearly free: the next run re-applies the mirror, and until then the shelf is merely contested.
TIMEOUT_S = 10.0
ATTEMPTS = 2


class AgregarrError(RuntimeError):
    """An agregarr call failed. Never carries the API key (plex-safety rule 9)."""


def _write_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    """The smallest payload for one item in a ``/reorder`` write.

    Agregarr merges what it receives OVER its stored config (`{...stored, ...sent}`), so every field
    we send is a field we overwrite and every field we omit is one we preserve. Sending back the
    whole config we read is therefore the maximum-clobber option, not the safe one.

    What has to be here:

    * ``id`` — how agregarr finds the stored config to merge into.
    * ``configType`` — routes the item to the right config list. It is destructured off before
      storage, so it is never itself persisted.
    * the JOIN KEY (``collectionRatingKey`` or ``hubIdentifier``) — agregarr's type guards are bare
      ``'collectionRatingKey' in config`` / ``'hubIdentifier' in config`` checks, and an item that
      satisfies neither is SKIPPED with a warning while the request still returns 200. Omitting it
      would make the whole write silently do nothing.
    * ``position`` — required by the request schema, which rejects the entire request if any item
      lacks it. Agregarr serves configs that omit the field (106 of 213 on a real instance), so it
      cannot simply be passed through. The stored value is preserved where there is one; only a
      missing one is filled in, because ordering is decided by array index, never by this field.
    """
    out: dict[str, Any] = {"id": item.get("id"), "configType": item.get("configType")}
    for join_key in ("collectionRatingKey", "hubIdentifier"):
        if join_key in item:
            out[join_key] = item[join_key]
    position = item.get("position")
    out["position"] = position if isinstance(position, int) else index
    return out


class AgregarrClient:
    """Talks to one agregarr instance. Read-only unless ``apply_home_order`` is called."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = TIMEOUT_S,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @property
    def _api(self) -> str:
        return f"{self._base_url}/api/v1"

    @property
    def _headers(self) -> dict[str, str]:
        # X-Api-Key, not `Authorization: Bearer` — agregarr answers 401 to a bearer token.
        return {"X-Api-Key": self._api_key}

    def ping(self) -> str:
        """A tiny authenticated call for the settings "Test" button; raises ``AgregarrError`` on failure.

        Returns:
            A plain-English success line naming the instance, e.g. "Connected to Agregarr API 1.0".
        """
        # `/api/v1` itself is unauthenticated, so it proves reachability but NOT the key. `/preexisting`
        # is the endpoint the mirror actually depends on, so probe that: a wrong key fails here the
        # same way it would fail mid-run, which is the point of a Test button.
        payload = self._get("/preexisting", what="collection configs")
        if not isinstance(payload, list):
            raise AgregarrError("Agregarr returned an unexpected response shape for /preexisting")
        version = self._version()
        return f"Connected to {version} — {len(payload)} collection(s) known"

    def _version(self) -> str:
        """Best-effort version string for the Test message; never fails the probe."""
        try:
            r = http_retry.get(self._api, headers=self._headers, timeout=self._timeout)
            data = r.json()
            return f"{data.get('api', 'Agregarr')} {data.get('version', '')}".strip()
        except (httpx.HTTPError, ValueError):
            return "Agregarr"

    def home_items_by_library(self) -> dict[str, list[dict[str, Any]]]:
        """Every config agregarr can place on a home shelf, grouped by Plex library id.

        Both endpoints return the WHOLE instance regardless of library, so this fetches each once
        and groups client-side. Callers with several libraries must use this rather than
        ``home_items`` per library, which would re-download every config once per library.

        Returns:
            ``{library_id: [config, …]}`` — pre-existing collection configs then hub configs, each
            carrying a ``configType``. Order within a library is not meaningful.

        Raises:
            AgregarrError: the instance was unreachable or answered with an unexpected shape.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entries, default_type in ((self._preexisting(), "preExisting"), (self._hub_configs(), "hub")):
            for entry in entries:
                library_id = str(entry.get("libraryId") or "")
                if not library_id:
                    continue
                grouped.setdefault(library_id, []).append(
                    {**entry, "configType": entry.get("configType") or default_type}
                )
        return grouped

    def home_items(self, library_id: str) -> list[dict[str, Any]]:
        """Every config agregarr can place on ONE library's home shelf, as it stores them.

        Convenience wrapper over ``home_items_by_library`` for a single-library caller; it still
        costs a full fetch, so do not loop over it.
        """
        return self.home_items_by_library().get(str(library_id), [])

    def _preexisting(self) -> list[dict[str, Any]]:
        payload = self._get("/preexisting", what="collection configs")
        if not isinstance(payload, list):
            raise AgregarrError("Agregarr /preexisting did not return a list")
        return [e for e in payload if isinstance(e, dict)]

    def _hub_configs(self) -> list[dict[str, Any]]:
        """Plex's built-in hubs ("Recently Added", …) as agregarr configures them.

        `/defaulthubs`, NOT the more obvious `/hubs/configs`: that one is session-authenticated and
        answers a 307 to `/login` for an API key, so following the redirect yields a sign-in page
        rather than JSON. `/defaulthubs` returns the same configs and accepts the key.
        """
        payload = self._get("/defaulthubs", what="hub configs")
        if isinstance(payload, dict):  # tolerate an enveloped variant
            payload = payload.get("hubConfigs", [])
        if not isinstance(payload, list):
            raise AgregarrError("Agregarr /defaulthubs did not return a list")
        return [e for e in payload if isinstance(e, dict)]

    def apply_home_order(self, library_id: str, ordered_items: list[dict[str, Any]]) -> int:
        """Store ``ordered_items`` as the home-shelf order for one library.

        Agregarr assigns each item `sortOrderHome = index + 1` and persists; it does NOT push the
        result to Plex, which is what we want — Shortlist has already placed the shelf, and this
        only stops agregarr's next sync from undoing it.

        An item agregarr knows about but that is ABSENT from ``ordered_items`` keeps its stored key,
        which may well sort it into the middle of the list we just sent — so the caller must pass
        every item it wants placed, not just the ones it cares about.

        Args:
            library_id: Plex library section id, e.g. "1".
            ordered_items: Configs from ``home_items_by_library``, in the desired top-to-bottom order.

        Returns:
            The number of items agregarr reported processing.

        Raises:
            AgregarrError: the write failed or was rejected.
        """
        payload = [_write_item(item, index) for index, item in enumerate(ordered_items)]
        # An item with no `id` cannot be matched to a stored config, and agregarr rejects the WHOLE
        # request over one malformed entry — so dropping it costs one row instead of the library.
        payload = [item for item in payload if item.get("id")]
        if not payload:
            return 0
        body = {
            "libraryId": str(library_id),
            "context": "home",
            "mode": "manual",
            "mixedItems": payload,
        }
        try:
            r = http_retry.request(
                "POST",
                f"{self._api}/reorder",
                headers=self._headers,
                json=body,
                timeout=self._timeout,
                attempts=ATTEMPTS,
            )
        except httpx.HTTPError as e:
            raise AgregarrError(f"Agregarr unreachable while writing the shelf order ({type(e).__name__})") from e
        if r.status_code != 200:
            raise AgregarrError(f"Agregarr rejected the shelf order with HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise AgregarrError("Agregarr returned a non-JSON response to the shelf order write") from None
        processed = data.get("totalItemsProcessed")
        logger.debug(
            "agregarr: stored home order for library {} — {} item(s) sent, {} processed",
            library_id,
            len(ordered_items),
            processed,
        )
        return int(processed) if isinstance(processed, int) else len(ordered_items)

    def _get(self, path: str, *, what: str, follow_redirects: bool = False) -> Any:
        try:
            r = http_retry.get(
                f"{self._api}{path}",
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=follow_redirects,
                attempts=ATTEMPTS,
            )
        except httpx.HTTPError as e:
            raise AgregarrError(f"Agregarr unreachable ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise AgregarrError("Agregarr rejected the API key")
        if r.status_code != 200:
            # Never raise_for_status(): its message embeds the URL (plex-safety rule 9).
            raise AgregarrError(f"Agregarr returned HTTP {r.status_code} for {what}")
        try:
            return r.json()
        except ValueError:
            raise AgregarrError(f"Agregarr returned a non-JSON response for {what}") from None
