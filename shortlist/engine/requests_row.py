"""Who asked for what: the read behind a "Your requests" row (issue #127).

Two sources, merged so a title counts once per person. Overseerr's request list covers requests that
are still there; Radarr/Sonarr requester tags (``<seerrUserId>-<username>``, written by Seerr's
"Tag Requests" and never removed when the request is deleted — fixture ``radarr_request_tags.json``)
cover the ones an owner tidied away. A person is only ever identified by Plex account ID: an
Overseerr tag resolves through Overseerr's user list, an own-pattern tag through the roster, and a
tag that fits nobody or two people is reported, never guessed — a wrong guess puts one person's
requests in another person's private row.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from shortlist.engine.clients.arr import RadarrClient, SonarrClient
from shortlist.engine.clients.seerr import SeerrClient, _int_or_none
from shortlist.engine.delivery import section_kind
from shortlist.engine.models import MediaType, Pick, RequestSources, RowSpec, UserProfile

if TYPE_CHECKING:
    # rows.py imports this module; a runtime import here would close the cycle.
    from shortlist.engine.rows import RowPolicy

REQUESTER_TAG = re.compile(r"^(\d+)\s?-\s?\S")
# Seerr's enums: MediaRequestStatus and MediaStatus (server/constants/media.ts).
_REQ_APPROVED, _REQ_COMPLETED = 2, 5
_MEDIA_AVAILABLE = 5
# Radarr/Sonarr lower-case a tag and turn spaces into dashes, so "Sarah Jones" is stored "sarah-jones".
_TAG_CHARSET = re.compile(r"[^a-z0-9-]")
_SEERR_MEDIA_KIND = {MediaType.MOVIE: "movie", MediaType.SHOW: "tv"}


@dataclass(frozen=True)
class RequestedTitle:
    """One title one person asked for, with when it was asked for and when it arrived."""

    tmdb_id: int
    media_type: MediaType
    plex_account_id: int
    requested_at: datetime | None
    landed_at: datetime | None
    on_disk: bool
    seasons_landed: bool
    found_in: tuple[str, ...]
    title: str = ""
    #: The own-pattern that matched its tag; empty for Overseerr and override matches.
    pattern: str = ""


@dataclass(frozen=True)
class TagMatch:
    """How one requester tag resolved, for the owner to check the mapping.

    ``titles`` counts every item carrying the tag, including ones that yielded nothing because the
    tag was ambiguous or the item is Shortlist's own.
    """

    label: str
    source: str  # "overseerr" | "pattern" | "override"
    plex_account_id: int | None
    titles: int
    ambiguous: bool


@dataclass
class RequestLedger:
    """Every configured source read once, plus what went wrong doing it."""

    titles: list[RequestedTitle]
    complete: bool
    problems: list[str] = field(default_factory=list)
    tag_matches: list[TagMatch] = field(default_factory=list)
    seerr_requests: int = 0
    seerr_requesters: int = 0
    seerr_linked: int = 0
    seerr_servers: list[dict] = field(default_factory=list)
    #: Every Plex account id an Overseerr account is linked to — how "linked" is told apart from
    #: "has asked for something": a person with an account and no requests is still linked.
    seerr_plex_ids: set[int] = field(default_factory=set)
    #: App names ("Overseerr", "Radarr", "Sonarr") whose read FAILED — the same events that flip
    #: ``complete``. Advice and degradations land in ``problems`` only, so a setup screen can say
    #: "unreachable" about a source that is down and nothing else.
    unreadable: set[str] = field(default_factory=set)

    def for_person(self, plex_account_id: int) -> list[RequestedTitle]:
        """The titles one person asked for, newest arrival first."""
        return [t for t in self.titles if t.plex_account_id == plex_account_id]


def parse_requester_tag(label: str) -> int | None:
    """The Seerr user id in an Overseerr-format requester tag (``12-sarah``), else None."""
    m = REQUESTER_TAG.match(label.strip())
    return int(m.group(1)) if m else None


def _norm(label: str) -> str:
    return _TAG_CHARSET.sub("", label.strip().lower().replace(" ", "-").replace("_", "-"))


def pattern_matches(label: str, pattern: str, people: list[UserProfile]) -> list[UserProfile]:
    """Every person whose ``{username}``/``{name}`` rendering of ``pattern`` is this tag.

    Case-insensitive, and spaces read as dashes, because that is how the Arr stores a tag. A pattern
    with no placeholder renders the same for everyone, so it can name nobody.
    """
    if "{username}" not in pattern and "{name}" not in pattern:
        return []
    want = _norm(label)
    out = []
    for p in people:
        rendered = pattern.replace("{username}", p.username).replace("{name}", p.display_name)
        if _norm(rendered) == want:
            out.append(p)
    return out


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Every source stamps UTC; a bare timestamp would make the ledger's sort raise on comparison.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def collect_requests(
    sources: RequestSources,
    people: list[UserProfile],
    *,
    seerr: SeerrClient | None = None,
    radarr: RadarrClient | None = None,
    sonarr: SonarrClient | None = None,
    patterns: frozenset[str] = frozenset(),
) -> RequestLedger:
    """Read every configured source once and return the ledger.

    Never raises: a source that fails is recorded in ``problems`` and flips ``complete`` to False,
    which is what stops any row being REMOVED on the strength of a read that did not happen.

    Args:
        sources: Where to read; a source that is None is skipped.
        people: The roster; a request is only ever attributed to one of these, by Plex account ID.
        seerr: Injectable Overseerr client; built from ``sources.overseerr`` when None.
        radarr: Injectable Radarr client; built from ``sources.radarr`` when None.
        sonarr: Injectable Sonarr client; built from ``sources.sonarr`` when None.
        patterns: Every distinct own-tag pattern across the requests rows (``req-{username}``).

    Returns:
        The ledger, titles sorted newest arrival first.
    """
    ledger = RequestLedger(titles=[], complete=True)
    by_plex = {p.plex_account_id: p for p in people}
    seerr_plex: dict[int, int | None] = {}
    seerr_client = seerr or (SeerrClient(sources.overseerr) if sources.overseerr else None)
    merged: dict[tuple[MediaType, int, int], RequestedTitle] = {}

    def add(t: RequestedTitle) -> None:
        # The Overseerr request carries the dates the person saw; a tag only proves they asked. The
        # request side wins its ``pattern`` too (empty): Overseerr proof holds in every row for the
        # person, whatever tag pattern that row uses. Between two tags, the override's empty pattern
        # wins for the same reason.
        key = (t.media_type, t.tmdb_id, t.plex_account_id)
        prev = merged.get(key)
        if prev is None:
            merged[key] = t
        elif "overseerr" in prev.found_in or ("overseerr" not in t.found_in and not prev.pattern):
            merged[key] = replace(
                prev,
                found_in=tuple(dict.fromkeys(prev.found_in + t.found_in)),
                title=prev.title or t.title,
            )
        else:
            merged[key] = replace(
                t,
                found_in=tuple(dict.fromkeys(t.found_in + prev.found_in)),
                title=t.title or prev.title,
            )

    if seerr_client is not None:
        try:
            seerr_plex = seerr_client.user_plex_ids()
            ledger.seerr_plex_ids = {pid for pid in seerr_plex.values() if pid is not None}
            rows = seerr_client.requests()
            ledger.seerr_servers = [
                {
                    "kind": kind,
                    "name": s.get("name", ""),
                    "is4k": bool(s.get("is4k")),
                    "tag_requests": bool(s.get("tagRequests")),
                }
                for kind, servers in seerr_client.arr_settings().items()
                for s in servers
            ]
            if not rows and not seerr_plex:
                # A Seerr with an admin has at least one user, so this shape is a broken read.
                ledger.complete = False
                ledger.unreadable.add("Overseerr")
                ledger.problems.append("Overseerr answered with no users and no requests — treated as a failed read")
            ledger.seerr_requests = len(rows)
            requesters = {r.get("requestedBy", {}).get("id") for r in rows if isinstance(r.get("requestedBy"), dict)}
            ledger.seerr_requesters = len(requesters)
            ledger.seerr_linked = len({u for u in requesters if seerr_plex.get(u) in by_plex})
            for r in rows:
                t = _from_seerr_request(r, seerr_plex, by_plex, sources.exclude_seerr_user_id)
                if t is not None:
                    add(t)
        except Exception as e:
            ledger.complete = False
            ledger.unreadable.add("Overseerr")
            ledger.problems.append(f"Overseerr could not be read: {e}")
            logger.warning("requests row: Overseerr read failed ({})", e)

    for kind, client in (
        (MediaType.MOVIE, radarr or (RadarrClient(sources.radarr) if sources.radarr else None)),
        (MediaType.SHOW, sonarr or (SonarrClient(sources.sonarr) if sources.sonarr else None)),
    ):
        if client is None:
            continue
        try:
            tags = client.tags()
            items = client.movies() if kind is MediaType.MOVIE else client.series()
        except Exception as e:
            ledger.complete = False
            ledger.unreadable.add(client.app_name)
            ledger.problems.append(f"{client.app_name} could not be read: {e}")
            logger.warning("requests row: {} read failed ({})", client.app_name, e)
            continue
        _add_tagged(
            ledger, kind, items, tags, people, by_plex, seerr_plex, seerr_client is not None, sources, patterns, add
        )

    # A title found only by tag has no request to date it from. Seerr's media table knows when Plex
    # got it (and, for a show, when the latest season did), which is the arrival the person saw;
    # the Arr's own date is the fallback, and for Sonarr that is the day the series was ADDED, not
    # when anything landed. The table is one paged walk, so it is read only when a title needs it.
    tag_only = [k for k, t in merged.items() if "overseerr" not in t.found_in]
    if tag_only and seerr_client is not None:
        media_dates: dict[tuple[str, int], dict] = {}
        try:
            media_dates = seerr_client.media_dates()
        except Exception as e:
            ledger.problems.append(f"Overseerr media dates could not be read: {e}")
            logger.warning("requests row: Overseerr media dates failed ({})", e)
        for key in tag_only:
            t = merged[key]
            rec = media_dates.get((_SEERR_MEDIA_KIND[t.media_type], t.tmdb_id))
            if not rec:
                continue
            landed = _iso(rec.get("lastSeasonChange")) if t.media_type is MediaType.SHOW else None
            landed = landed or _iso(rec.get("mediaAddedAt"))
            if landed:
                merged[key] = replace(t, landed_at=landed)

    ledger.titles = sorted(merged.values(), key=lambda t: t.landed_at or datetime(1, 1, 1, tzinfo=UTC), reverse=True)
    ledger.tag_matches = _merge_tag_matches(ledger.tag_matches)
    return ledger


def _merge_tag_matches(matches: list[TagMatch]) -> list[TagMatch]:
    """One entry per (label, source): Radarr and Sonarr each report the tag, and the owner wants one line.

    A tag resolves to the same person in both arrs (same label, same roster), so only the counts differ.
    First-seen order is kept.
    """
    by_key: dict[tuple[str, str], TagMatch] = {}
    for m in matches:
        key = (m.label, m.source)
        seen = by_key.get(key)
        by_key[key] = (
            m if seen is None else replace(seen, titles=seen.titles + m.titles, ambiguous=seen.ambiguous or m.ambiguous)
        )
    return list(by_key.values())


def _from_seerr_request(
    r: dict, seerr_plex: dict[int, int | None], by_plex: dict[int, UserProfile], exclude_uid: int
) -> RequestedTitle | None:
    who = r.get("requestedBy") if isinstance(r.get("requestedBy"), dict) else {}
    uid = who.get("id")
    if uid is None or (exclude_uid and uid == exclude_uid):
        return None
    if r.get("status") not in (_REQ_APPROVED, _REQ_COMPLETED):
        return None
    plex_id = _int_or_none(who.get("plexId")) or seerr_plex.get(uid)
    if plex_id not in by_plex:
        return None
    media = r.get("media") if isinstance(r.get("media"), dict) else {}
    tmdb_id = media.get("tmdbId")
    kind = MediaType.MOVIE if r.get("type") == "movie" else MediaType.SHOW if r.get("type") == "tv" else None
    if not isinstance(tmdb_id, int) or kind is None:
        return None
    # A 4K request is fulfilled by the 4K copy: Seerr tracks the two libraries separately.
    status = media.get("status4k" if r.get("is4k") else "status")
    on_disk = status == _MEDIA_AVAILABLE or (kind is MediaType.SHOW and r.get("status") == _REQ_COMPLETED)
    seasons = r.get("seasons") if isinstance(r.get("seasons"), list) else []
    seasons_landed = (
        kind is MediaType.MOVIE
        or r.get("status") == _REQ_COMPLETED
        or (bool(seasons) and all(s.get("status") == _REQ_COMPLETED for s in seasons))
    )
    landed = None
    if kind is MediaType.SHOW:
        done = [_iso(s.get("updatedAt")) for s in seasons if s.get("status") == _REQ_COMPLETED]
        landed = max((d for d in done if d), default=None) or _iso(media.get("lastSeasonChange"))
    landed = landed or _iso(media.get("mediaAddedAt"))
    return RequestedTitle(
        tmdb_id=tmdb_id,
        media_type=kind,
        plex_account_id=int(plex_id),
        requested_at=_iso(r.get("createdAt")),
        landed_at=landed,
        on_disk=bool(on_disk),
        seasons_landed=bool(seasons_landed),
        found_in=("overseerr",),
    )


def _add_tagged(
    ledger: RequestLedger,
    kind: MediaType,
    items: list[dict],
    tags: dict[int, str],
    people: list[UserProfile],
    by_plex: dict[int, UserProfile],
    seerr_plex: dict[int, int | None],
    seerr_connected: bool,
    sources: RequestSources,
    patterns: frozenset[str],
    add: Callable[[RequestedTitle], None],
) -> None:
    shortlist_tag_ids = {
        i for i, label in tags.items() if sources.shortlist_tag and _norm(label) == _norm(sources.shortlist_tag)
    }
    override: dict[str, list[UserProfile]] = {}
    for p in people:
        if p.requested_by_tag:
            override.setdefault(_norm(p.requested_by_tag), []).append(p)
    counts: dict[tuple[str, str], int] = {}
    owner_of: dict[
        int, tuple[UserProfile | None, str, str, bool]
    ] = {}  # tag id -> (person, source, pattern, ambiguous)
    warned_seerr = False
    for tag_id, label in tags.items():
        if _norm(label) in override:
            # Before the Overseerr-format branch: an owner who typed `12-sarah` on a person meant that
            # person, whether or not Overseerr is connected to say who user 12 is.
            # Two people who typed the same tag: nobody's, the same as an ambiguous pattern match.
            claimants = override[_norm(label)]
            owner_of[tag_id] = (claimants[0] if len(claimants) == 1 else None, "override", "", len(claimants) > 1)
            continue
        uid = parse_requester_tag(label)
        if uid is not None:
            # The number is a Seerr user id, meaningful only through Seerr's user list — the name
            # after it is whatever the person was called when the tag was made, never a key.
            if not seerr_connected:
                if not warned_seerr:
                    ledger.problems.append(
                        "Overseerr requester tags were found in Radarr/Sonarr, but Overseerr isn't connected, "
                        "so they can't be traced to a person"
                    )
                    warned_seerr = True
                continue
            if sources.exclude_seerr_user_id and uid == sources.exclude_seerr_user_id:
                # The account Shortlist files its own requests as: its tag rides on every title we
                # added, and its requests are already left out of the Overseerr read for the same reason.
                owner_of[tag_id] = (None, "overseerr", "", False)
                continue
            plex_id = seerr_plex.get(uid)
            owner_of[tag_id] = (by_plex.get(plex_id), "overseerr", "", False)
            continue
        for pattern in sorted(patterns):
            hits = pattern_matches(label, pattern, people)
            if hits:
                owner_of[tag_id] = (hits[0] if len(hits) == 1 else None, "pattern", pattern, len(hits) > 1)
                break
    for item in items:
        tmdb_id = item.get("tmdbId")
        if not isinstance(tmdb_id, int):
            if any(t in owner_of for t in item.get("tags", [])):
                ledger.problems.append(
                    f"{item.get('title', '?')} carries a requester tag but has no TMDB id, "
                    "so it can't be matched to Plex"
                )
            continue
        item_tags = [t for t in item.get("tags", []) if t in owner_of]
        # Shortlist's own additions carry its tag and usually the owner's requester tag too; only an
        # Overseerr tag proves the person asked, an own-pattern match on such an item is the owner's.
        ours = bool(shortlist_tag_ids & set(item.get("tags", [])))
        for tag_id in item_tags:
            person, source, pattern, _ambiguous = owner_of[tag_id]
            counts[(tags[tag_id], source)] = counts.get((tags[tag_id], source), 0) + 1
            if person is None or (ours and source != "overseerr"):
                continue
            if kind is MediaType.MOVIE:
                on_disk = bool(item.get("hasFile"))
                landed = _iso((item.get("movieFile") or {}).get("dateAdded"))
            else:
                on_disk = int((item.get("statistics") or {}).get("episodeFileCount") or 0) > 0
                landed = _iso(item.get("added"))
            add(
                RequestedTitle(
                    tmdb_id=tmdb_id,
                    media_type=kind,
                    plex_account_id=person.plex_account_id,
                    requested_at=_iso(item.get("added")),
                    landed_at=landed if on_disk else None,
                    on_disk=on_disk,
                    seasons_landed=True,
                    found_in=("tag",),
                    title=str(item.get("title") or ""),
                    pattern=pattern,
                )
            )
    for tag_id, (person, source, _pattern, ambiguous) in owner_of.items():
        ledger.tag_matches.append(
            TagMatch(
                label=tags[tag_id],
                source=source,
                plex_account_id=person.plex_account_id if person else None,
                titles=counts.get((tags[tag_id], source), 0),
                ambiguous=ambiguous,
            )
        )


def build_requests_picks(
    policy: RowPolicy, spec: RowSpec, targets: list, k: int, ledger: RequestLedger, *, now: datetime
) -> dict[str, list[Pick]]:
    """This person's requested titles that are on Plex, unwatched, visible to them and recent — newest first.

    No pool, no curator, no padding: the row is exactly what they asked for, or nothing. Every title
    the ledger holds for them is written to the trace with the reason it is or isn't in the row.

    Args:
        policy: The person's row policy — their watched breakdown, visibility check and the run context.
        spec: The requests row being built; its window and own-tag pattern decide what qualifies.
        targets: The Plex library sections this row is delivered into.
        k: The row size.
        ledger: Every request read this run, for everyone.
        now: The moment the window is measured from.

    Returns:
        ``{section.key: picks}`` for every target section, ranked newest arrival first.
    """
    ctx, user = policy.ctx, policy.user
    mine = [t for t in ledger.for_person(user.plex_account_id) if t.pattern in ("", spec.requests_tag_pattern)]
    cutoff = now - timedelta(days=spec.requests_window_days) if spec.requests_window_days > 0 else None
    out: dict[str, list[Pick]] = {}
    for section in targets:
        kind = section_kind(section)
        sec_idx = ctx.section_index.get(section.key, {})
        # One trace row per title, keyed by tmdb_id: a person's ledger holds a title once, and a
        # section holds one media type, so the key is unique within a section.
        rows: dict[int, dict] = {}
        keep: list[tuple[RequestedTitle, int]] = []
        for t in (x for x in mine if x.media_type is kind):
            key = sec_idx.get(t.tmdb_id)
            result = "in_row"
            # The library index comes first on purpose: a completed request whose media Seerr has
            # since deleted still reads on_disk=True, and only Plex knows whether the title is here.
            if key is None or not t.on_disk:
                result = "not_on_plex"
            elif not t.seasons_landed:
                result = "season_not_landed"
            elif _watched(policy, t):
                result = "watched"
            elif cutoff and t.landed_at and t.landed_at < cutoff:
                result = "too_old"
            rows[t.tmdb_id] = {
                "tmdb_id": t.tmdb_id,
                "media_type": t.media_type.value,
                "title": t.title,
                "asked_at": _stamp(t.requested_at),
                "landed_at": _stamp(t.landed_at),
                "found_in": list(t.found_in),
                "result": result,
            }
            if result == "in_row":
                keep.append((t, key))
        seen = policy.visible([key for _, key in keep]) if keep else None
        if seen is not None:
            for t, key in keep:
                if key not in seen:
                    rows[t.tmdb_id]["result"] = "hidden"
            keep = [(t, key) for t, key in keep if key in seen]
        keep.sort(key=lambda tk: tk[0].landed_at or datetime(1, 1, 1, tzinfo=UTC), reverse=True)
        for t, _key in keep[k:]:
            rows[t.tmdb_id]["result"] = "over_size"
        keep = keep[:k]
        items, _missing = ctx.plex.fetch_items([key for _, key in keep]) if keep else ([], [])
        by_key = {int(getattr(i, "ratingKey", 0)): i for i in items}
        picks: list[Pick] = []
        for t, key in keep:
            item = by_key.get(key)
            if item is None:
                rows[t.tmdb_id]["result"] = "not_on_plex"
                continue
            picks.append(
                Pick(
                    tmdb_id=t.tmdb_id,
                    rating_key=key,
                    title=str(getattr(item, "title", "") or t.title),
                    rank=len(picks) + 1,
                    reason=_reason(t),
                    media_type=kind,
                    sources=["requests"],
                    year=getattr(item, "year", None),
                )
            )
        out[section.key] = picks
        policy.report.trace.setdefault("selection", []).append(
            {
                "row": spec.slug,
                "library": getattr(section, "title", str(section.key)),
                "decision": "requests",
                "size": k,
                "delivered": len(picks),
                "candidates": len(rows),
                "pick_order": "newest",
                # The setting behind a `too_old` verdict, as it was on the night — the row may be edited later.
                "requests_window_days": spec.requests_window_days,
                "requests": list(rows.values()),
            }
        )
    return out


def _watched(policy: RowPolicy, t: RequestedTitle) -> bool:
    if t.media_type is MediaType.MOVIE:
        return t.tmdb_id in policy.watched_movies
    viewed, total = policy.watched_shows.get(t.tmdb_id, (0, None))
    # `total > 0`: a show Plex holds zero episodes of is not finished — `0 >= 0` would drop it as watched.
    return total is not None and total > 0 and viewed >= total


def _reason(t: RequestedTitle) -> str:
    return f"You asked for this on {t.requested_at:%-d %b}" if t.requested_at else "You asked for this"


def _stamp(d: datetime | None) -> str | None:
    return d.isoformat() if d else None
