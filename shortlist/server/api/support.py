"""Support Mode: a read-only diagnostics surface the OWNER switches on when someone asks them to.

The audience for this module is not the person operating it. A maintainer debugging a stranger's
server can only ever read what that stranger pastes back, so every endpoint here renders its own
findings as a fixed-width ``text`` block alongside the JSON. The UI's "Copy for support" button
copies that string verbatim — which means the wire format is decided (and unit-tested) here, not in
the browser, and cannot drift between the two.

Three rules shape everything below:

* **Read-only.** Nothing in here writes to Plex, plex.tv, or the settings that drive a run. The only
  mutations are the mode's own on/off switch and the audit rows it writes.
* **Gated by a mode, not by obscurity.** `require_support_mode` refuses every tool until the owner
  turns the mode on, and it expires on its own (`_SUPPORT_HOURS`). A hidden URL is not a boundary;
  an expiring, audited mode is one, and "turn on Support Mode" is an instruction a non-technical
  person can follow over chat.
* **Degrade, never blank.** These tools are wanted precisely when something is broken, so each check
  fails on its own and reports the failure as content. A dead PMS must not 500 the page that would
  have told you the PMS is dead.
"""

from __future__ import annotations

import platform
import re
import textwrap
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy import func
from sqlalchemy import text as sa_text

import shortlist
from shortlist.engine import privacy
from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    Collection,
    CollectionUserOverride,
    Event,
    Job,
    PickRow,
    Run,
    RunUser,
    User,
    WatchedTitle,
    WatchSyncState,
)
from shortlist.server.services.redaction import known_identifiers, redact_all
from shortlist.server.settings_store import SettingsStore

#: How long a single "turn it on" lasts. Long enough to survive a slow chat exchange across
#: timezones, short enough that nobody leaves the surface open for ever after one bug report.
_SUPPORT_HOURS = 24

#: The settings key holding the expiry.
#:
#: It lives in `settings_store.DEFAULTS` for its default, and in `PRIVATE_KEYS` so it is NOT writable
#: through `PUT /api/settings`. That second half is the load-bearing one: while it was an ordinary
#: settings key, writing a far-future timestamp switched every tool on with no `support.enable` event
#: and no 24h lapse — which is both halves of the boundary this mode is supposed to be.
ENABLED_UNTIL_KEY = "support.enabled_until"

#: A short PMS timeout: this is a page, not a run. Past a few seconds the tab reads as broken and
#: the person retries, which is the worst thing to do to an already-slow server.
_PROBE_TIMEOUT_S = 8

#: Copy blocks are pasted into Discord, Reddit and GitHub, all of which mangle or truncate wide
#: text. Everything rendered here stays inside this many columns.
_WIDTH = 76

#: Row cap on a substring search. A one-character query against a deep watched cache would otherwise
#: materialise the whole table — on the page whose premise is that the server may already be
#: struggling. Reaching it is reported, never silent.
_MATCH_CAP = 2000

#: How many WARNING+ log lines ride along in a report. Enough to show a repeating failure, few enough
#: that the whole thing still pastes into a chat window. The full log zip is a separate download.
_ERROR_LINES = 40

#: The label prefix every Shortlist exclusion carries, lowercased. Plex title-cases new labels, so
#: comparisons are always case-insensitive.
_LABEL_PREFIX = "shortlist_"


def _per_person_excludes(row: dict) -> list[str]:
    """The Shortlist excludes on one account that "leave their sharing alone" would actually remove.

    A restricted shared row's `shortlist__shared_*` exclude is deliberately kept, so it is not
    evidence that a removal is owed. `_is_ours` matches on `shortlist_`, which a shared label also
    starts with — the same collision that made the writer strip them in the first place.
    """
    shared = SHARED_LABEL_PREFIX.lower()
    return [v for v in row["shortlist_excludes"] if not unquote(v).lower().startswith(shared)]


def _is_ours(value: str) -> bool:
    """Is this filter value one of Shortlist's own labels?

    Matched URL-DECODED, the same way `privacy` matches them: plex.tv stores whatever encoding the
    last writer used, so the same label reaches us written more than one way. Comparing raw bytes
    here would report a label this account already excludes as missing — the opposite of the one
    question this report exists to answer.
    """
    return unquote(value).lower().startswith(_LABEL_PREFIX)


# --------------------------------------------------------------------------------------------
# mode
# --------------------------------------------------------------------------------------------


