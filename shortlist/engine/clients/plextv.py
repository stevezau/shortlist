"""plex.tv client (raw httpx) — the share-filter and Home-user surfaces plexapi doesn't cover.

plex.tv quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Share filters are attributes of ``<User>`` in ``GET /api/users``; ``PUT /api/users/{id}``
  persists them verbatim.
- Writes are ADAPTIVELY throttled (plex-safety rule 6): fire at a floor pace (default 0 = as fast
  as plex.tv accepts), and on a 429 grow the spacing (to >=1s, then x2, capped 30s), easing back
  toward the floor on each clean write.
- A Home-user switch token gets 401 on the PMS; it must be exchanged for the server-scoped
  ``accessToken`` via ``GET /api/v2/resources`` as the switched user.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import UserType

PLEXTV = "https://plex.tv"
CLIENT_ID = "shortlist"


class FilterWriteRefused(RuntimeError):
    """plex.tv permanently refused a share-filter write (HTTP 422).

    Distinct from transient errors (429, 5xx, network): a 422 means plex.tv will NEVER accept this
    write for this account in its current state (e.g. accounts with parental restriction profiles).
    The pipeline catches this separately so it doesn't block promotion for every other user (#14).
    """


@dataclass(frozen=True)
class PlexTvUser:
    """One row from plex.tv /api/users (shared or Home user)."""

    id: int
    username: str
    user_type: UserType
    home: bool
    restricted: bool
    protected: bool
    avatar_url: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    # The parental preset applied to a MANAGED account: "little_kid" | "older_kid" | "teen", or "" for
    # none. Only `/api/home/users` reports it — `/api/users` says `restricted="1"` for every managed
    # account and cannot tell the two apart, which is the whole of issue #20.
    #
    # It decides whether a share-filter write is even possible. Plex's own documentation: "For Managed
    # users, the restriction profile must be set to None if you wish to edit Rating and Label
    # restrictions on the library types."
    # (support.plex.tv/articles/204232573-restricting-the-shares/). Live-confirmed on a real server
    # 2026-07-29: a `little_kid` account 422s the write AND sees 0 collections of any kind, so skipping
    # it is both necessary and harmless; a profile-less managed account is an ordinary user that has
    # simply never been given its excludes.
    restriction_profile: str = ""


class PlexTvClient:
    """Thin plex.tv API client for the surfaces plexapi doesn't cover well."""

    # Adaptive write throttle (rule 6): plex.tv is shared infra, but a fixed 1/s is needlessly slow on
    # a healthy server. Instead fire at the FLOOR pace, and only slow down when plex.tv actually pushes
    # back — a 429 grows the spacing (to at least _SLOW_TO, then exponentially, capped at _MAX_PACE),
    # and clean writes decay it back toward the floor. Fast in the common case, self-limiting under load.
    _SLOW_TO = 1.0  # the first 429 jumps the pace to at least this (the old conservative rate)
    _MAX_PACE = 30.0  # never space writes further apart than this, even after repeated 429s

    def __init__(
        self,
        token: str,
        machine_id: str,
        *,
        min_write_interval: float = 0.0,
        # plex.tv is shared infra outside our control, no closer than the other read APIs — the
        # shared ceiling gives it the same tolerance.
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
    ):
        self._token = token
        self._machine_id = machine_id
        self._floor = max(0.0, min_write_interval)  # fastest allowed spacing between writes
        self._pace = self._floor  # current adaptive spacing; grows on 429, decays on success
        self._timeout = timeout
        self._last_write = 0.0
        # Cached `/api/home/users` lookup — profiles do not change during a run, and `list_users()` is
        # called several times (privacy sync, the read-back verification, uninstall's per-user restore).
        # Without this, every one of those paid a second plex.tv GET for a value most never read.
        self._home_profiles: dict[int, str] | None = None

    def _slow_down(self) -> None:
        """A 429 means we're going too fast — widen the spacing for the writes that follow."""
        self._pace = min(self._MAX_PACE, max(self._SLOW_TO, self._pace * 2))

    def _speed_up(self) -> None:
        """A clean write means there's headroom — ease the spacing back toward the floor."""
        if self._pace > self._floor:
            self._pace = max(self._floor, self._pace / 2)

    def _headers(self, token: str | None = None, json: bool = False) -> dict[str, str]:
        h = {"X-Plex-Token": token or self._token, "X-Plex-Client-Identifier": CLIENT_ID}
        if json:
            h["Accept"] = "application/json"
        return h

    def _throttle(self) -> None:
        # Space writes by the CURRENT adaptive pace (0 when healthy). Surfacing the wait makes
        # "why did the sync take W seconds" answerable from the log.
        self._last_write = http_retry.throttle(
            self._last_write,
            self._pace,
            on_wait=lambda w: logger.debug("plex.tv throttle: waiting {:.2f}s before next write", w),
        )

    def list_users(self) -> list[PlexTvUser]:
        """All shared + Home users with their share filters (the owner is not in this list).

        A rate-limited or timed-out READ that raised would abort the privacy sync, and the accounts
        we hadn't reached yet would keep seeing rows that aren't theirs until some later run got
        luckier (rule 6) — so it retries transient failures (429, 5xx, timeouts).
        """
        r = http_retry.get(f"{PLEXTV}/api/users", headers=self._headers(), timeout=self._timeout)
        r.raise_for_status()
        users = []
        for el in ET.fromstring(r.text):
            if el.tag != "User":
                # Only `<User>` children are accounts. Without this, any other element plex.tv adds to
                # the container (a `<Server>` block, an error node) becomes a user with `id=0` and no
                # filters — and callers that compare their own roster against this list would read that
                # as "everybody left the share" (rule 11: assume nothing about the response shape).
                continue
            home = el.get("home") == "1"
            restricted = el.get("restricted") == "1"
            users.append(
                PlexTvUser(
                    id=int(el.get("id", "0")),
                    username=el.get("username") or el.get("title") or "",
                    user_type=UserType.MANAGED if restricted else UserType.SHARED,
                    home=home,
                    restricted=restricted,
                    protected=el.get("protected") == "1",
                    avatar_url=el.get("thumb") or "",
                    filters={
                        f: el.get(f) or ""
                        for f in ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
                    },
                )
            )
        # Fold in the one field `/api/users` does not carry. Best-effort: a failure here leaves every
        # profile blank, which reads as "no preset" — so the caller ATTEMPTS the write and plex.tv gets
        # the final say (a 422 is already handled). Failing the whole roster read over an enrichment
        # would be far worse than trying a write that might be refused.
        profiles = self.home_restriction_profiles()
        if profiles:
            users = [replace(u, restriction_profile=profiles.get(u.id, "")) for u in users]
        return users

    def home_restriction_profiles(self) -> dict[int, str]:
        """``{plex_account_id: restrictionProfile}`` for every Plex Home account, "" when none is set.

        `/api/users` reports `restricted="1"` for EVERY managed account and offers nothing to tell a
        parental-controlled one from a plain one — so Shortlist skipped both, and a managed user with no
        age restriction never got the `label!=` excludes that hide other people's rows (issue #20).

        `/api/home/users` carries `restrictionProfile` ("little_kid" | "older_kid" | "teen", absent for
        none), which is exactly that distinction. It matters because Plex will not accept label
        restrictions at all while a preset is applied — "For Managed users, the restriction profile must
        be set to None if you wish to edit Rating and Label restrictions on the library types"
        (support.plex.tv/articles/204232573-restricting-the-shares/).

        Returns {} on any failure, which the caller treats as "no presets known".
        """
        if self._home_profiles is not None:
            return self._home_profiles  # profiles do not change mid-run; one read per client
        profiles: dict[int, str] = {}
        try:
            r = http_retry.get(f"{PLEXTV}/api/home/users", headers=self._headers(), timeout=self._timeout)
            r.raise_for_status()
            # Parsing is INSIDE the try. It used to sit outside, so a single non-numeric id raised out
            # of `list_users()` — which the pipeline reads as "could not read the plex.tv user list" and
            # writes no filters for anyone, promoting nothing, for the whole server. The exact opposite
            # of the best-effort behaviour this method promises (rule 11: assume nothing about a shape).
            for el in ET.fromstring(r.text):
                if el.tag != "User" or not el.get("id"):
                    continue
                try:
                    profiles[int(el.get("id", "0"))] = el.get("restrictionProfile") or ""
                except (TypeError, ValueError):
                    logger.debug("skipping a Home user with a non-numeric id: {!r}", el.get("id"))
        except Exception as e:
            logger.warning("could not read Plex Home restriction profiles ({}) — assuming none", type(e).__name__)
            return {}
        self._home_profiles = profiles
        return profiles

    def home_profile_known(self, plex_account_id: int) -> bool:
        """Do we actually know THIS account's parental profile?

        Without this, "this account has no parental profile" and "we could not find out" are the same
        value — an empty string — and a caller deciding whether a 422 is expected cannot tell them
        apart. That matters for a PERMANENT failure: this is the v1 XML surface, and if it ever goes
        away every profiled account would 422, be treated as an unknown failure, and block promotion
        for the whole server every night. Which is exactly the shape of #14.

        Answered PER ACCOUNT, not once for the whole read, because a 200 is not the same as a
        complete answer. `/api/home/users` returning an empty `<MediaContainer>`, or a roster that
        simply omits somebody, used to satisfy a global "the read succeeded" flag — so a genuinely
        profiled child read as having NO profile, their 422 looked unexpected, and promotion was
        blocked for every user on the server, every night, behind a green test suite. An account the
        roster never mentioned is unknown, whatever the HTTP status was.

        False until a read succeeds, and failures are deliberately not cached, so a blip recovers
        within the same run.
        """
        return self._home_profiles is not None and plex_account_id in self._home_profiles

    def get_user(self, plex_account_id: int) -> PlexTvUser:
        for user in self.list_users():
            if user.id == plex_account_id:
                return user
        raise LookupError(f"plex.tv account {plex_account_id} not found in users list")

    def shared_server_tokens(self) -> dict[int, str]:
        """``{plex_account_id: server-scoped accessToken}`` for every user this server is shared with.

        plex.tv mints a per-user ``accessToken`` for each shared invite (``GET
        /api/servers/{machine_id}/shared_servers``). Passed to the PMS as that user's ``X-Plex-Token``
        it reads the library AS them — including their ``viewCount``/``viewedLeafCount`` and their
        MARKS, which the playback-history API never returns (live-verified 2026-07-24: a MooHouse
        mark-as-watched absent from 11,476 history rows was present via this token). This is how
        Shortlist sees "already watched" for shared users without mounting the PMS database.

        The owner is NOT in this list (they own the server, not shared to it) — read the owner's own
        state with the admin token. Home users ARE included, so this one call covers the whole shared
        roster; only a non-shared managed sub-account needs the switch path (`canary_server_token`).

        The returned tokens are live per-user credentials (plex-safety rule 9): kept in memory for the
        run, never logged, never persisted.
        """
        r = http_retry.get(
            f"{PLEXTV}/api/servers/{self._machine_id}/shared_servers", headers=self._headers(), timeout=self._timeout
        )
        r.raise_for_status()
        tokens: dict[int, str] = {}
        for ss in ET.fromstring(r.text).iter("SharedServer"):
            account_id, access_token = ss.get("userID"), ss.get("accessToken")
            if account_id and access_token:
                tokens[int(account_id)] = access_token
        return tokens

    def update_user_filters(self, plex_account_id: int, fields: dict[str, str]) -> None:
        """PUT only the given filter fields (never rebuilds), adaptively throttled with 429 backoff.

        Fires at the current pace (0 when healthy). A 429 widens the pace via ``_slow_down`` and
        retries — the next ``_throttle`` at the loop top enforces the new, larger spacing, so repeated
        429s back off exponentially. A clean write eases the pace back toward the floor.
        """
        url = f"{PLEXTV}/api/users/{plex_account_id}"
        net_backoff = 2.0
        # What the LAST attempt actually failed with — reported if every attempt is exhausted, so the
        # final error names its real cause instead of always blaming throttling (a repeated connect
        # failure used to raise "still throttling", sending the operator to the wrong diagnosis on
        # the most privacy-sensitive write path).
        last_failure = "no attempt was made"
        for attempt in range(6):
            self._throttle()
            try:
                r = httpx.put(url, params=fields, headers=self._headers(), timeout=self._timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                # The request provably never reached plex.tv, so re-sending the SAME pre-merged filter
                # is safe (it's a full-value PUT, not a delta — rule 3's merge already happened). A
                # read timeout is deliberately NOT retried here: the write may have applied.
                logger.warning("plex.tv unreachable on filter write (attempt {}): {}", attempt + 1, type(e).__name__)
                last_failure = f"plex.tv was unreachable ({type(e).__name__})"
                time.sleep(net_backoff)
                net_backoff = min(net_backoff * 2, 30.0)
                continue
            if r.status_code in (200, 201):
                logger.debug("PUT {} {} -> {}", url, sorted(fields), r.status_code)
                self._speed_up()
                return
            if r.status_code == 429:
                self._slow_down()
                logger.warning(
                    "plex.tv 429 on filter write (attempt {}); slowing to {:.1f}s/write", attempt + 1, self._pace
                )
                last_failure = "plex.tv is rate-limiting filter writes (HTTP 429)"
                continue  # the loop-top _throttle now waits the new, larger pace before retrying
            # Carry plex.tv's own words. "HTTP 400" alone leaves the operator guessing which of
            # their accounts plex.tv won't accept a filter for, and why (issue #1). The body is
            # short XML/JSON; truncate it and redact in case it ever echoes the token back.
            detail = redact(" ".join((r.text or "").split()))[:300]
            msg = (
                f"plex.tv rejected the share-filter update for account {plex_account_id}: "
                f"HTTP {r.status_code}{f' — {detail}' if detail else ''}"
            )
            if r.status_code == 422:
                raise FilterWriteRefused(msg)
            raise RuntimeError(msg)
        raise RuntimeError(
            f"plex.tv filter update for {plex_account_id} failed after {attempt + 1} attempts: {last_failure}"
        )

    def home_users(self) -> list[dict]:
        r = http_retry.get(f"{PLEXTV}/api/v2/home/users", headers=self._headers(json=True), timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("users", data if isinstance(data, list) else [])

    def canary_server_token(self, plex_account_id: int) -> str:
        """Mint a server-scoped access token for a (non-PIN) Home user — lets a test view the server
        as that user to confirm each account's Home shows only its own rows.

        Switch to the Home user, then exchange the plex.tv token for this server's
        ``accessToken`` via the resources listing (the switch token alone 401s on the PMS).
        """
        me = next((u for u in self.home_users() if int(u.get("id", 0)) == plex_account_id), None)
        if me is None:
            raise LookupError(f"account {plex_account_id} is not a Home user — cannot borrow their server token")
        if me.get("protected"):
            raise PermissionError(f"Home user {me.get('title')} is PIN-protected — cannot switch automatically")
        r = http_retry.request(
            "POST",
            f"{PLEXTV}/api/v2/home/users/{me['uuid']}/switch",
            headers=self._headers(json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        switch_token = r.json()["authToken"]
        r = http_retry.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1",
            headers=self._headers(token=switch_token, json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        resource = next((x for x in r.json() if x.get("clientIdentifier") == self._machine_id), None)
        if resource is None or not resource.get("accessToken"):
            raise LookupError(f"no server access token for canary {plex_account_id} on {self._machine_id}")
        return resource["accessToken"]