def _expiry(session) -> datetime | None:
    """When support mode lapses, or None if it is off. A malformed stored value reads as off."""
    raw = SettingsStore(session).get(ENABLED_UNTIL_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("support mode: unparseable expiry {!r} — treating as off", raw)
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when if when > datetime.now(UTC) else None


async def require_support_mode(request: Request) -> None:
    """Refuse a tool unless the owner has support mode switched on and unexpired.

    Deliberately a separate gate from `require_owner` rather than folded into it. The owner is
    already authenticated everywhere else in the app; this gate exists so the diagnostics surface is
    off by default even for them, and so switching it on is a deliberate, audited, self-reversing
    act rather than a permanent extra attack surface on every install.
    """
    with request.app.state.sessions() as session:
        if _expiry(session) is None:
            raise HTTPException(status_code=403, detail="Support mode is off. Turn it on to use the diagnostic tools.")
        # Set on the GATE, so every tool inherits it and none can forget. A tool added later gets the
        # literal redaction for free, which is the only way this stays true.
        #
        # `async def` is load-bearing: FastAPI runs a SYNC dependency in a threadpool, which copies
        # the context — so the value set there never reaches the endpoint, and every literal silently
        # went unredacted. Async runs it in the request's own task.
        _KNOWN.set(known_identifiers(session))


#: Owner-gated but NOT support-gated: the three endpoints that report and flip the mode itself.
#: Folding these behind `require_support_mode` would make the mode impossible to turn on.
_mode = APIRouter(dependencies=[Depends(require_owner)])
#: Owner-gated AND support-gated. Every diagnostic tool hangs off this one. As in `system.py`, the
#: exported `router` is assembled at the BOTTOM of the module, so a handler written against a
#: not-yet-existing `router` fails at import rather than shipping ungated.
_tool = APIRouter(dependencies=[Depends(require_owner), Depends(require_support_mode)])


class StatusOut(PassthroughModel):
    enabled: bool
    expires_at: str | None
    seconds_remaining: int


def _status_payload(session) -> dict:
    when = _expiry(session)
    if when is None:
        return {"enabled": False, "expires_at": None, "seconds_remaining": 0}
    return {
        "enabled": True,
        "expires_at": when.isoformat(),
        "seconds_remaining": int((when - datetime.now(UTC)).total_seconds()),
    }


@_mode.get("/support/status", response_model=StatusOut)
async def status(request: Request) -> dict:
    """Whether the diagnostic tools are currently usable, and for how much longer."""
    with request.app.state.sessions() as session:
        return _status_payload(session)


@_mode.post("/support/enable", response_model=StatusOut)
async def enable(request: Request) -> dict:
    """Switch support mode on for `_SUPPORT_HOURS`. Re-enabling extends from now, it does not stack."""
    until = datetime.now(UTC) + timedelta(hours=_SUPPORT_HOURS)
    with request.app.state.sessions() as session:
        SettingsStore(session).set(ENABLED_UNTIL_KEY, until.isoformat())
        session.add(Event(scope="support.enable", level="warning", message={"until": until.isoformat()}))
        session.commit()
        return _status_payload(session)


@_mode.post("/support/disable", response_model=StatusOut)
async def disable(request: Request) -> dict:
    """Switch support mode off immediately, without waiting for the expiry."""
    with request.app.state.sessions() as session:
        SettingsStore(session).set(ENABLED_UNTIL_KEY, "")
        session.add(Event(scope="support.disable", level="info", message={"at": datetime.now(UTC).isoformat()}))
        session.commit()
        return _status_payload(session)


def _audit(session, tool: str, detail: dict[str, Any]) -> None:
    """Record that a diagnostic ran. Support mode is a privileged surface, so "who looked at what,
    when" stays answerable the same way every write path is. Volume is bounded by the mode's own
    expiry, and the events table already has retention."""
    session.add(Event(scope="support.read", level="info", message={"tool": tool, **detail}))
    session.commit()


# --------------------------------------------------------------------------------------------
# plain-text rendering
# --------------------------------------------------------------------------------------------


#: Anything shaped like a credential in a URL, a header line or a dict repr. Covers the `=`, `:` and
#: quoted forms, because an exception message may carry any of them:
#:     ?X-Plex-Token=abc      X-Plex-Token: abc      {'X-Plex-Token': 'abc'}
_SECRET_PATTERN = re.compile(
    r"((?:X-Plex-Token|token|apikey|api[-_]?key|key|secret)['\"]?\s*[:=]\s*['\"]?)[^&\s'\",;}\]]+",
    re.IGNORECASE,
)


def _scrub(s: str) -> str:
    """Strip anything credential-shaped out of a string before it reaches a client.

    Belt and braces for rule 9. No tool here renders a token deliberately — plexapi and PlexTvClient
    both send it as a header — but these responses quote EXCEPTION MESSAGES, and an HTTP client's
    error carries the URL and sometimes the headers it called with. One library that puts a
    credential in a query string, now or later, would leak it into a public GitHub issue.

    Applied in TWO places, which is the point: `_Block` (so every copy block is covered) and `_fail`
    (so every error string that reaches the JSON is too). The JSON matters as much as the block —
    `issue.tsx` prints `checks[].detail` and `error` on screen verbatim.
    """
    # `redact_all` owns the literals-then-patterns order; see its docstring for why it is not the
    # other way round.
    return redact_all(_SECRET_PATTERN.sub(r"\1<redacted>", s), _KNOWN.get())


#: Setting keys whose VALUE is a network location. Reported as a shape, never verbatim: a report is
#: destined for a public issue tracker, and a bare `http://172.16.10.240:32400` hands over someone's
#: LAN topology — while a `plex.direct` hostname embeds their server's machine id. The scheme and port
#: are the only parts with diagnostic value ("is it https", "is it the standard port").
_LOCATION_KEYS = {
    "plex.url",
    "tautulli.url",
    "requests.radarr.url",
    "requests.sonarr.url",
    "curator.ollama_url",
    "curator.openai_base_url",
}


def _location_shape(value: str) -> str:
    """`http://172.16.10.240:32400` -> `http://<host>:32400`. Empty stays empty."""
    from urllib.parse import urlsplit

    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return "<set>"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://<host>{port}"
    except ValueError:
        return "<set>"


#: Identifiers this instance is KNOWN to have, set per request from the database.
#:
#: Pattern-matching alone has now missed the same machine id three times — `\b` fails after the `F`
#: of a `%2F` escape, so a URL-encoded `uri=server%3A%2F%2F<id>%2F…` in a log line sailed through. The
#: exact values are not a guess, so they are redacted as literals and the patterns are only the net
#: for ids belonging to something else. A ContextVar rather than a global: two reports can be building
#: at once, and one must never scrub with the other's values.
_KNOWN: ContextVar[Mapping[str, str]] = ContextVar("_KNOWN", default=MappingProxyType({}))


def _fail(e: BaseException) -> str:
    """One exception-to-string conversion for the whole module, scrubbed.

    Every `except` here reports its failure as CONTENT rather than propagating it, so this is the
    single choke point every such string passes through. Formatting them inline is what let the JSON
    error fields bypass the scrubber that the copy blocks had.
    """
    return _scrub(f"{type(e).__name__}: {e}")


class _Block:
    """Accumulates the fixed-width report a tool hands to "Copy for support".

    Plain text rather than markdown on purpose: the destination is a Discord message or a Reddit
    comment, both of which mangle markdown tables and neither of which the reporter will think to
    wrap in a code fence.
    """

    def __init__(self, title: str) -> None:
        self._lines: list[str] = [f"=== Shortlist support · {title} ===".ljust(0)[:_WIDTH]]

    def kv(self, key: str, value: object) -> _Block:
        """A labelled value. Wrapped under its own label rather than cut — a truncated value reads
        as a shorter fact, not as a missing one (a caught case ended a URL at "for url 'http")."""
        return self.line(f"{key:<14}{_scrub(str(value))}")

    def line(self, s: str = "") -> _Block:
        """A prose line, WRAPPED at the width rather than truncated.

        Table cells are cut to keep columns aligned, but a sentence must never be: a caught
        truncation read "…it has not appl", which is worse than useless in the one place a
        maintainer is relying on the words. Continuations keep the original indent so an indented
        detail line still reads as subordinate.
        """
        s = _scrub(s)
        if len(s) <= _WIDTH:
            self._lines.append(s)
            return self
        indent = " " * (len(s) - len(s.lstrip()))
        self._lines.extend(textwrap.wrap(s, width=_WIDTH, subsequent_indent=indent, break_long_words=False) or [""])
        return self

    def rule(self) -> _Block:
        self._lines.append("-" * 46)
        return self

    def table(self, headers: list[str], rows: list[list[str]], widths: list[int]) -> _Block:
        """A space-aligned table. Values longer than their column are truncated rather than wrapped —
        a ragged column breaks the alignment that makes these readable at a glance in chat."""
        self._lines.append("".join(h[:w].ljust(w + 2) for h, w in zip(headers, widths, strict=True))[:_WIDTH])
        for row in rows:
            self._lines.append(
                "".join(_scrub(str(c))[:w].ljust(w + 2) for c, w in zip(row, widths, strict=True))[:_WIDTH].rstrip()
            )
        return self

    def render(self) -> str:
        return "\n".join([*self._lines, "=== end ==="])


def _stamp(block: _Block) -> _Block:
    """The provenance every block carries. Read without the screen that produced it, a block is
    useless unless it says which build, which database and when — so this is not optional."""
    return block.kv("version", shortlist.__version__).kv("generated", datetime.now(UTC).isoformat(timespec="seconds"))


# --------------------------------------------------------------------------------------------
# shared lookups
# --------------------------------------------------------------------------------------------


def _plex_client(store: SettingsStore):
    """A short-timeout PlexClient, or None when Plex isn't connected. Never raises: a caller here is
    always a diagnostic that has to report the failure rather than become it."""
    from shortlist.engine.clients.plex_pms import PlexClient

    url, token = store.get("plex.url"), store.get("plex.token")
    if not url or not token:
        return None
    return PlexClient(url, token, timeout=_PROBE_TIMEOUT_S)


def _existing_row_labels(store: SettingsStore) -> tuple[set[str], str | None]:
    """Lowercased labels of the PER-PERSON rows that exist on Plex right now, plus why not if unread.

    The engine hides a row by excluding the label it found on the PMS (`privacy.desired_excludes`
    works off `stored_labels`), so "which labels belong in everyone's filter" is a question only the
    server can answer — the user table cannot, because a user with no row yet has no label anywhere.

    Shared rows are left out. They are public (or audience-scoped) by design and are deliberately NOT
    excluded from everyone, so counting them here would report every account as leaking one.

    Returns ``(labels, error)``. An error means UNKNOWN, never "none": a read that failed must not be
    reported as a clean bill of health, which is the same fail-safe the engine applies to this
    enumeration (see `collections_known` in `pipeline._privacy_sync_phase`).

    "No labels came back" is checked against the title MARKER, not just against exceptions. A read
    that succeeds and returns nothing looks identical to a server with no rows — and on a server whose
    rows have LOST their labels, which is the state this whole area exists to defend against, those
    rows are visible to everyone. Printing "there is nothing for anyone to hide" there would be the
    most reassuring possible lie. The marker is independent of the label, so the two disagreeing says
    which of the two is true.
    """
    try:
        plex = _plex_client(store)
        if plex is None:
            return set(), "Plex isn't connected."
        owned = plex.owned_collections(LABEL_PREFIX)
        shared_prefix = SHARED_LABEL_PREFIX.lower()
        labels = {row.label.lower() for row in owned.values() if not row.label.lower().startswith(shared_prefix)}
        if not labels:
            # Same client, so the collection list is already cached — this costs no extra listing read.
            marked = sum(1 for row in plex.owned_row_surfaces(flags=False) if row.get("marked"))
            if marked:
                return set(), f"{marked} collection(s) are ours by title but carry no label"
    except Exception as e:
        # Building the client is inside the guard too. It is documented never to raise, but this is
        # the fail-safe path for the tool that reports leaks — one that raises here would take out
        # the whole section instead of reporting "unknown".
        return set(), _fail(e)
    return labels, None


def _counts_as_watched(
    viewed: int | None, total: int | None, media_type: str, show_pct: float, *, cap: float, rewatch: bool = False
) -> bool:
    """Whether the row this title was delivered into treats it as already-watched.

    Delegates to the ENGINE's own predicates rather than restating them. But it must delegate to the
    RIGHT one, and since 1.2 there are two, split by the cap:

    * cap 0 (and not a rewatch row) — `zero_pct_exclusions`: anything TOUCHED, started included;
    * cap above 0 — `_watched_titles`: only FINISHED, because a percentage of the row needs a
      definite line to mean anything.

    Answering with the finished rule at cap 0 was the pre-1.2 answer, and got the diagnosis exactly
    backwards: someone reports "I'm two episodes into Teacup and it's still in my row", the tool says
    `counts: no` at `cap 0%`, and a maintainer reads that as the bug 1.2 just fixed — instead of the
    real cause, which is that the row has not rebuilt since. It is also the only way to check the 1.2
    change took effect for a person, so it reporting the old rule made it useless for that too.
    """
    from shortlist.engine.models import MediaType
    from shortlist.engine.rows import _started_shows, _watched_titles

    if media_type == "movie":
        return True  # a watched movie is finished by definition — there is no fraction to apply
    shows = {1: (viewed or 0, total)}
    finished = _watched_titles(set(), shows, show_pct)
    if cap == 0 and not rewatch:
        finished = finished | _started_shows(shows)
    return (1, MediaType.SHOW) in finished


def _show_pct(store: SettingsStore) -> float:
    """The finished-show fraction the engine will use.

    Read from settings if a key ever appears, else the engine dataclass default. Today there is no
    such settings key — `EngineConfig.watched_show_pct` is hardcoded — and this indirection is what
    keeps the tool honest the day that changes.
    """
    from shortlist.engine.models import EngineConfig

    value = store.get("recommendations.watched_show_pct")
    return float(value) if isinstance(value, int | float) and value else EngineConfig.watched_show_pct


def _effective_cap(collection: Collection, global_pct: float) -> tuple[float, str]:
    """This row's already-watched cap and where the winning value came from."""
    if collection.watched_pct is not None:
        return float(collection.watched_pct), "row"
    return float(global_pct), "global"


# --------------------------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------------------------


def _check(name: str, fn) -> dict:
    """Run one health probe so that its failure is CONTENT, not an exception.

    Every cell is independent for a reason: the whole point of this page is to load when something
    is broken, and a single unreachable PMS must not take the panel that would have said so with it.
    """
    try:
        ok, detail = fn()
        return {"name": name, "ok": bool(ok), "detail": str(detail)}
    except Exception as e:
        logger.debug("support health probe {} failed: {}", name, e)
        return {"name": name, "ok": False, "detail": _fail(e)}


@_tool.get("/support/health")
async def health(request: Request) -> dict:
    """The overview strip: is each moving part working, answered independently."""
    state = request.app.state
    checks: list[dict] = []

    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        # ONE handshake for the whole strip. `PlexClient` connects in its constructor, so building a
        # client per probe meant two handshakes here and roughly seven across the bundle — against a
        # server the page may already be reporting as slow. Timed around the CONSTRUCTION, because
        # that is the round trip; reading `_server.version` afterwards is a cached attribute and
        # always measured ~0ms, so the strip claimed "0ms" on a PMS that took the full 8s timeout.
        started = datetime.now(UTC)
        plex, plex_error = None, None
        try:
            plex = _plex_client(store)
        except Exception as e:
            plex_error = _fail(e)
        elapsed_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

        checks.append(_check("Plex server", lambda: _probe_plex(plex, plex_error, elapsed_ms)))
        checks.append(_check("Libraries", lambda: _probe_libraries(plex, plex_error)))
        checks.append(_check("Share tokens", lambda: _probe_tokens(session)))
        checks.append(
            _check(
                "TMDB",
                lambda: (bool(store.get("tmdb.apikey")), "configured" if store.get("tmdb.apikey") else "not set"),
            )
        )
        checks.append(_check("AI curator", lambda: _probe_curator(store)))
        checks.append(_check("Database", lambda: _probe_db(session)))
        checks.append(_check("Clocks", _probe_clocks))
        checks.append(_check("Last run", lambda: _probe_last_run(session)))
        _audit(session, "health", {})

    block = _stamp(_Block("health"))
    block.rule()
    for c in checks:
        block.line(f"{'OK ' if c['ok'] else 'BAD'}  {c['name']:<16}{c['detail']}")
    return {"checks": checks, "text": block.render()}


def _probe_plex(client, error: str | None, elapsed_ms: int) -> tuple[bool, str]:
    """Version and the time the connection actually took. `elapsed_ms` is measured by the caller
    around the client CONSTRUCTION, which is where the round trip happens."""
    if error is not None:
        return False, f"unreachable after {elapsed_ms}ms — {error}"
    if client is None:
        return False, "not connected"
    version = getattr(client._server, "version", "?")
    return True, f"{version} · {elapsed_ms}ms"


def _probe_libraries(client, error: str | None) -> tuple[bool, str]:
    if error is not None or client is None:
        return False, "not connected" if error is None else "server unreachable"
    sections = list(client.sections())
    return bool(sections), f"{len(sections)} readable"


def _probe_tokens(session) -> tuple[bool, str]:
    """How many enabled people have a usable watch read.

    Judged from the CACHE rather than by minting a live token per person: this runs on page load,
    and a live probe for every share would be a burst of plex.tv traffic every time someone opens
    the page. A person with no `watch_sync_state` row at all has never been read successfully.
    """
    enabled = session.query(User).filter(User.enabled.is_(True)).all()
    if not enabled:
        return True, "no enabled users"
    synced = {row.user_id for row in session.query(WatchSyncState).all()}
    good = sum(1 for u in enabled if u.id in synced)
    return good == len(enabled), f"{good} of {len(enabled)} reading"


def _probe_curator(store: SettingsStore) -> tuple[bool, str]:
    provider = store.get("curator.provider") or "none"
    return provider != "none", str(provider)


def _probe_db(session) -> tuple[bool, str]:
    head = session.execute(sa_text("select version_num from alembic_version")).scalar()
    return bool(head), f"head {head}"


def _probe_clocks() -> tuple[bool, str]:
    """Local vs UTC, spelled out.

    Worth a cell of its own because every timestamp in the database is UTC while every line in the
    log is local — reading one as the other inverts the order of events, which has cost this project
    real debugging time before.
    """
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timedelta(0)
    hours = offset.total_seconds() / 3600
    return True, f"local UTC{hours:+g} · db stores UTC"


def _probe_last_run(session) -> tuple[bool, str]:
    last = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
    if last is None:
        return False, "never run"
    return (
        last.status == "ok",
        f"#{last.id} {last.status} at {last.finished_at:%d %b %H:%M}"
        if last.finished_at
        else f"#{last.id} {last.status}",
    )


# --------------------------------------------------------------------------------------------
# title lookup
# --------------------------------------------------------------------------------------------


@_tool.get("/support/title")
async def title_lookup(request: Request, q: str = Query(min_length=1, max_length=200)) -> dict:
    """Where one title stands for every person: watched record, episodes, whether it counts, delivery.

    The tool this whole surface was built for. A run log records how MANY titles a watch read
    returned and never WHICH, so "is this in their watched set" has been unanswerable from the
    outside — this is that missing column.
    """
    needle = q.strip()
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        show_pct = _show_pct(store)
        global_pct = float(store.get("recommendations.watched_pct") or 0.0)

        # Capped, and the cap is reported. A one-character query against a deep watched cache would
        # otherwise materialise hundreds of thousands of ORM objects, on the page whose whole premise
        # is that the server may already be struggling.
        watched = session.query(WatchedTitle).filter(WatchedTitle.title.ilike(f"%{needle}%")).limit(_MATCH_CAP).all()
        picks = session.query(PickRow).filter(PickRow.title.ilike(f"%{needle}%")).limit(_MATCH_CAP).all()
        capped = len(watched) >= _MATCH_CAP or len(picks) >= _MATCH_CAP
        users = {u.id: u for u in session.query(User).all()}
        collections = {c.slug: c for c in session.query(Collection).all()}

        # Keyed per PERSON AND TITLE, never per person alone. A substring query matches whole
        # franchises ("Doctor Who", "Star Wars", "The Office"), and keying on the user kept only the
        # last watched record, then judged it against an unrelated delivered pick. That reported the
        # exact bug this tool was built for — delivered on a 0% row, never watched — as a green "all
        # clear", because some other title in the franchise happened to be finished.
        #
        # Identity is (tmdb_id, media_type), never the bare id: movie 1399 and show 1399 are
        # different titles.
        #
        # `WatchedTitle.tmdb_id` is nullable, so a bare `or 0` would collapse every un-identified
        # watched row for one person into a single group — the same merge-two-titles bug in miniature.
        # Falling back to the title keeps them apart. (Unreachable today: the watch cache only stores
        # rows that carried a `tmdb://` guid. Cheap to be right anyway.)
        Key = tuple[int, object, str]

        def key_of(user_id: int, tmdb_id: int | None, media_type: str, title: str) -> Key:
            return (user_id, tmdb_id if tmdb_id else f"title:{title.lower()}", media_type)

        grouped: dict[Key, dict] = {}
        for w in watched:
            entry = grouped.setdefault(
                key_of(w.user_id, w.tmdb_id, w.media_type, w.title or ""), {"watched": None, "picks": []}
            )
            entry["watched"] = w
        for p in picks:
            entry = grouped.setdefault(
                key_of(p.user_id, p.tmdb_id, p.media_type, p.title or ""), {"watched": None, "picks": []}
            )
            entry["picks"].append(p)

        rows: list[dict] = []
        for (user_id, identity, media_type), found in grouped.items():
            user = users.get(user_id)
            if user is None:
                continue
            # The id is only reported when it IS one — a title fallback is not a TMDB id.
            tmdb_id = identity if isinstance(identity, int) else None
            w = found["watched"]
            delivered = found["picks"]
            # Every pick in this group is the same title by construction, so any of them names the
            # row whose cap applies.
            cap, cap_from, rewatch = global_pct, "global", False
            if delivered:
                collection = collections.get(delivered[0].collection_slug)
                if collection is not None:
                    cap, cap_from = _effective_cap(collection, global_pct)
                    rewatch = bool(collection.rewatch)
            # The row's OWN cap decides which rule applies — see `_counts_as_watched`.
            counts = bool(w) and _counts_as_watched(
                w.viewed_leaf_count, w.leaf_count, w.media_type, show_pct, cap=cap, rewatch=rewatch
            )
            rows.append(
                {
                    "user": user.slug,
                    # Named per row: with one row per title, the reader has to be able to tell which
                    # title each verdict is about.
                    "title": (w.title if w else delivered[0].title) or "",
                    "tmdb_id": tmdb_id,
                    "watched_record": bool(w),
                    "media_type": media_type,
                    "viewed_leaf_count": w.viewed_leaf_count if w else None,
                    "leaf_count": w.leaf_count if w else None,
                    "counts_as_watched": counts,
                    "cap_pct": cap,
                    "cap_source": cap_from,
                    "rewatch": rewatch,
                    "delivered": [{"row": p.collection_slug, "rank": p.rank, "library": p.library} for p in delivered],
                    # The finding, stated by the tool rather than left for the reader to infer from
                    # a table they cannot interpret.
                    "problem": bool(delivered) and not counts and cap == 0.0,
                }
            )
        rows.sort(key=lambda r: (not r["problem"], r["user"].lower(), r["title"].lower()))
        _audit(session, "title", {"q": needle, "matches": len(rows)})

    titles = {r["title"] for r in rows}
    block = _stamp(_Block("title lookup"))
    block.kv("query", f'"{needle}"')
    block.kv("matches", f"{len(rows)} row(s) · {len(titles)} title(s)")
    block.rule()
    if rows:
        # The title is a COLUMN, because a substring query can match several and a verdict without
        # its title is unreadable.
        block.table(
            ["person", "title", "watched", "eps", "counts", "cap", "in row"],
            [
                [
                    r["user"],
                    r["title"],
                    "yes" if r["watched_record"] else "none",
                    f"{r['viewed_leaf_count']}/{r['leaf_count']}" if r["watched_record"] and r["leaf_count"] else "-",
                    "yes" if r["counts_as_watched"] else "no",
                    f"{int(r['cap_pct'] * 100)}%",
                    (f"{r['delivered'][0]['row']} #{r['delivered'][0]['rank']}" if r["delivered"] else "no"),
                ]
                for r in rows
            ],
            [12, 18, 7, 6, 6, 4, 12],
        )
    else:
        block.line("No watched record and no delivery for this title, for anyone.")
    flagged = sorted({f"{r['user']} ({r['title']})" for r in rows if r["problem"]})
    if flagged:
        block.rule()
        block.line(f"PROBLEM: delivered but not counted as watched: {', '.join(flagged)}")
    if capped:
        block.rule()
        block.line(f"NOTE: only the first {_MATCH_CAP} matches were read — narrow the search.")
    return {
        "query": needle,
        "rows": rows,
        # Bare usernames, so a caller can still ask "who is affected" without parsing the labels.
        "flagged": sorted({r["user"] for r in rows if r["problem"]}),
        "flagged_detail": flagged,
        "capped": capped,
        "text": block.render(),
    }


# --------------------------------------------------------------------------------------------
# person
# --------------------------------------------------------------------------------------------


@_tool.get("/support/person/{slug}")
async def person(request: Request, slug: str) -> dict:
    """One person's watch read, library by library — the tool that separates "watches nothing" from
    "a library silently refused their token", which look identical everywhere else."""
    state = request.app.state
    with state.sessions() as session:
        user = session.query(User).filter(User.slug == slug).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail=f"No person with the username {slug!r}")

        store = SettingsStore(session, state.secrets)
        sync = session.query(WatchSyncState).filter(WatchSyncState.user_id == user.id).all()
        per_section = {s.section_key: s for s in sync}
        totals = dict(
            session.query(WatchedTitle.section_key, func.count(WatchedTitle.id))
            .filter(WatchedTitle.user_id == user.id)
            .group_by(WatchedTitle.section_key)
            .all()
        )
        movies = (
            session.query(func.count(WatchedTitle.id))
            .filter(WatchedTitle.user_id == user.id, WatchedTitle.media_type == "movie")
            .scalar()
        )
        shows = (
            session.query(func.count(WatchedTitle.id))
            .filter(WatchedTitle.user_id == user.id, WatchedTitle.media_type == "show")
            .scalar()
        )
        overrides = session.query(CollectionUserOverride).filter(CollectionUserOverride.user_id == user.id).all()

        # Enumerate from PLEX, not from this person's own rows.
        #
        # The whole point of the tool is to spot a library we have never managed to read for
        # someone — and such a library has, by definition, no watched titles and no sync state, so
        # building the list from their rows makes the very thing being looked for invisible. Live
        # check (2026-08-05): a user with a refused TV library rendered a one-row table showing only
        # the library that worked, and reported no problem.
        names: dict[str, str] = {}
        try:
            client = _plex_client(store)
            if client is not None:
                names = {str(s.key): s.title for s in client.sections()}
        except Exception as e:  # a listing failure degrades the tool, never 500s it
            logger.debug("support person: could not list sections ({})", type(e).__name__)
        # Falling back to the union means "never read" under-reports rather than lying: with no
        # section list we can only speak about libraries we have some trace of.
        section_keys = set(names) or (set(totals) | set(per_section))

        libraries: list[dict] = []
        for key in sorted(section_keys, key=lambda k: (len(k), k)):
            st = per_section.get(key)
            libraries.append(
                {
                    "section_key": key,
                    "library": names.get(key, ""),
                    "titles_known": int(totals.get(key, 0)),
                    "last_full_at": st.last_full_at.isoformat() if st and st.last_full_at else None,
                    "last_incremental_at": (
                        st.last_incremental_at.isoformat() if st and st.last_incremental_at else None
                    ),
                    # No sync row means the library has NEVER been read for this person — the
                    # signature of a refused token, not of someone who watches nothing.
                    "ever_read": st is not None,
                }
            )
        never_read = [lib["section_key"] for lib in libraries if not lib["ever_read"]]
        _audit(session, "person", {"slug": slug})

        payload = {
            "slug": user.slug,
            "display_name": user.display_name,
            "user_type": user.user_type,
            "enabled": user.enabled,
            "cold_start": user.cold_start,
            "restricted": user.restricted,
            "restriction_profile": user.restriction_profile,
            "watched_movies": int(movies or 0),
            "watched_shows": int(shows or 0),
            "libraries": libraries,
            "never_read": never_read,
            "muted_rows": [o.collection_id for o in overrides if o.muted],
        }

    block = _stamp(_Block("person"))
    block.kv("person", f"{payload['slug']} ({payload['user_type']})")
    block.kv("enabled", "yes" if payload["enabled"] else "no")
    block.kv("watched", f"{payload['watched_movies']} movies, {payload['watched_shows']} shows")
    block.rule()
    block.table(
        ["library", "titles", "last full read", "ever read"],
        [
            [
                lib["library"] or f"section {lib['section_key']}",
                str(lib["titles_known"]),
                (lib["last_full_at"] or "-")[:16].replace("T", " "),
                "yes" if lib["ever_read"] else "NEVER",
            ]
            for lib in payload["libraries"]
        ],
        [20, 8, 18, 10],
    )
    if never_read:
        block.rule()
        # Named, not keyed: the reporter has to be able to match this against what they see in Plex.
        named = ", ".join(
            lib["library"] or f"section {lib['section_key']}" for lib in payload["libraries"] if not lib["ever_read"]
        )
        # Same caution as the connection check: never-read does not prove broken. A library that is
        # not shared with someone is never read, and that is correct configuration.
        block.line(f"NEVER READ for this person: {named}")
        block.line("Expected if those are not shared with them. If they are, reads are failing —")
        block.line("'What errors has it logged?' will name them in a warning.")
    payload["text"] = block.render()
    return payload


# --------------------------------------------------------------------------------------------
# libraries
# --------------------------------------------------------------------------------------------


@_tool.get("/support/libraries")
async def libraries(request: Request) -> dict:
    """Every library with its key, type and TMDB-id coverage.

    A library whose TYPE is not what everyone assumes, or that holds titles Plex never matched, is
    invisible in a log and explains a whole class of "nothing gets recommended from here".
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        rows: list[dict] = []
        error = None
        # The construction is INSIDE the try, not before it: `PlexClient` handshakes with the server
        # in its constructor, so an unreachable PMS raises here — and the tool whose job is to report
        # that Plex cannot be read must not be the thing that 500s when Plex cannot be read.
        try:
            client = _plex_client(store)
            if client is None:
                error = "Plex isn't connected."
            else:
                for section in client.sections():
                    rows.append(
                        {
                            "key": str(section.key),
                            "title": section.title,
                            "type": section.type,
                            "items": int(getattr(section, "totalSize", 0) or 0),
                        }
                    )
        except Exception as e:  # reported as content, see the module docstring
            error = _fail(e)
        _audit(session, "libraries", {"count": len(rows)})

    block = _stamp(_Block("libraries"))
    block.rule()
    if error:
        block.line(f"COULD NOT READ: {error}")
    else:
        block.table(
            ["key", "type", "title", "items"],
            [[r["key"], r["type"], r["title"], str(r["items"])] for r in rows],
            [6, 7, 30, 8],
        )
    return {"libraries": rows, "error": error, "text": block.render()}


# --------------------------------------------------------------------------------------------
# row settings
# --------------------------------------------------------------------------------------------


@_tool.get("/support/rows")
async def rows(request: Request) -> dict:
    """Every row's effective settings, with the SOURCE of each winning value.

    "But I set it to 0%" is answerable only by showing whether the global default or a per-row
    override actually applied — the two are indistinguishable from the row editor alone.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        global_pct = float(store.get("recommendations.watched_pct") or 0.0)
        global_days = int(store.get("recommendations.refresh_days") or 0)
        out: list[dict] = []
        for c in session.query(Collection).order_by(Collection.sort_order, Collection.id).all():
            cap, cap_from = _effective_cap(c, global_pct)
            out.append(
                {
                    "slug": c.slug,
                    "name": c.name,
                    "enabled": c.enabled,
                    "media": c.media,
                    "size": c.size,
                    "watched_pct": cap,
                    "watched_pct_source": cap_from,
                    "refresh_days": c.refresh_days if c.refresh_days is not None else global_days,
                    "refresh_days_source": "row" if c.refresh_days is not None else "global",
                    "rewatch": bool(c.rewatch),
                    "unstarted_only": bool(c.unstarted_only),
                }
            )
        _audit(session, "rows", {"count": len(out)})

    block = _stamp(_Block("row settings"))
    block.kv("global cap", f"{int(global_pct * 100)}% already-watched")
    block.rule()
    block.table(
        ["row", "media", "cap", "from", "rewatch", "unstarted"],
        [
            [
                r["slug"],
                r["media"],
                f"{int(r['watched_pct'] * 100)}%",
                r["watched_pct_source"],
                "yes" if r["rewatch"] else "no",
                "yes" if r["unstarted_only"] else "no",
            ]
            for r in out
        ],
        [16, 7, 5, 7, 9, 9],
    )
    return {"rows": out, "global_watched_pct": global_pct, "text": block.render()}


# --------------------------------------------------------------------------------------------
# everything, as one download
# --------------------------------------------------------------------------------------------


@_tool.get("/support/bundle.txt", response_class=PlainTextResponse)
async def bundle(request: Request) -> str:
    """Every tool's block in one file, for when a paste would be truncated.

    Discord collapses long messages and Reddit truncates them, so past a certain size the right
    answer is an attachment rather than a paste. Deliberately over-collects: a second round trip
    with a non-technical reporter costs a day, and redundant text costs nothing.
    """
    parts: list[str] = []
    header = _stamp(_Block("full diagnostic"))
    header.kv("python", platform.python_version())
    parts.append(header.render())
    # Every server-wide tool, in the order a maintainer reads them: is it healthy, what is it
    # configured as, what is on Plex, what has been happening. The per-person tools follow, for the
    # people the checks above actually flagged — see `_people_worth_including`.
    #
    # Each is awaited INSIDE the loop so one failing tool costs its own section and not the file:
    # the bundle is most wanted when something is broken, which is exactly when a tool may raise.
    tools = {
        "health": health,
        "libraries": libraries,
        "row settings": rows,
        "row schedule": row_schedule,
        "connection": connection,
        "sharing": sharing,
        "surfaces": surfaces,
        "drift": drift,
        "jobs": jobs,
        "clocks": clocks,
        "database": database,
        "config": config,
        "settings history": settings_history,
        "timeline": timeline,
        # Last, and the two most often missing from a bug report: what actually went wrong, and which
        # people a run could not build.
        "recent runs": recent_runs,
        "recent warnings and errors": errors,
    }
    findings: dict[str, dict] = {}
    for name, tool in tools.items():
        try:
            result = await tool(request)
            if isinstance(result, dict):
                findings[name] = result
                parts.append(result["text"])
            else:
                parts.append(str(result))
        except Exception as e:
            failed = _stamp(_Block(name))
            failed.line(f"THIS SECTION FAILED: {_fail(e)}")
            parts.append(failed.render())

    # Per-person detail, for the people the checks above flagged — nobody else.
    #
    # The per-person tools need a name, and a report that asked for one would stop being a single
    # button. But "here is the server, now go and ask me about each of 46 people" is a round trip, and
    # a round trip with a non-technical reporter costs a day. So the report answers the follow-up it
    # has just invited: whoever the connection check could not read, whoever a run failed on, whoever
    # can see a row that is not theirs.
    for slug in _people_worth_including(findings):
        try:
            parts.append((await person(request, slug))["text"])
        except Exception as e:
            failed = _stamp(_Block(f"person {slug}"))
            failed.line(f"THIS SECTION FAILED: {_fail(e)}")
            parts.append(failed.render())

    return "\n\n".join(parts)


#: How many flagged people get their own section. A cap, because a server where every share token is
#: broken would otherwise append 46 of them and stop being pasteable — and by then the first three
#: have made the point.
_PEOPLE_IN_REPORT = 5


def _people_worth_including(findings: dict[str, dict]) -> list[str]:
    """Whose per-person detail belongs in the report, drawn from what the other checks flagged.

    Ordered by how likely each signal is to BE the problem: a run that failed on someone names them
    outright; a person we cannot fully read explains an empty watched set; a missing share exclusion is
    a privacy fault. De-duplicated, capped, and empty on a healthy server — which is the point, so a
    clean report stays short.
    """
    ordered: list[str] = []
    for run in findings.get("recent runs", {}).get("runs", []) or []:
        ordered.extend(str(f["user"]) for f in run.get("failed", []) or [])
    ordered.extend(str(s) for s in findings.get("connection", {}).get("problems", []) or [])
    # SLUGS, not the usernames the sharing block prints — see `sharing` for why they differ.
    ordered.extend(str(s) for s in findings.get("sharing", {}).get("missing_excludes_slugs", []) or [])
    return list(dict.fromkeys(ordered))[:_PEOPLE_IN_REPORT]


# --------------------------------------------------------------------------------------------
# row schedule
# --------------------------------------------------------------------------------------------


@_tool.get("/support/row-schedule")
async def row_schedule(request: Request) -> dict:
    """When each row last rebuilt, and when it is next due to.

    The gap this closes: the refresh cadence is exactly that — a cadence. At the 8-day default a row
    re-selects its titles about weekly and redelivers the same picks on every other night, and the
    engine logs that decision NOWHERE. So "I changed the setting and nothing happened" has been
    unanswerable — the setting was fine, the row simply had not rebuilt yet.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        global_days = int(store.get("recommendations.refresh_days") or 0)
        last_built = dict(
            session.query(PickRow.collection_slug, func.max(PickRow.created_at)).group_by(PickRow.collection_slug).all()
        )
        out: list[dict] = []
        for c in session.query(Collection).order_by(Collection.sort_order, Collection.id).all():
            # 0 is "never refresh once built" — a frozen, pinned row, not a broken one.
            period = max(0, c.refresh_days if c.refresh_days is not None else global_days)
            built = last_built.get(c.slug)
            days_since = (datetime.now(UTC) - built.replace(tzinfo=built.tzinfo or UTC)).days if built else None
            out.append(
                {
                    "slug": c.slug,
                    "enabled": c.enabled,
                    "refresh_days_source": "row" if c.refresh_days is not None else "global",
                    "rebuild_every_days": period,
                    "last_built_at": built.isoformat() if built else None,
                    "days_since_built": days_since,
                    # The actionable bit: a setting change does not reach the row until this is true.
                    "due": bool(period and days_since is not None and days_since >= period),
                    "never_built": built is None,
                }
            )
        _audit(session, "row-schedule", {"rows": len(out)})

    block = _stamp(_Block("row schedule"))
    block.rule()
    block.table(
        ["row", "rebuilds", "last built", "age", "due"],
        [
            [
                r["slug"],
                "never (frozen)" if not r["rebuild_every_days"] else f"every {r['rebuild_every_days']}d",
                (r["last_built_at"] or "never")[:10],
                "-" if r["days_since_built"] is None else f"{r['days_since_built']}d",
                "YES" if r["due"] else "no",
            ]
            for r in out
        ],
        [16, 15, 12, 6, 5],
    )
    stale = [r["slug"] for r in out if r["rebuild_every_days"] and not r["due"] and r["days_since_built"] is not None]
    if stale:
        block.rule()
        block.line("NOTE: a setting change does not affect a row until it next rebuilds.")
    return {"rows": out, "text": block.render()}


# --------------------------------------------------------------------------------------------
# jobs, clocks, database, config
# --------------------------------------------------------------------------------------------


@_tool.get("/support/jobs")
async def jobs(request: Request) -> dict:
    """The durable queue: what is stuck, what failed, and with what error."""
    with request.app.state.sessions() as session:
        recent = session.query(Job).order_by(Job.id.desc()).limit(40).all()
        by_status: dict[str, int] = {}
        for job in recent:
            by_status[job.status] = by_status.get(job.status, 0) + 1
        rows = [
            {
                "id": job.id,
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "detail": job.detail,
                # Already redacted at write time (rule 9); truncated so one stack trace cannot
                # swallow the whole paste.
                "error": (job.error or "")[:200],
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
            for job in recent
        ]
        _audit(session, "jobs", {"count": len(rows)})

    failed = [r for r in rows if r["status"] == "failed"]
    block = _stamp(_Block("jobs"))
    block.kv("counts", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "none")
    block.rule()
    if rows:
        block.table(
            ["id", "kind", "status", "tries", "detail"],
            [[str(r["id"]), r["kind"], r["status"], str(r["attempts"]), r["detail"]] for r in rows[:15]],
            [5, 20, 9, 6, 26],
        )
    else:
        # An empty table with only a header reads as a broken tool rather than as good news.
        block.line("No background work has run yet — nothing queued, nothing failed.")
    for job in failed[:5]:
        block.line(f"FAILED #{job['id']} {job['kind']}: {job['error']}")
    return {"jobs": rows, "counts": by_status, "failed": len(failed), "text": block.render()}


@_tool.get("/support/clocks")
async def clocks(request: Request) -> dict:
    """Timezones and the next scheduled fire times.

    Every timestamp in the database is UTC and every line in the log is local. Reading one as the
    other inverts the order of events, which has cost this project real debugging time — so the
    offset is stated outright rather than left to be inferred.
    """
    import os

    now_local = datetime.now().astimezone()
    offset_h = (now_local.utcoffset() or timedelta(0)).total_seconds() / 3600
    scheduler = getattr(request.app.state, "scheduler", None)
    fires: list[dict] = []
    if scheduler is not None:
        for job in scheduler.get_jobs():
            fires.append({"id": job.id, "next_run": job.next_run_time.isoformat() if job.next_run_time else None})
    with request.app.state.sessions() as session:
        _audit(session, "clocks", {})

    block = _stamp(_Block("clocks and schedule"))
    block.kv("TZ env", os.environ.get("TZ", "(unset)"))
    block.kv("local now", now_local.isoformat(timespec="seconds"))
    block.kv("utc now", datetime.now(UTC).isoformat(timespec="seconds"))
    block.kv("offset", f"UTC{offset_h:+g} — db stores UTC, logs print local")
    block.rule()
    block.table(
        ["scheduled job", "next run"],
        [[f["id"], (f["next_run"] or "never")[:19].replace("T", " ")] for f in fires],
        [26, 22],
    )
    return {
        "tz": os.environ.get("TZ", ""),
        "local_now": now_local.isoformat(),
        "utc_now": datetime.now(UTC).isoformat(),
        "offset_hours": offset_h,
        "scheduled": fires,
        "text": block.render(),
    }


@_tool.get("/support/database")
async def database(request: Request) -> dict:
    """Migration head plus proof the schema actually has what the ORM claims.

    Head alone is not health: a migration that no-ops on a real database still stamps its version,
    and this project has shipped exactly that. So the tables and indexes are counted, not assumed.
    """
    from shortlist.server.db.models import Base

    state = request.app.state
    with state.sessions() as session:
        head = session.execute(sa_text("select version_num from alembic_version")).scalar()
        present = {
            row[0] for row in session.execute(sa_text("select name from sqlite_master where type='table'")).all()
        }
        expected = set(Base.metadata.tables)
        missing = sorted(expected - present)
        indexes = session.execute(sa_text("select count(*) from sqlite_master where type='index'")).scalar()
        page_count = session.execute(sa_text("pragma page_count")).scalar() or 0
        page_size = session.execute(sa_text("pragma page_size")).scalar() or 0
        journal = session.execute(sa_text("pragma journal_mode")).scalar()
        _audit(session, "database", {"head": head})

    size_mb = (int(page_count) * int(page_size)) / (1024 * 1024)
    block = _stamp(_Block("database"))
    block.kv("head", str(head))
    block.kv("tables", f"all {len(expected)} the code needs are present" if not missing else f"{len(missing)} MISSING")
    block.kv("indexes", str(indexes))
    block.kv("size", f"{size_mb:.1f} MB (journal {journal})")
    if missing:
        block.rule()
        block.line(f"PROBLEM: tables the code expects but the database lacks: {', '.join(missing)}")
    return {
        "head": head,
        "tables_present": len(present),
        "tables_expected": len(expected),
        "missing_tables": missing,
        "indexes": int(indexes or 0),
        "size_mb": round(size_mb, 2),
        "journal_mode": journal,
        "text": block.render(),
    }


@_tool.get("/support/config")
async def config(request: Request) -> dict:
    """Where each setting's value came from: an environment variable, or the database.

    Env vars are ONE-TIME seeds — read into the database on first boot and ignored for ever after —
    which surprises people who then edit their compose file and see nothing change. This says so per
    key. Secret values are never rendered; only whether one is set.
    """
    import os

    from shortlist.server.settings_store import ENV_SEEDS, PRIVATE_KEYS, SECRET_KEYS

    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        rows: list[dict] = []
        for env_name, key in sorted(ENV_SEEDS.items()):
            in_env = env_name in os.environ
            # Secrets are reported as set/not-set and never read out (rule 9).
            stored = store.get(key) if key not in SECRET_KEYS else None
            has_value = bool(stored) if key not in SECRET_KEYS else key in _keys_with_values(session)
            rows.append(
                {
                    "env": env_name,
                    "key": key,
                    "env_set": in_env,
                    "secret": key in SECRET_KEYS,
                    "value": (
                        ""
                        if key in SECRET_KEYS or key in PRIVATE_KEYS
                        else _location_shape(str(stored or ""))
                        if key in _LOCATION_KEYS
                        else str(stored or "")
                    ),
                    "has_value": has_value,
                }
            )
        _audit(session, "config", {})

    block = _stamp(_Block("config source"))
    block.line("Env vars seed the database ONCE, then are ignored. The database wins after that.")
    block.rule()
    block.table(
        ["env var", "setting", "in env", "stored"],
        [
            [
                r["env"],
                r["key"],
                "yes" if r["env_set"] else "no",
                ("(secret set)" if r["has_value"] else "(unset)") if r["secret"] else (r["value"] or "(empty)"),
            ]
            for r in rows
        ],
        [16, 18, 7, 20],
    )
    return {"settings": rows, "text": block.render()}


def _keys_with_values(session) -> set[str]:
    """Setting keys with a non-empty stored value, read WITHOUT decrypting.

    Deliberately raw: `SettingsStore.get` would decrypt a secret to answer "is it set", and this
    module must never hold a plaintext credential even briefly.
    """
    from shortlist.server.db.models import Setting

    return {row.key for row in session.query(Setting).all() if row.value not in (None, "", '""')}


# --------------------------------------------------------------------------------------------
# explainers: why is this here / missing, the funnel, the AI's call
# --------------------------------------------------------------------------------------------


def _event_summary(event) -> str:
    """One readable line for an audited event.

    `str(message)` is a Python dict repr — `{'tool': 'config'}` — which is noise in a timeline meant
    to be skim-read by someone who has never seen this app's internals. Pulls out the keys that
    actually say what happened and falls back to the repr for a shape not seen before.
    """
    payload = event.message if isinstance(event.message, dict) else {}
    interesting = [f"{k}={payload[k]}" for k in ("user", "slug", "row", "collection", "kind") if payload.get(k)]
    detail = " ".join(interesting) or str(payload)[:48]
    return f"{event.scope} {detail}".rstrip()


def _user_or_404(session, slug: str) -> User:
    user = session.query(User).filter(User.slug == slug).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail=f"No person with the username {slug!r}")
    return user


def _latest_trace(session, user_id: int) -> tuple[int | None, dict]:
    """The most recent run trace for one person, or (None, {}) if they have never been built.

    Ordered by run id, not by date: a re-run of an older run keeps its id ordering, and two runs in
    the same second would otherwise come back in an arbitrary order.
    """
    row = session.query(RunUser).filter(RunUser.user_id == user_id).order_by(RunUser.run_id.desc()).first()
    if row is None:
        return None, {}
    return row.run_id, dict(row.trace or {})


@_tool.get("/support/pick")
async def why_here(request: Request, user: str = Query(min_length=1), title: str = Query(min_length=1)) -> dict:
    """Why a delivered title is in someone's row: the seed it came from, the source, its strength."""
    with request.app.state.sessions() as session:
        profile = _user_or_404(session, user)
        picks = (
            session.query(PickRow)
            .filter(PickRow.user_id == profile.id, PickRow.title.ilike(f"%{title.strip()}%"))
            .order_by(PickRow.created_at.desc())
            .limit(20)
            .all()
        )
        rows = [
            {
                "title": p.title,
                "row": p.collection_slug,
                "library": p.library,
                "rank": p.rank,
                "reason": p.reason,
                "sources": p.sources,
                "affinity": p.affinity,
                "seed_title": p.seed_title,
                "delivered_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in picks
        ]
        _audit(session, "pick", {"user": user, "title": title})

    block = _stamp(_Block("why is this here"))
    block.kv("person", user)
    block.kv("title", f'"{title}"')
    block.rule()
    if not rows:
        block.line("Not in any of this person's rows. Try the 'why is this missing' check instead.")
    else:
        for r in rows[:6]:
            block.line(f"{r['title']} — {r['row']} #{r['rank']} in {r['library']}")
            block.line(f"  seed: {r['seed_title'] or 'none (discover)'}")
            block.line(f"  source: {r['sources'] or 'unknown'} · strength {r['affinity']:.2f}")
            if r["reason"]:
                block.line(f"  shown as: {r['reason']}")
    return {"user": user, "title": title, "picks": rows, "text": block.render()}


@_tool.get("/support/missing")
async def why_missing(request: Request, user: str = Query(min_length=1), title: str = Query(min_length=1)) -> dict:
    """Why a title never appeared — usually the real question, and nothing else answers it.

    Walks the last run's trace for this person and reports the first stage that rejected the title:
    it was never pooled, the library does not hold it, it counted as already-watched, a genre filter
    dropped it, or it simply lost the ranking cut.
    """
    needle = title.strip().lower()
    with request.app.state.sessions() as session:
        profile = _user_or_404(session, user)
        run_id, trace = _latest_trace(session, profile.id)
        delivered = (
            session.query(PickRow).filter(PickRow.user_id == profile.id, PickRow.title.ilike(f"%{needle}%")).first()
        )
        # `returned` on each gather carries every pooled candidate with the fate selection gave it.
        hits: list[dict] = []
        for gather in trace.get("gathers", []) or []:
            for item in gather.get("returned", []) or []:
                if needle in str(item.get("title", "")).lower():
                    hits.append({"pool": gather.get("pool", ""), "source": item.get("source", ""), **item})
        _audit(session, "missing", {"user": user, "title": title})

    block = _stamp(_Block("why is this missing"))
    block.kv("person", user)
    block.kv("title", f'"{title}"')
    block.kv("from run", f"#{run_id}" if run_id else "never built for this person")
    block.rule()
    verdict: str
    if delivered is not None:
        verdict = f"It IS in their row ({delivered.collection_slug} #{delivered.rank}) — nothing to explain."
    elif run_id is None:
        verdict = "This person has never been built, so nothing was considered for them at all."
    elif not hits:
        verdict = (
            "Never even suggested: no candidate source proposed this title, so no filter rejected "
            "it. Widen the sources, or it may not be related to anything they watch."
        )
    else:
        fates = sorted({str(h.get("fate") or "unknown") for h in hits})
        verdict = f"Suggested, then dropped: {', '.join(fates)}."
    block.line(verdict)
    for hit in hits[:8]:
        block.line(f"  {hit.get('title', '')} · {hit.get('source', '?')} → {hit.get('fate', 'unknown')}")
    return {"user": user, "title": title, "run_id": run_id, "verdict": verdict, "hits": hits, "text": block.render()}


@_tool.get("/support/funnel")
async def funnel(request: Request, user: str = Query(min_length=1)) -> dict:
    """Counts at each stage from TMDB pool to delivered row — "why is my row short", answered.

    A short row has one cause per stage, and they need completely different fixes: a thin library, an
    over-tight watched cap, a genre filter, or the AI declining. Only the counts distinguish them.
    """
    with request.app.state.sessions() as session:
        profile = _user_or_404(session, user)
        run_id, trace = _latest_trace(session, profile.id)
        delivered = (
            session.query(func.count(PickRow.id))
            .filter(PickRow.user_id == profile.id, PickRow.run_id == run_id)
            .scalar()
            if run_id
            else 0
        )
        stages: list[dict] = []
        for gather in trace.get("gathers", []) or []:
            disposition = gather.get("disposition", {}) or {}
            pooled = int(gather.get("pooled", 0) or 0) or sum(int(v) for v in disposition.values())
            stages.append(
                {
                    "pool": gather.get("pool", ""),
                    "pooled": pooled,
                    # Every fate selection recorded, so a stage that ate the row names itself rather
                    # than being inferred from a drop between two totals.
                    "disposition": {str(k): int(v) for k, v in disposition.items()},
                }
            )
        _audit(session, "funnel", {"user": user})

    block = _stamp(_Block("candidate funnel"))
    block.kv("person", user)
    block.kv("from run", f"#{run_id}" if run_id else "never built")
    block.kv("delivered", str(delivered))
    block.rule()
    if not stages:
        block.line("No candidate trace recorded for this person's last run.")
    for stage in stages:
        block.line(f"pool {stage['pool'] or '(default)'} — {stage['pooled']} pooled")
        for fate, count in sorted(stage["disposition"].items(), key=lambda kv: -kv[1]):
            block.line(f"    {fate:<22}{count}")
    return {"user": user, "run_id": run_id, "delivered": int(delivered or 0), "stages": stages, "text": block.render()}


@_tool.get("/support/ai")
async def ai_decisions(request: Request, user: str = Query(min_length=1)) -> dict:
    """What the AI curator actually did for this person: tokens spent per step, and any error."""
    with request.app.state.sessions() as session:
        profile = _user_or_404(session, user)
        row = session.query(RunUser).filter(RunUser.user_id == profile.id).order_by(RunUser.run_id.desc()).first()
        store = SettingsStore(session, request.app.state.secrets)
        provider = str(store.get("curator.provider") or "none")
        model = str(store.get("curator.model") or "")
        payload = {
            "user": user,
            "provider": provider,
            "model": model,
            "run_id": row.run_id if row else None,
            "llm_tokens": row.llm_tokens if row else 0,
            "by_step": dict(row.llm_tokens_by_step or {}) if row else {},
            "exa_searches": row.exa_searches if row else 0,
            "status": row.status if row else "",
            "error": (row.error or "")[:300] if row else "",
        }
        _audit(session, "ai", {"user": user})

    block = _stamp(_Block("AI decisions"))
    block.kv("person", user)
    block.kv("provider", f"{provider} {model}".strip())
    block.kv("from run", f"#{payload['run_id']}" if payload["run_id"] else "never built")
    block.kv("tokens", str(payload["llm_tokens"]))
    block.rule()
    for step, tokens in sorted(payload["by_step"].items(), key=lambda kv: -kv[1]):
        block.line(f"  {step:<26}{tokens}")
    if provider == "none":
        block.line("No AI curator configured — picks are chosen by ranking alone.")
    if payload["error"]:
        block.line(f"ERROR: {payload['error']}")
    payload["text"] = block.render()
    return payload


# --------------------------------------------------------------------------------------------
# history: timeline, settings changes
# --------------------------------------------------------------------------------------------


@_tool.get("/support/timeline")
async def timeline(request: Request, user: str = "") -> dict:
    """Runs, jobs and audited events on one axis, newest first.

    Rendered in BOTH local and UTC. The database stores UTC and the log prints local, so a timeline
    that picked one would be misread against the other — which is precisely the mistake this is here
    to stop someone making.

    ``user`` is a PLAIN default, not ``Query(default="")``. `bundle` calls these handlers directly,
    which bypasses FastAPI's dependency injection — so a `Query(...)` default arrives as a
    `fastapi.params.Query` OBJECT, which is truthy and then blows up on `in`. That silently cost the
    downloaded diagnostic its whole runs/jobs/events section on every install. FastAPI still parses
    `?user=` from the query string for a plain str parameter, so nothing is lost by dropping it.
    """
    with request.app.state.sessions() as session:
        entries: list[dict] = []
        for run in session.query(Run).order_by(Run.id.desc()).limit(15).all():
            entries.append(
                {
                    "at": run.started_at,
                    "kind": "run",
                    "what": f"run #{run.id} {run.status}" + (" (dry-run)" if run.dry_run else ""),
                }
            )
        for job in session.query(Job).order_by(Job.id.desc()).limit(15).all():
            entries.append({"at": job.created_at, "kind": "job", "what": f"{job.kind} {job.status}"})
        # `support.read` EXCLUDED. Every check writes one, so within a single support session they
        # outnumber everything else and the tool that answers "what has been happening" showed
        # nothing but the fact that someone had been looking. The audit rows still exist in `events`
        # — they are just not what this question is asking. `support.enable`/`disable` stay: those
        # are state changes, and rare.
        events = session.query(Event).filter(Event.scope != "support.read").order_by(Event.id.desc()).limit(40).all()
        for event in events:
            text = _event_summary(event)
            if user and user not in text and user not in event.scope:
                continue
            entries.append({"at": event.ts, "kind": "event", "what": text})
        _audit(session, "timeline", {"user": user})

    entries = [e for e in entries if e["at"] is not None]
    entries.sort(key=lambda e: e["at"], reverse=True)
    entries = entries[:40]

    block = _stamp(_Block("timeline"))
    if user:
        block.kv("filtered to", user)
    block.line("times shown LOCAL (db stores UTC)")
    block.rule()
    for entry in entries:
        stamp = entry["at"]
        local = stamp.astimezone() if stamp.tzinfo else stamp.replace(tzinfo=UTC).astimezone()
        block.line(f"{local:%d %b %H:%M}  {entry['kind']:<6}{entry['what']}")
    return {
        "entries": [
            {
                "at_utc": e["at"].isoformat(),
                "at_local": (
                    e["at"].astimezone() if e["at"].tzinfo else e["at"].replace(tzinfo=UTC).astimezone()
                ).isoformat(),
                "kind": e["kind"],
                "what": e["what"],
            }
            for e in entries
        ],
        "text": block.render(),
    }


@_tool.get("/support/settings-history")
async def settings_history(request: Request) -> dict:
    """Settings changes, newest first — turning "but I set it to 0%" into a timestamp.

    Paired with the row schedule: a change made after a row's last rebuild has not reached that row
    yet, which is the commonest reason a correct setting appears to do nothing.
    """
    with request.app.state.sessions() as session:
        events = session.query(Event).filter(Event.scope.like("settings%")).order_by(Event.id.desc()).limit(30).all()
        rows = [
            {"at": e.ts.isoformat() if e.ts else None, "scope": e.scope, "change": str(e.message)[:160]} for e in events
        ]
        last_built = session.query(func.max(PickRow.created_at)).scalar()
        _audit(session, "settings-history", {"count": len(rows)})

    # A flag, not just a sentence in the block: the page needs to render this as a warning, and
    # asserting on prose breaks the moment the line wraps (it did).
    pending = bool(rows and last_built and rows[0]["at"] and rows[0]["at"] > last_built.isoformat())

    block = _stamp(_Block("settings history"))
    block.kv("last build", last_built.isoformat()[:19] if last_built else "never")
    block.rule()
    if not rows:
        block.line("No settings changes recorded. (Only changes made since this version are kept.)")
    for row in rows[:12]:
        block.line(f"{(row['at'] or '')[:16].replace('T', ' ')}  {row['change']}")
    if pending:
        block.rule()
        block.line("NOTE: the newest change is newer than the last build, so it has not applied yet.")
    return {
        "changes": rows,
        "last_build_at": last_built.isoformat() if last_built else None,
        "change_after_last_build": pending,
        "text": block.render(),
    }


# --------------------------------------------------------------------------------------------
# live Plex / plex.tv
# --------------------------------------------------------------------------------------------

#: What "read as a user" is allowed to fetch, as a fixed menu.
#:
#: An allowlist and never a free URL field. This container sits on someone's home network, so an
#: arbitrary-URL fetcher behind owner auth is a port scanner with extra steps — the same mistake as a
#: raw SQL console, wearing a better disguise. Every entry is a GET, and the `{key}` placeholder is
#: filled only from a section key the PMS itself just reported.
_READ_AS_ENDPOINTS: dict[str, tuple[str, dict[str, object]]] = {
    "libraries": ("/library/sections", {}),
    "watched-movies": ("/library/sections/{key}/all", {"type": 1, "unwatched": 0, "includeGuids": 1}),
    "watched-shows": ("/library/sections/{key}/all", {"type": 2, "unwatched": 0, "includeGuids": 1}),
    "home-rows": ("/hubs", {}),
}

#: Filesystem locations in a PMS response, which `read-as` echoes verbatim behind a Copy button.
#:
#: `/library/sections` returns a `<Location path="/data_16tb/Movies">` per library and an item listing
#: returns `<Part file="…">` per file, so a paste hands over the owner's storage layout — and a layout
#: is routinely named after a person (`/Users/johnsmith/Media`, `/home/dave/tv`). None of it answers
#: what this tool asks, which is whose token can read which library.
_MEDIA_PATH_ATTR = re.compile(r"\b(path|file)=\"[^\"]*\"", re.IGNORECASE)


def _plextv_client(store: SettingsStore, machine_id: str):
    """A plex.tv client, or None when Plex isn't linked. Never raises, for the usual reason."""
    from shortlist.engine.clients.plextv import PlexTvClient

    token = store.get("plex.token")
    if not token or not machine_id:
        return None
    return PlexTvClient(str(token), machine_id)


def _machine_id(session) -> str:
    from shortlist.server.db.models import Server

    server = session.query(Server).first()
    return server.machine_id if server else ""


@_tool.get("/support/connection")
async def connection(request: Request) -> dict:
    """Per person: do we hold a working share token, and which libraries has it ever read?

    A library that silently refuses someone's token is indistinguishable from a person who watches
    nothing — both produce an empty watched set. Only this pairing separates them, and getting it
    wrong sends a maintainer hunting the recommendation engine for a permissions bug.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        users = session.query(User).filter(User.enabled.is_(True)).order_by(User.slug).all()
        synced: dict[int, set[str]] = {}
        for row in session.query(WatchSyncState).all():
            synced.setdefault(row.user_id, set()).add(row.section_key)

        tokens: dict[int, str] = {}
        token_error = None
        client = _plextv_client(store, _machine_id(session))
        if client is None:
            token_error = "Plex isn't linked."
        else:
            try:
                # The tokens themselves are never rendered, stored, or logged — only their presence
                # (rule 9). This is the same roster read a run does.
                tokens = client.shared_server_tokens()
            except Exception as e:  # a probe reports its failure rather than becoming one
                token_error = _fail(e)

        sections: list[str] = []
        try:
            plex = _plex_client(store)
            if plex is not None:
                sections = [str(s.key) for s in plex.sections()]
        except Exception as e:
            logger.debug("support connection: section list failed ({})", type(e).__name__)

        rows: list[dict] = []
        for user in users:
            read = synced.get(user.id, set())
            never = sorted(set(sections) - read) if sections else []
            rows.append(
                {
                    "user": user.slug,
                    "user_type": user.user_type,
                    # The owner reads with the admin token and never appears in the share roster, so
                    # a missing entry there is expected for them and a real finding for anyone else.
                    "has_token": user.user_type == "owner" or user.plex_account_id in tokens,
                    "libraries_read": sorted(read),
                    "never_read": never,
                }
            )
        _audit(session, "connection", {"users": len(rows)})

    bad = [r for r in rows if not r["has_token"] or r["never_read"]]
    block = _stamp(_Block("connection check"))
    if token_error:
        block.kv("plex.tv", token_error)
    block.rule()
    # Same reasoning as `sharing`: only the people with a problem get a row. On a healthy 50-user
    # server the table was fifty lines of "yes, yes, yes", which buries the one line that matters.
    block.line(f"{len(rows) - len(bad)} of {len(rows)} people can be read in full.")
    if bad:
        block.rule()
        block.table(
            ["person", "type", "token", "libraries read", "never read"],
            [
                [
                    r["user"],
                    r["user_type"],
                    "yes" if r["has_token"] else "NO",
                    ",".join(r["libraries_read"]) or "-",
                    ",".join(r["never_read"]) or "-",
                ]
                for r in bad
            ],
            [14, 8, 6, 16, 12],
        )
        block.rule()
        # NOT stated as a fault, because it may not be one — and this ran on the maintainer's own
        # healthy server and flagged two people whose only "problem" was a Sports library never
        # shared with them. An unshared library and a failing one are indistinguishable from here:
        # `watch_sync` skips a 403 without recording state, and `force_full_next_time` only touches a
        # row that already exists, so neither leaves a trace. The check that CAN tell them apart is
        # the log — a real failure warns, an unshared library does not — so it points there.
        block.line(f"{len(bad)} person/people have a library we have never read:")
        block.line(f"  {', '.join(r['user'] for r in bad)}")
        block.line("That is expected if the library simply is not shared with them. If it IS shared,")
        block.line("reads are failing — check 'What errors has it logged?' for a warning naming them.")
    return {"users": rows, "problems": [r["user"] for r in bad], "error": token_error, "text": block.render()}


@_tool.get("/support/read-as")
async def read_as(
    request: Request,
    user: str = Query(min_length=1),
    endpoint: str = Query(default="libraries"),
    section: str = Query(default=""),
) -> dict:
    """Read the PMS AS one person, using their own share token, and show the raw response.

    The one thing nobody can do from a browser: a maintainer cannot log in as someone else's Plex
    account, and the owner's own view answers a different question. Reading the library through the
    share token is what settles "is this actually marked watched for THEM".
    """
    if endpoint not in _READ_AS_ENDPOINTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown check {endpoint!r}. Choose one of: {', '.join(sorted(_READ_AS_ENDPOINTS))}",
        )
    path, params = _READ_AS_ENDPOINTS[endpoint]

    state = request.app.state
    with state.sessions() as session:
        profile = _user_or_404(session, user)
        store = SettingsStore(session, state.secrets)
        plex_url = str(store.get("plex.url") or "")
        owner_token = str(store.get("plex.token") or "")
        client = _plextv_client(store, _machine_id(session))
        token = ""
        if profile.user_type == "owner":
            token = owner_token
        elif client is not None:
            try:
                token = client.shared_server_tokens().get(profile.plex_account_id, "")
            except Exception as e:
                logger.debug("support read-as: token fetch failed ({})", type(e).__name__)
        sections: list[tuple[str, str]] = []
        try:
            plex = _plex_client(store)
            if plex is not None:
                sections = [(str(s.key), s.title) for s in plex.sections()]
        except Exception as e:
            logger.debug("support read-as: section list failed ({})", type(e).__name__)
        _audit(session, "read-as", {"user": user, "endpoint": endpoint, "section": section})

    if not plex_url:
        raise HTTPException(status_code=409, detail="Plex isn't connected yet.")
    if not token:
        raise HTTPException(
            status_code=409,
            detail=f"No share token for {user}. Plex has not minted one — see the connection check.",
        )
    # `{key}` is only ever filled from a key the PMS itself just listed, so the path can't be steered.
    if "{key}" in path:
        valid = {key for key, _title in sections}
        if section not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Pick a library. This server has: {', '.join(sorted(valid)) or '(none readable)'}",
            )
        path = path.format(key=section)

    import httpx

    try:
        response = httpx.get(
            f"{plex_url.rstrip('/')}{path}",
            params=params,
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
            timeout=_PROBE_TIMEOUT_S,
        )
        status_code, body = response.status_code, response.text
    except Exception as e:
        status_code, body = 0, _fail(e)

    # Never echo the token back, even though we sent it — this text is destined for a chat window.
    # Two passes: the token we KNOW we sent, plus anything else credential-shaped the server echoed
    # back. This text is destined for a chat window.
    body = _scrub(body.replace(token, "<redacted>") if token else body)
    body = _MEDIA_PATH_ATTR.sub(r'\1="<path>"', body)
    total = ""
    if 'totalSize="' in body:
        total = body.split('totalSize="', 1)[1].split('"', 1)[0]

    block = _stamp(_Block("read as a user"))
    block.kv("person", user)
    block.kv("check", endpoint + (f" · library {section}" if section else ""))
    block.kv("status", f"HTTP {status_code}")
    if total:
        block.kv("titles", total)
    block.rule()
    for line in body.splitlines()[:20]:
        block.line(line.strip())
    return {
        "user": user,
        "endpoint": endpoint,
        "section": section,
        "status_code": status_code,
        "total_size": total,
        "body": body[:20000],
        "sections": [{"key": k, "title": t} for k, t in sections],
        "choices": sorted(_READ_AS_ENDPOINTS),
        "text": block.render(),
    }


@_tool.get("/support/sharing")
async def sharing(request: Request) -> dict:
    """Every account's live share filters, with OUR exclusions separated from everything else.

    Rows are made private by merging `label!=shortlist_<user>` into each other account's filters, and
    the event log records what CHANGED. Nothing shows what is true right now — so a filter that was
    never written, or was overwritten by another tool, is invisible until someone reports a leak.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        client = _plextv_client(store, _machine_id(session))

        # Which labels need hiding: the per-person rows that EXIST ON PLEX, read from the server.
        #
        # NOT the enabled-user list, which is what this used to do. The engine only ever excludes
        # labels it found on the PMS (`desired_excludes` <- `stored_labels`), so an enabled user who
        # has never received a row — a cold start, zero picks, a delivery that failed — contributes a
        # label that can never appear in anybody's filter. Every account then read as "missing" it,
        # and this reported `0 of N accounts hide every row` on a perfectly healthy server. One
        # reporter's server had 13 of 24 users on `picks=0`; the tool told them their privacy was
        # entirely broken (issue #76).
        #
        # Nor `len(accounts) - 1`, which is wrong in both directions: the OWNER has a row but is
        # absent from the plex.tv roster (`list_users` returns shared + Home users only), so that
        # undercounts; and a DISABLED user is in the roster with no row, so it also overcounts.
        #
        # Whose row is whose comes from ALL users, not just enabled ones: a paused or disabled person
        # still owns their collection, and their own label must never count as something they should
        # be hiding from themselves (see `privacy.desired_excludes`).
        labelled = {u.plex_account_id: f"{_LABEL_PREFIX}{u.slug}".lower() for u in session.query(User).all()}
        all_labels, rows_error = _existing_row_labels(store)
        # plex.tv gives us a USERNAME; `person()` and every other tool key on a SLUG, and `slugify`
        # lowercases and replaces punctuation — so they differ for essentially every real account
        # ("MooHouse" -> "moohouse", "Chris Smith" -> "chris_smith"). Passing a username on as if it
        # were a slug made the per-person section 404 for exactly the people with a privacy fault.
        slug_of = {u.plex_account_id: u.slug for u in session.query(User).all()}
        # Accounts the owner asked us to leave alone. They legitimately hide nothing, so counting them
        # as a fault would make this tool cry wolf on a server that is exactly as its owner set it up.
        unmanaged = {u.plex_account_id for u in session.query(User).filter_by(manage_sharing=False).all()}

        rows: list[dict] = []
        error = None
        if client is None:
            error = "Plex isn't linked."
        else:
            try:
                for account in client.list_users():
                    ours: dict[str, list[str]] = {}
                    theirs: list[str] = []
                    for name in ("filterMovies", "filterTelevision"):
                        raw = account.filters.get(name) or ""
                        if not raw:
                            continue
                        try:
                            conditions = privacy.parse_filter(raw)
                        except Exception:
                            # A filter we cannot parse is reported verbatim rather than
                            # mis-attributed — the engine refuses to rewrite one too.
                            theirs.append(f"{name}: {raw} (unparseable)")
                            continue
                        for condition in conditions:
                            mine = [v for v in condition.values if _is_ours(v)]
                            others = [v for v in condition.values if not _is_ours(v)]
                            if mine:
                                ours.setdefault(name, []).extend(sorted(mine))
                            if others or not mine:
                                joined = ",".join(others)
                                theirs.append(
                                    f"{name}: {condition.field}{condition.op}{joined}" if joined else f"{name}: —"
                                )
                    ours_flat = sorted({label for labels in ours.values() for label in labels})
                    rows.append(
                        {
                            "user": account.username,
                            "slug": slug_of.get(account.id, ""),
                            "account_id": account.id,
                            # LABELS, not clauses. `merge_label_excludes` unions every shortlist label
                            # into ONE `label!=` condition, so counting clauses reported "2
                            # exclusions" on a server hiding forty rows — and classified a whole
                            # clause as ours whenever it contained any shortlist label, blaming the
                            # owner's own `label!=Kids` on Shortlist. That made this unable to answer
                            # the one question it exists for: is every row excluded for this person.
                            # The owner turned sharing management off for this account: `missing` below
                            # is still reported truthfully, but it is a setting, not a fault.
                            "manage_sharing": account.id not in unmanaged,
                            "shortlist_excludes": ours_flat,
                            "shortlist_excludes_by_filter": {k: sorted(set(v)) for k, v in ours.items()},
                            "other_conditions": theirs,
                            "filters": {k: v for k, v in account.filters.items() if v},
                            # Every row that should be hidden from THIS person: all labelled people
                            # bar themselves. Their own label must never sit in their own filter —
                            # that would hide them from their own row (see `privacy.py`).
                            "should_hide": sorted(all_labels - {labelled.get(account.id, "")}),
                            "missing": sorted(
                                (all_labels - {labelled.get(account.id, "")}) - {unquote(v).lower() for v in ours_flat}
                            ),
                        }
                    )
            except Exception as e:
                error = _fail(e)
        _audit(session, "sharing", {"accounts": len(rows)})

    # A left-alone account is expected to hide nothing, so it is never "a person who can see a row
    # that is not theirs" — it is listed separately, by name, so the state stays visible.
    short = [r["user"] for r in rows if r["missing"] and r["manage_sharing"]]
    left_alone = [r["user"] for r in rows if not r["manage_sharing"]]
    # Slugs for the machine-readable field, usernames for the human-readable block. `person()` takes
    # a slug; an account plex.tv knows but our roster does not has none, and is dropped rather than
    # sent on to 404.
    short_slugs = [r["slug"] for r in rows if r["missing"] and r["manage_sharing"] and r["slug"]]

    # Only the accounts with something WRONG get detail; the rest are a count.
    #
    # Measured on a real 50-user server: printing every account made this section 446 lines of a
    # 779-line report — 57% of the whole thing, saying "fine" fifty times. That is not a formatting
    # preference: it pushed the report past what a chat message holds and buried the two lines that
    # mattered. A healthy server should produce a short report.
    block = _stamp(_Block("sharing and privacy"))
    block.rule()
    if error:
        block.line(f"COULD NOT READ: {error}")
    elif not rows:
        block.line("No accounts came back from plex.tv.")
    elif rows_error:
        # The filters below are still worth printing, but nothing can be CHECKED against them: with
        # no list of rows, every account trivially hides all zero of them. Saying so beats printing
        # "19 of 19 accounts hide every row" off a failed read.
        block.line(f"COULD NOT READ THE ROWS ON PLEX: {rows_error}")
        block.line("The filters below are real, but there is nothing to check them against.")
    elif not all_labels:
        block.line("No per-person rows exist on Plex yet, so there is nothing for anyone to hide.")
    else:
        healthy = len(rows) - len(short) - len(left_alone)
        managed_total = len(rows) - len(left_alone)
        # "managed accounts" only once some account is NOT managed. On the overwhelming majority of
        # servers nothing is left alone, and qualifying the count there would make every reader ask
        # what the qualifier excludes.
        counted = "managed accounts" if left_alone else "accounts"
        block.line(f"{healthy} of {managed_total} {counted} hide every row that is not theirs.")
        block.line(f"({len(all_labels)} per-person row(s) exist on Plex right now.)")
        if left_alone:
            # Split on what the FILTER actually says, not on the setting. The setting is the owner's
            # intent; whether our excludes really came off is a fact about plex.tv, and they are not
            # the same thing while a write is owed or permanently refused (a parental profile makes
            # plex.tv reject the removal for ever). Reporting intent as fact here would re-create
            # exactly what the comment at the top of this function exists to prevent.
            # PER-PERSON excludes only, matching what the writer actually removes. A restricted
            # shared row's exclude is KEPT on purpose (`privacy.clear_our_excludes`), so counting it
            # here reports a correct state as a permanent failure — and worse, makes the genuinely
            # stuck case (a 422'd removal) indistinguishable from the normal one.
            cleared = [r["user"] for r in rows if not r["manage_sharing"] and not _per_person_excludes(r)]
            still_held = [r for r in rows if not r["manage_sharing"] and _per_person_excludes(r)]
            if cleared:
                block.line(
                    f"Left alone by choice, so they hide nothing and can see other people's rows: "
                    f"{', '.join(cleared[:8])}"
                )
                if len(cleared) > 8:
                    block.line(f"  …and {len(cleared) - 8} more")
            for row in still_held[:8]:
                held = ", ".join(_per_person_excludes(row)[:6])
                block.line(f"{row['user']} (#{row['account_id']}) is set to be left alone, but our exclusions are")
                block.line(f"  STILL on this account — the removal has not gone through: {held}")
            if len(still_held) > 8:
                block.line(f"  …and {len(still_held) - 8} more account(s) in the same state")
        for row in (r for r in rows if r["missing"] and r["manage_sharing"]):
            block.line(f"{row['user']} (#{row['account_id']})")
            block.line(f"  hides {len(row['shortlist_excludes'])} of {len(row['should_hide'])} other rows")
            # Named, not counted: "which row can this person see" is the actual question.
            block.line(f"  NOT HIDDEN: {', '.join(row['missing'][:6])}")
            if len(row["missing"]) > 6:
                block.line(f"    …and {len(row['missing']) - 6} more")
    if short and not error:
        block.rule()
        block.line(f"PROBLEM: these people can see a row that is not theirs: {', '.join(short[:8])}")
    return {
        "accounts": rows,
        # Usernames — what the block shows, and what a human reads.
        "missing_excludes_for": short if not error else [],
        # Slugs — what `_people_worth_including` feeds to `person()`.
        "missing_excludes_slugs": short_slugs if not error else [],
        # What the verdict was measured AGAINST, so a caller can tell "everyone is covered" from
        # "there was nothing to cover" — the two used to be the same empty answer.
        "rows_on_plex": sorted(all_labels),
        "rows_error": rows_error,
        "error": error,
        "text": block.render(),
    }


@_tool.get("/support/surfaces")
async def surfaces(request: Request) -> dict:
    """Where every Shortlist row is ACTUALLY showing on the server, versus where it should be.

    The gap this closes: rows are hidden from other people by share filters, but the server OWNER has
    no share filter (plex-safety rule 5), so the only thing keeping somebody else's row off the
    owner's Home screen is the row's own ``promotedToOwnHome`` flag. Nothing reported those flags, so
    "the admin can see another user's row" was uninvestigable (issue #75).

    Two things are checked, and they are different in kind:

    * An INVARIANT — a per-person row that is not the owner's must never claim the owner's Home.
      No configuration makes that correct, so a violation is always a bug.
    * A CONSEQUENCE — the Recommended shelf is one flag per collection, and the owner has no filter,
      so a row set to show on friends' library shelves also appears on the OWNER's. That is a Plex
      limitation (see `RowSpec.show_friends_library`), so it is reported as an explanation, not a
      fault: the fix is a settings change, not a code change.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        owner = session.query(User).filter(User.user_type == "owner").one_or_none()
        owner_label = f"{_LABEL_PREFIX}{owner.slug}".lower() if owner else None
        placements = {
            c.slug: (c.name, c.placement or "both", c.placement_friends or c.placement or "both")
            for c in session.query(Collection).filter(Collection.enabled.is_(True)).all()
        }
        rows: list[dict] = []
        error = None
        try:
            plex = _plex_client(store)
            if plex is None:
                error = "Plex isn't connected."
            else:
                rows = plex.owned_row_surfaces()
        except Exception as e:
            error = _fail(e)
        _audit(session, "surfaces", {"rows": len(rows)})

    shared_prefix = SHARED_LABEL_PREFIX.lower()

    def is_someone_elses(row: dict) -> bool:
        """A per-person row belonging to anyone but the owner. A SHARED row is public and may sit on
        the owner's Home legitimately, so it is not a violation."""
        label = (row.get("label") or "").lower()
        if not label or label.startswith(shared_prefix):
            return False
        return label != owner_label

    on_owner_home = [r for r in rows if r.get("own_home") and is_someone_elses(r)]
    unlabelled = [r for r in rows if not r.get("label") and r.get("marked")]
    on_owner_shelf = [r for r in rows if r.get("recommended") and is_someone_elses(r)]

    block = _stamp(_Block("where each row is showing"))
    block.rule()
    if error:
        block.line(f"COULD NOT READ: {error}")
    elif not rows:
        block.line("No Shortlist collections on the server.")
    else:
        if owner_label is None:
            block.line("No owner recorded, so 'whose row is this' cannot be judged.")
        block.line("R = library Recommended, H = owner's Home, S = friends' Home")
        block.table(
            ["row", "library", "flags"],
            [
                [
                    # `.get`, not `[]`: this is a diagnostic, and a row missing a field must cost
                    # that cell, never the whole section of the bundle someone is trying to send us.
                    r.get("title", "?"),
                    r.get("library", "?"),
                    "".join(
                        (
                            "R" if r.get("recommended") else ".",
                            "H" if r.get("own_home") else ".",
                            "S" if r.get("shared_home") else ".",
                        )
                    )
                    if "error" not in r
                    else "?",
                ]
                for r in rows
            ],
            [34, 16, 5],
        )
        block.rule()
        block.line("configured placement, per row:")
        for slug, (name, own, friends) in sorted(placements.items()):
            block.line(f"  {name or slug}: you={own}, everyone else={friends}")
    if on_owner_home:
        block.rule()
        block.line(f"BUG: {len(on_owner_home)} row(s) that are not yours sit on YOUR Home screen.")
        block.line("Nothing can hide these from you — no share filter applies to the owner.")
        for r in on_owner_home[:8]:
            block.line(f"  {r.get('title', '?')} ({r.get('library', '?')})")
    if on_owner_shelf:
        block.rule()
        block.line(f"{len(on_owner_shelf)} row(s) that are not yours are on a Recommended shelf.")
        block.line(
            "Expected if a row is set to show on everyone else's library shelf: Plex has one "
            "Recommended flag per collection and you have no filter, so you see them all. Set that "
            "row's placement for everyone else to Home only."
        )
    if unlabelled:
        block.rule()
        block.line(f"BUG: {len(unlabelled)} collection(s) are ours but carry NO label.")
        block.line("No share filter can hide an unlabelled row, so everyone can see it.")
        for r in unlabelled[:8]:
            block.line(f"  {r.get('title', '?')} ({r.get('library', '?')})")
    return {
        "rows": rows,
        "owner_label": owner_label,
        "on_owner_home": on_owner_home,
        "on_owner_shelf": on_owner_shelf,
        "unlabelled": unlabelled,
        "error": error,
        "text": block.render(),
    }


@_tool.get("/support/drift")
async def drift(request: Request) -> dict:
    """Does Plex match the ledger? The register of a bug class with fifteen recorded instances.

    The database records that a row was delivered; the server is where it either exists or does not.
    Nothing today compares the two, so a write that silently failed looks identical to one that
    worked. Read-only by design: this reports drift and never repairs it, because repair belongs to a
    run, where snapshots and dry-run already live.
    """
    from shortlist.server.db.models import Delivery

    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        ledger = session.query(Delivery).all()
        on_plex: list[dict] = []
        # Collections that are ours by TITLE MARKER, whatever their label says. `list_owned_collections`
        # matches on the label alone, so "the rows were deleted" and "we could not read their labels"
        # both come back as zero and are indistinguishable — which is exactly the ambiguity a reporter
        # hit (issue #76: `in ledger 14 / on Plex 0`). The marker is independent of the label, so the
        # two counts disagreeing says which of the two happened.
        marked = 0
        error = None
        try:
            plex = _plex_client(store)
            if plex is None:
                error = "Plex isn't connected."
            else:
                on_plex = plex.list_owned_collections()
                # `flags=False`: this only needs the count, and the surface flags cost a round-trip
                # per collection — 91 of them on a real server, for a number nothing here reads.
                marked = sum(1 for r in plex.owned_row_surfaces(flags=False) if r.get("marked"))
        except Exception as e:
            error = _fail(e)

        live_keys = {int(c["rating_key"]) for c in on_plex}
        ledger_keys = {int(d.rating_key) for d in ledger}
        # Only meaningful when the read SUCCEEDED: an empty list from a failed read would otherwise
        # report every row on the server as missing, which is the most alarming possible false alarm.
        missing = (
            [
                {"row": d.collection_slug, "user": d.user_slug, "library": d.library_key, "rating_key": d.rating_key}
                for d in ledger
                if int(d.rating_key) not in live_keys
            ]
            if not error
            else []
        )
        orphans = [c for c in on_plex if int(c["rating_key"]) not in ledger_keys] if not error else []
        _audit(session, "drift", {"ledger": len(ledger), "on_plex": len(on_plex)})

    block = _stamp(_Block("drift check"))
    block.kv("in ledger", str(len(ledger)))
    block.kv("on plex", "unknown" if error else str(len(on_plex)))
    block.kv("ours by marker", "unknown" if error else str(marked))
    block.rule()
    if error:
        block.line(f"COULD NOT READ PLEX: {error}")
        block.line("No comparison made — an unread server is not the same as an empty one.")
    elif marked > len(on_plex):
        # The two counts are read the same way except for what identifies a row, so this can only
        # mean the labels went missing — not the rows. Worth saying loudly: an unlabelled row is one
        # no share filter can hide, AND one the next run's sweep deletes as an orphan.
        block.line(f"WARNING: {marked - len(on_plex)} collection(s) are ours but have lost their label.")
        block.line("The rows are still there — Shortlist just cannot see them by label any more.")
        block.line("Nothing can hide an unlabelled row, and the next run will delete it.")
    elif not missing and not orphans:
        block.line("Every delivered row exists on the server, and nothing extra is labelled ours.")
    for item in missing[:8]:
        block.line(f"MISSING on Plex: {item['row']} for {item['user']} (library {item['library']})")
    for item in orphans[:8]:
        block.line(f"ORPHAN on Plex: {item.get('title', '?')} [{item.get('label', '?')}]")
    return {
        "ledger_count": len(ledger),
        "plex_count": len(on_plex),
        "marked_count": marked,
        "missing_on_plex": missing,
        "orphans_on_plex": orphans,
        "error": error,
        "text": block.render(),
    }


@_tool.get("/support/suggestions")
async def suggestions(request: Request) -> dict:
    """People and titles to offer as you type, for the checks that take a name.

    Exists because a username typed from memory is the commonest way one of these checks comes back
    empty — and an empty result is indistinguishable from "nothing is wrong", which is the worst
    answer a diagnostic can give. The person operating the page may not know that Plex usernames are
    not display names, or how a title is spelled in the library.

    Deliberately DB-only and unconditional: no Plex call, so it still populates when the server is
    unreachable, which is exactly when these checks are being used.
    """
    with request.app.state.sessions() as session:
        people = [
            {"slug": u.slug, "display_name": u.display_name, "enabled": bool(u.enabled)}
            for u in session.query(User).order_by(User.enabled.desc(), User.slug).all()
        ]
        # Titles worth asking about: what has been recommended, plus what has been watched. Delivered
        # first — a question is far more often about something that turned up than about something in
        # a watch history — then capped, because a datalist of 13,000 entries helps nobody.
        delivered = [t for (t,) in session.query(PickRow.title).filter(PickRow.title != "").distinct().limit(400).all()]
        watched = [
            t for (t,) in session.query(WatchedTitle.title).filter(WatchedTitle.title != "").distinct().limit(600).all()
        ]
        titles = sorted(dict.fromkeys([*delivered, *watched]))
    return {"people": people, "titles": titles}


@_tool.get("/support/errors")
async def errors(request: Request) -> dict:
    """Recent WARNING and ERROR log lines — the part of a bug report nobody remembers to attach.

    Everything here is already redacted by `log_reader.scrub` (an alias for `http_retry.redact`),
    which covers more credential shapes than this module's own `_scrub`: query params, header forms,
    Bearer credentials. Over-redaction is fine; a leaked token is not (rule 9).

    Capped hard, because this rides along in a report someone pastes into a chat window. When the tail
    is not enough, the full redacted log zip is a separate download (`/api/system/logs/download`) and
    the block says so.
    """
    from shortlist.server.services import log_reader

    config_dir = request.app.state.config_dir
    try:
        found = log_reader.read_lines(config_dir, level="WARNING", limit=_ERROR_LINES)
    except Exception as e:
        found = {"lines": [], "total_matched": 0, "truncated": False, "file": None, "error": _fail(e)}
    # `read_lines` strips credentials but not addresses — it serves the live Logs view, which renders
    # on the owner's own screen. Here the same lines are bound for a bug report, so they get the full
    # pass BEFORE the block is built, which also covers the raw `lines` in the JSON below. Nothing
    # renders that field today; leaving it as the one unshaped thing in the response is how it
    # becomes a leak the day something does.
    lines = [
        {**entry, "source": _scrub(str(entry.get("source", ""))), "message": _scrub(str(entry.get("message", "")))}
        for entry in found.get("lines", [])
    ]
    with request.app.state.sessions() as session:
        _audit(session, "errors", {"lines": len(lines)})

    block = _stamp(_Block("recent warnings and errors"))
    block.kv("log file", str(found.get("file") or "none found"))
    block.kv("matched", f"{found.get('total_matched', 0)} at WARNING or above")
    block.rule()
    if found.get("error"):
        block.line(f"COULD NOT READ THE LOG: {found['error']}")
    elif not lines:
        block.line("No warnings or errors in the current log file.")
    for entry in lines:
        stamp = (entry.get("ts") or "")[:19].replace("T", " ")
        block.line(f"{stamp} {entry.get('level', '')[:4]:<5}{entry.get('source', '')}")
        block.line(f"    {entry.get('message', '')}")
    if found.get("total_matched", 0) > len(lines):
        block.rule()
        block.line(f"Only the newest {len(lines)} are here. For the rest, attach the log zip from Logs.")
    return {
        "lines": lines,
        "total_matched": found.get("total_matched", 0),
        "log_file": found.get("file"),
        "text": block.render(),
    }


@_tool.get("/support/runs")
async def recent_runs(request: Request) -> dict:
    """The last few runs, and WHO failed in each — the other thing a report always needs.

    The timeline says a run finished with a status. This says which people it could not build and why,
    which is the difference between "a run failed" and something actionable.
    """
    with request.app.state.sessions() as session:
        users = {u.id: u.slug for u in session.query(User).all()}
        out: list[dict] = []
        for run in session.query(Run).order_by(Run.id.desc()).limit(5).all():
            per_user = session.query(RunUser).filter(RunUser.run_id == run.id).all()
            failed = [
                {"user": users.get(ru.user_id, str(ru.user_id)), "error": _scrub((ru.error or "")[:200])}
                for ru in per_user
                if ru.status not in ("ok", "skipped", "cold_start")
            ]
            out.append(
                {
                    "id": run.id,
                    "status": run.status,
                    "trigger": run.trigger,
                    "dry_run": bool(run.dry_run),
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "stats": dict(run.stats or {}),
                    "people": len(per_user),
                    "failed": failed,
                }
            )
        _audit(session, "runs", {"runs": len(out)})

    block = _stamp(_Block("recent runs"))
    block.rule()
    if not out:
        block.line("No runs yet. Nothing has been built for anyone.")
    for run in out:
        when = (run["started_at"] or "")[:16].replace("T", " ")
        block.line(f"#{run['id']} {run['status']} ({run['trigger']}) {when} — {run['people']} people")
        stats = run["stats"]
        if stats:
            block.line(f"    {', '.join(f'{k}={v}' for k, v in list(stats.items())[:6])}")
            # The 6-key truncation above is a readability cap on ordinary counters, but two keys
            # report a privacy FAULT — and both sort late enough to fall outside it. A bundle that
            # silently dropped them is exactly the artifact someone attaches when reporting the leak.
            for key in ("filters_not_enforced", "left_alone_failures"):
                if stats.get(key):
                    block.line(f"    !! {key}={stats[key]}")
        for failure in run["failed"][:5]:
            block.line(f"    FAILED {failure['user']}: {failure['error'] or '(no message)'}")
    return {"runs": out, "text": block.render()}


@_tool.get("/support/report.zip")
async def report_zip(request: Request) -> Response:
    """The whole report AND every redacted log file, as one attachment.

    The text report is deliberately capped so it can be pasted into a chat window — newest 40
    warnings, five runs. That is enough to name most problems and not enough for one that needs
    history, and asking someone to then find the Logs page and export separately is a step that does
    not happen. This is the one button for "give them everything".

    Log files are redacted by `log_reader.build_zip`, file by file, because an export is the single
    most likely thing to end up in a public issue tracker.
    """
    import io
    import zipfile

    from fastapi.concurrency import run_in_threadpool

    from shortlist.server.services import log_reader

    text = await bundle(request)
    config_dir = request.app.state.config_dir
    # `require_support_mode` already read these from the same DB for this request; querying again
    # would be a second source of truth for one value, which is the drift this module exists to end.
    literals = _KNOWN.get()

    def _build() -> bytes:
        """Decompress, rewrite and recompress the log set OFF the event loop.

        Logs rotate at 10 MB a file, so on a busy install this is tens of megabytes of unzip + regex +
        rezip. Done inline it stalls the SSE run-progress stream, the UI, and `/api/system/health` —
        which is Docker's HEALTHCHECK — on the page whose whole premise is that the server may already
        be struggling.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("shortlist-report.txt", text)
            # Nested rather than merged: `build_zip` owns ALL of the redaction — credentials,
            # addresses and known literals — and the vanished-under-rotation handling. This function
            # used to shape hosts itself on top, which is how the machine id survived: the copy here
            # lacked the literal pass that `_scrub` had, and the pattern could not match a
            # URL-encoded id. Nothing is rewritten here now, so there is nothing left to drift.
            try:
                with zipfile.ZipFile(io.BytesIO(log_reader.build_zip(config_dir, literals))) as logs:
                    for name in logs.namelist():
                        archive.writestr(name, logs.read(name))
            except Exception as e:
                archive.writestr("logs/UNAVAILABLE.txt", f"Logs could not be read: {_fail(e)}")
        return buffer.getvalue()

    content = await run_in_threadpool(_build)
    with request.app.state.sessions() as session:
        _audit(session, "report.zip", {})
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="shortlist-report.zip"'},
    )


#: Assembled last, on purpose — see the module docstring. `_mode` carries owner auth only; `_tool`
#: carries owner auth AND the support-mode gate.
router = APIRouter(tags=["support"])
router.include_router(_mode)
router.include_router(_tool)
