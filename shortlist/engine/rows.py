"""Row construction: turn one user's (or the audience's) history into ranked, delivered picks.

Everything here is the "what goes in the row" half of the engine. The ordering that keeps a row
private — deliver unpromoted, merge filters, promote last — lives in ``pipeline.py``; this module
only builds and delivers collections, always UNPROMOTED.
"""

from __future__ import annotations

import hashlib
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from functools import cached_property

from loguru import logger

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import picker, ranking
from shortlist.engine import requests as requests_mod
from shortlist.engine.clients.mdblist import MdbListRateLimitError
from shortlist.engine.clients.plex_pms import _retry_idempotent
from shortlist.engine.context import EngineContext, _emit
from shortlist.engine.delivery import (
    deliver_rows,
    remove_row,
    render_row_name,
    resolve_row_template,
    row_marker,
    section_kind,
    sections_for_keys,
    target_sections,
)
from shortlist.engine.history import RatingsPolicy, derive_seeds, ratings_policy
from shortlist.engine.models import (
    SHARED_SLUG_PREFIX,
    Candidate,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    RequestWhy,
    RowSpec,
    UserProfile,
    UserRunReport,
    UserType,
    WatchedItem,
)


def effective_row_sources(spec: RowSpec, default_sources: list[str]) -> tuple[str, ...]:
    """The candidate sources a row actually gathers from, sorted (so identical sets share one pool).

    A row uses its own ``candidate_sources`` or the global default — the setting is the single source
    of truth for every row, shared or per-person. ``llm_web`` (a live web search + an LLM call) used
    to be hard-dropped from per-person rows on a cost assumption; a head-to-head (2026-07-19) showed it
    surfaces ~22 strong taste matches per person that TMDB-similar misses (Ozark, Succession, True
    Detective, Chernobyl…), so it's now allowed wherever it's configured. It's still gated by
    ``_web_search_capable`` (needs a curator + a search backend), and remains OFF unless it's in the
    sources list — remove it there to control the per-person Exa/LLM cost.
    """
    return tuple(sorted(spec.candidate_sources or default_sources))


def effective_max_seeds(spec: RowSpec, cfg: EngineConfig) -> int:
    """How many watched titles seed this row: its own budget, else the run's.

    Module-level so the per-person and shared paths cannot resolve it differently — they used to
    re-inline the same expression twice, which is how two "identical" fallbacks drift apart.
    """
    return spec.max_seeds if spec.max_seeds is not None else cfg.max_seeds


def effective_cold_start(spec: RowSpec, cfg: EngineConfig) -> str:
    """What this row does for a cold-start user: ``"popular"`` (top-rated fallback) or ``"skip"``.

    Row first, then the run's default — the same direction every other inheritable row setting
    resolves in. Anything unrecognised reads as ``"popular"``, so a bad value can only ever leave the
    pre-existing behaviour in place; it can never silently delete somebody's row.
    """
    value = spec.cold_start if spec.cold_start is not None else cfg.cold_start
    return "skip" if value == "skip" else "popular"


def effective_seed_window(spec: RowSpec) -> int:
    """How many recent watches this row may cycle between. 1 = always the most recent.

    No global to inherit, unlike `max_seeds`: whether a row rotates is part of what that row is.
    """
    return max(1, spec.seed_window or 1)


def _run_year(run_day: int) -> int:
    """The calendar year this run belongs to — what "how old is this title" is measured against.

    Derived from ``run_day`` rather than read from the clock so ranking stays reproducible and the
    whole run agrees with itself: a run that starts at 23:59 on 31 December must not age half the
    roster's candidates against one year and half against the next.

    ``run_day`` is 0 for a direct engine call (the server always sets it). Falling back to today
    rather than to 0 keeps the setting working for CLI and library callers — `recency_factor` reads
    a 0 as "no opinion", which would have made this silently do nothing there.
    """
    return date.fromordinal(run_day).year if run_day > 0 else date.today().year


def seed_cycle_offset(row_slug: str, owner_slug: str, run_day: int) -> int:
    """Which step of the cycle a row is on tonight.

    A STABLE crc32 phase, never Python's per-process-salted ``hash`` — the same reasoning as
    ``_is_refresh_night``: a phase that moved every restart would re-pick the seed on every run and
    rebuild the row each time. Adding the phase to the day staggers people, so a server does not
    re-derive every cycling row on the same night.
    """
    return run_day + zlib.crc32(f"{row_slug}|{owner_slug}".encode())


def effective_recency(spec: RowSpec, cfg: EngineConfig) -> float:
    """How much this row weights a title's release date: its own value, else the run's.

    Module-level for exactly the reason ``effective_max_seeds`` is. The shared-row path resolves its
    dials independently of the per-person one, and this setting shipped resolved on ONE of them: a
    shared row read the global and silently ignored its own stored value, in both directions — an
    override did nothing, and an explicit 0.0 "Hidden Gems" opt-out was overridden back to new — while
    the editor still offered the control and the row card still badged the override.

    ``is not None``, not truthiness: a stored 0.0 is a choice ("ignore release date on THIS row"),
    not an absent one, and collapsing the two is what makes a high global silently win.
    """
    return spec.recency if spec.recency is not None else cfg.recency


def effective_recent_count(spec: RowSpec, cfg: EngineConfig) -> int:
    """How many recent watched titles the web-search source searches for this row: its own budget,
    else the run's. Module-level for the same reason as ``effective_max_seeds`` — a per-person row
    layers a row_override on top of this (see ``RowPolicy.effective_recent_count``), which a shared
    row has no per-user override to layer, so it uses this directly.
    """
    return spec.recent_count if spec.recent_count is not None else cfg.recent_count


def _sections_of(ctx: EngineContext, library_keys: list) -> dict[int, str]:
    """ratingKey -> section key, for the given libraries (all of them when none are pinned).

    Built by inverting the per-section tmdb->ratingKey index the run already holds, so it costs no
    extra Plex reads. Lets a WATCHED title be traced back to the library it lives in.
    """
    wanted = {str(k) for k in library_keys}
    out: dict[int, str] = {}
    for section_key, index in ctx.section_index.items():
        if wanted and str(section_key) not in wanted:
            continue
        for rating_key in index.values():
            out[rating_key] = str(section_key)
    return out


def _history_for_row(ctx: EngineContext, history: list[WatchedItem], spec: RowSpec) -> list[WatchedItem]:
    """The watches that should SEED this row: the ones from the libraries it delivers into.

    A row's libraries used to narrow only what could be delivered, never what was searched — so a
    Movies row on a server whose owner mostly watches sport and TV spent all `max_seeds` slots on
    titles it could never deliver, TMDB returned more of the same, the library intersection threw
    nearly all of it away, and the row came back thin and reported "ok" (issue #1 follow-up).

    Filtering BEFORE `derive_seeds` is what makes the fix work: the seed budget is then filled from
    the relevant watches, looking as far back through the history as it needs to.

    Falls back to the unfiltered history when nothing survives — a weak row beats no row, and that
    is exactly what this person would have got before.
    """
    by_media = _media_filter(history, spec.media)
    if not spec.library_keys:
        return by_media
    sections = _sections_of(ctx, spec.library_keys)
    in_library = [w for w in by_media if w.rating_key is not None and w.rating_key in sections]
    if in_library:
        return in_library
    logger.debug(
        "row '{}': nothing in this person's history comes from its libraries — seeding from all of it",
        spec.slug,
    )
    return by_media


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


# How many episodes watched = the person is clearly watching this show, not discovering it. The
# ``show_pct`` fraction alone is unreachable for a long RETURNING series: it keeps adding episodes, so
# watched/total never hits 80% even for someone 160 episodes deep (SFLIX/MooHouse Gold Rush 160/226 =
# 71%; 2026-07-20). A per-show floor catches those — someone that far in has plainly seen it, not
# sampled it, however many unaired-then-aired seasons pushed the total up.
#
# The floor SCALES with series length rather than being flat. 3 episodes = "given it a real try" for a
# limited series; but 3 of a 200-episode run is 1.5%, still plainly a discovery. ``_ENGAGED_FRACTION``
# lifts the floor toward ~15% of length for long shows (200 eps -> 30) while ``_ENGAGED_EPISODES`` holds
# the 3-episode minimum for short ones. The counts are Plex's own per-user ``viewedLeafCount`` (marks
# included), so this no longer has to over-count to compensate for invisible marks (was issue #12).
_ENGAGED_EPISODES = 3
_ENGAGED_FRACTION = 0.15


def _engaged_floor(total: int) -> float:
    """Episodes watched at which a show counts as 'engaged, not a fresh pick', scaled to its length."""
    return max(_ENGAGED_EPISODES, total * _ENGAGED_FRACTION)


def _watched_titles(
    watched_movies: set[int],
    watched_shows: dict[int, tuple[int, int | None]],
    show_pct: float,
) -> set[tuple[int, MediaType]]:
    """The (tmdb_id, media_type) titles this person has already watched — the ones a watched-cap counts.

    Every watched movie, plus every show they've clearly watched: seen to >= ``show_pct`` of its
    episodes, OR watched a length-scaled "engaged" floor of them (``_engaged_floor``). A returning
    series that keeps airing never reaches the fraction, so the floor is what catches a person 160
    episodes deep; scaling it with length stops 3 episodes of a 200-episode run counting as finished.
    For a short series the ``show_pct`` fraction is the tighter bar, so ``min`` keeps it strict there.

    Args:
        watched_movies: tmdb_ids of watched movies (each is finished on its own).
        watched_shows: ``tmdb_id -> (viewed_leaf_count, leaf_count)`` — the user's own watched-episode
            count and the show's total, straight from Plex. A show whose total is unknown (None/0) is
            counted as watched rather than risk re-surfacing one they've worked through.
        show_pct: The finished fraction (0..1).
    """
    finished: set[tuple[int, MediaType]] = {(tid, MediaType.MOVIE) for tid in watched_movies}
    for tid, (viewed, total) in watched_shows.items():
        if not total or viewed >= min(total * show_pct, _engaged_floor(total)):
            finished.add((tid, MediaType.SHOW))
    return finished


def _apply_watched_cap(
    picks: list[Pick],
    candidates: list[Candidate],
    watched: set[tuple[int, MediaType]],
    k: int,
    pct: float,
) -> list[Pick]:
    """Keep at most ``floor(k * pct)`` already-finished picks; backfill freed slots with fresh ones.

    The row shows unwatched titles first and lets at most ``pct`` of it be things the person has
    already finished. Only used when ``pct`` > 0 — at 0 the pool already excludes finished titles.
    Backfill prefers fresh candidates the curator didn't pick; it re-admits finished ones only if
    the row still can't reach ``k`` and the cap has room.
    """
    max_watched = int(k * pct)  # floor: 20% of a 15-row is 3
    kept: list[Pick] = []
    watched_kept = 0
    for pick in picks:
        if (pick.tmdb_id, pick.media_type) in watched:
            if watched_kept >= max_watched:
                continue  # over the cap — drop, backfill below
            watched_kept += 1
        kept.append(pick)
    if len(kept) < k:
        fresh = [c for c in candidates if (c.tmdb_id, c.media_type) not in watched]
        room = max_watched - watched_kept
        spare_watched = [c for c in candidates if (c.tmdb_id, c.media_type) in watched][: max(0, room)]
        kept = _pad_picks(kept, [*fresh, *spare_watched], k)
    return [replace(p, rank=i + 1) for i, p in enumerate(kept)]


def _prefer_watched(
    picks: list[Pick],
    candidates: list[Candidate],
    watched: set[tuple[int, MediaType]],
    k: int,
) -> list[Pick]:
    """Order already-finished picks FIRST, then fill the rest with unwatched ones — a rewatch row.

    The inverse of ``_apply_watched_cap``, which treats finished titles as a regrettable ceiling. Here
    they are the point, so a thin rewatch pool degrades by topping up with fresh titles rather than
    returning a short row: a half-full shelf reads as broken, and every title still came from this
    person's own candidates.
    """
    seen = [p for p in picks if (p.tmdb_id, p.media_type) in watched]
    fresh = [p for p in picks if (p.tmdb_id, p.media_type) not in watched]
    kept = [*seen, *fresh][:k]
    if len(kept) < k:
        # TWO passes, not one combined list. `_pad_picks` -> `build_picks` -> `diversify_by_seed`
        # round-robins one candidate per seed queue, so concatenating [watched, fresh] and padding once
        # does NOT prefer the watched ones: rewatches tend to share a seed while fresh titles spread
        # across many, and the interleave then fills most slots with fresh titles. Exhausting the
        # watched spares in their own call is the only way the preference survives.
        chosen = {(p.tmdb_id, p.media_type) for p in kept}
        spare_watched = [
            c for c in candidates if (c.tmdb_id, c.media_type) in watched and (c.tmdb_id, c.media_type) not in chosen
        ]
        kept = _pad_picks(kept, spare_watched, k)
        if len(kept) < k:
            chosen = {(p.tmdb_id, p.media_type) for p in kept}
            spare_fresh = [
                c
                for c in candidates
                if (c.tmdb_id, c.media_type) not in watched and (c.tmdb_id, c.media_type) not in chosen
            ]
            kept = _pad_picks(kept, spare_fresh, k)
    return [replace(p, rank=i + 1) for i, p in enumerate(kept)]


def _started_shows(watched_shows: dict[int, tuple[int, int | None]]) -> set[tuple[int, MediaType]]:
    """Shows this person has watched ANY episode of — what an "unstarted only" row must exclude.

    Deliberately not ``_watched_titles``: that one asks "have they finished it", so a show they are
    three episodes into passes. Here a single viewed episode disqualifies it.
    """
    return {(tid, MediaType.SHOW) for tid, (viewed, _total) in watched_shows.items() if viewed and viewed > 0}


_KEEP_FRACTION = 2 / 3  # on a refresh night, keep the strongest ~two-thirds; swap the weakest third


def _is_refresh_night(row_slug: str, owner_slug: str, run_day: int, refresh_days: int) -> bool:
    """Whether this row rebuilds today, vs redelivering last run's picks unchanged.

    ``refresh_days`` IS the cadence, in days: 0 = never refresh once built (a frozen, pinned row),
    1 = every night, N = every N days. It used to be a 0..1 "freshness" fraction stretched onto
    1..14 days by a ``_refresh_period_days`` curve, which meant the stored number said nothing about
    the behaviour (0.55 → 7 days), the scale's far end was a constant duplicated in TypeScript, and
    no cadence slower than a fortnight could be expressed at all. Migration 0065 converted every
    stored fraction through that same curve, so no row's cadence moved.

    A per-(row, owner) phase — a STABLE crc32, never Python's per-process-salted ``hash`` — spreads
    refreshes across the cycle so the whole server never re-curates (and re-writes to Plex) on one
    night. ``run_day <= 0`` (direct engine calls and tests, which pass no day) always refreshes,
    preserving the pre-cadence behaviour.
    """
    if run_day <= 0:
        return True
    if refresh_days <= 0:
        return False
    if refresh_days == 1:
        return True
    phase = zlib.crc32(f"{row_slug}|{owner_slug}".encode()) % refresh_days
    return run_day % refresh_days == phase


def _reusable_prior(
    prior: list[Pick],
    kind: MediaType,
    sec_idx: dict[int, int],
    watched: set[tuple[int, MediaType]],
    pct: float,
    *,
    keep_watched: bool = False,
    started: frozenset[tuple[int, MediaType]] = frozenset(),
) -> list[Pick]:
    """Last run's picks for this library still valid to redeliver, in their original rank order: right
    media type, still in the library, and — for a 0%-watched row — not since watched.

    "Watched" here is whatever `RowPolicy.zero_pct_exclusions` says, which since 1.2 means STARTED,
    not merely finished. Without that, a show someone began after the last run kept its place in
    their row until the next rebuild — however long that row's `refresh_days` cadence is.

    ``keep_watched`` exempts a REWATCH row from that last rule. Its picks are already-finished titles
    by design, so the 0%-row filter discarded every one of them — and since a rewatch row inherits the
    default `watched_pct` of 0.0, that was the normal case, not an edge one. The row then never took a
    carry-forward branch at all: its `refresh_days` was inoperative, the anti-immediate-repeat guard never
    ran, and it re-wrote to Plex on nights nothing had changed.

    ``started`` drops shows the person has begun, and is checked at EVERY `pct`, unlike the rule
    above. An "only series they haven't started" row above 0% took no filter at all here — and above
    0% is exactly the configuration the row editor recommends for it ("this only changes anything if
    you've allowed already-watched titles above"). So the row went on showing a series they had since
    started, for up to a fortnight, in its documented setup.
    """
    out: list[Pick] = []
    for p in prior:
        if p.media_type is not kind or p.tmdb_id not in sec_idx:
            continue
        if pct <= 0 and not keep_watched and (p.tmdb_id, p.media_type) in watched:
            continue
        if (p.tmdb_id, p.media_type) in started:
            continue
        out.append(p)
    return out


def _names_a_seed(spec: RowSpec, user: UserProfile, config: EngineConfig) -> bool:
    """Whether this row's TITLE claims a particular watch, and so has to keep answering to it.

    Reads the EFFECTIVE template, not `spec.name_template`. The DEFAULT row's own column is blank on
    purpose — its title comes from the global `row.name_template`, which is what the wizard and
    Settings edit (`context_builder`) — so testing the column alone answered "no" for the one row
    every new install starts with, and the wizard offers "Because you watched {top_seed}" for
    exactly that row. It was then neither forced nightly nor rebuilt when its seed moved: the row
    kept a title naming a watch it was no longer built from, which is the whole bug this guards
    (issue #57). Worse, the editor computes the same claim from the effective name, so it HID the
    cadence control and promised "every night" for a row the engine refreshed every eight days.

    `resolve_row_template` is the single source of truth for that precedence, and delivery renders
    the delivered title through it too — so this now asks the same question the title answers.
    """
    return "{top_seed}" in resolve_row_template(spec, user, config)


def _seed_moved(
    spec: RowSpec, prior_valid: list[Pick], sub: list[Candidate], user: UserProfile, config: EngineConfig
) -> bool:
    """Whether a row NAMED after its seed is now built from a different one than last run's picks.

    A `{top_seed}` title renders from the LOWEST-RANKED pick's ``seed_title`` (``render_row_name``
    takes ``min(picks, key=rank)``, never ``picks[0]`` — display order is not rank), and the
    refresh branch always carries pick #1 forward — so without this the title stays pinned to the seed
    of the very FIRST build while later refreshes quietly fill the row's tail from newer watches. The
    row then claims "Because you watched X" over contents mostly chosen for something else, which is
    exactly what a one-seed row exists to prevent (measured on a real run: history seeded only by
    Fargo still delivered "Because you watched Chernobyl").

    Keyed on the NAME making the claim, not on a seed budget, so the two-seed `media=both` named row
    is covered too. Rows that name no seed keep the cheap carry-forward — re-deriving them on every
    seed drift would turn a normal 30-seed row's refresh into a near-total rebuild.

    Note what this does NOT promise above one seed. It asks whether the POOL still leads with the
    named seed; the title renders from the best-matching DELIVERED pick (`render_row_name` takes the
    lowest `rank`). Re-ranking survivors against newcomers can hand the lead to a differently-seeded
    newcomer while the pool's own top seed never moved, and the row then renames itself to match. That
    is the title correctly following its lead rather than a stale claim — the two only diverge above
    one seed, where a `{top_seed}` row already names its strongest watch while holding others.
    """
    if not _names_a_seed(spec, user, config) or not prior_valid or not sub:
        return False
    current = sub[0].top_seed
    return prior_valid[0].seed_tmdb_id != (current.tmdb_id if current else None)


def _rated_by_source(picks: list[Pick], ctx: EngineContext) -> dict[tuple[int, MediaType], float] | None:
    """The configured service's score for each pick, for a row about to be ordered by rating — or
    None to order on the TMDB score every pick already carries.

    Returns an OVERRIDE MAP rather than rewriting `Pick.rating`. The rating on a Pick is TMDB's, is
    persisted as such, and comes back on every carried-forward pick next run; overwriting it made a
    fallback impossible to honour, because a refresh night mixes carried picks (holding last run's
    MDBList score) with newcomers (holding TMDB's) and nothing could tell them apart. Sorting one row
    on two services' scales is worse than sorting it on either.

    One MDBList lookup per title, cached for a week and shared across every row and user, so a warm
    cache costs nothing and a cold one costs one call per distinct title. Only the picks that survived
    into a row are looked up — never the whole candidate pool.

    Degrades rather than fails, because an order is cosmetic and a run is not:

    * no MDBList key configured -> None (TMDB), the setting's own documented fallback;
    * quota spent (429) -> None for this row AND every later row this run (`mdblist_rate_limited`
      latches), so a spent quota costs one failed call rather than one per row per user;
    * a title that service has no score for -> 0.0, which `_apply_order` already sorts last.
    """
    source = ctx.config.rating_source
    if source == "tmdb" or ctx.mdblist is None or not picks or ctx.mdblist_rate_limited:
        return None
    overrides: dict[tuple[int, MediaType], float] = {}
    for pick in picks:
        try:
            found = ctx.mdblist.rating(pick.tmdb_id, pick.media_type, source)
        except MdbListRateLimitError:
            # Latched for the whole run: without this every remaining rating-ordered row for every
            # user re-attempts, and each attempt is retried three times honouring Retry-After (up to
            # 60s) — minutes of stall for a result that is discarded anyway.
            ctx.mdblist_rate_limited = True
            logger.warning(
                "MDBList daily quota spent — ordering rows by TMDB score instead of {} for the rest of "
                "this run (the quota resets daily)",
                source,
            )
            return None
        except Exception as e:  # one lookup hiccup makes that title unrated, never breaks the row
            logger.debug("MDBList lookup failed for {} ({}) — treating as unrated", pick.title, e)
            found = None
        overrides[(pick.tmdb_id, pick.media_type)] = found[0] if found else 0.0
    return overrides


ROW_ORDERS = ("best", "rating", "newest", "shuffle", "new_first", "rotate")


def _apply_order(
    picks: list[Pick],
    order: str,
    *,
    row_slug: str,
    user_slug: str,
    run_day: int,
    ratings: dict[tuple[int, MediaType], float] | None = None,
    new_keys: set[tuple[int, MediaType]] | None = None,
) -> list[Pick]:
    """Order a row's picks for delivery. ``best`` (the default) leaves the ranking alone.

    Applied to EVERY path — carried-forward, refreshed, freshly built and cold-start — so changing a
    row's order takes effect on the next run without rebuilding it. Ordering is presentation, not selection: it
    decides how the same picks are arranged, so it never needs a re-curate.

    ``shuffle`` is deliberately NOT random: it is a stable hash of (row, user, day), so a row shifts
    day to day but a re-run on the same night reproduces the same order. A genuinely random order
    would rewrite the collection on every retry and could never be asserted in a test.

    ``new_first`` leads with the titles that arrived THIS run (in their own rank order), then the
    survivors in theirs — issue #63's "push new recommendations to the front". `new_keys` is what
    makes a pick "new"; an empty/None set means nothing arrived, so the order is a no-op. That is the
    correct answer on the two paths where it happens: a carried-forward night has no newcomers, and a
    bootstrap build is ALL newcomers, which leaves the ranking's order either way.

    ``rotate`` answers the same issue's "cycle the ones at the top off" — but as a cyclic shift of the
    display order, NOT as eviction. Eviction belongs to the refresh branch (weakest third out, every
    refresh nights); expressing it here instead would need a second, persisted notion of position
    and would collide with `rank`, which means match quality and is what `render_row_name` and
    `_seed_moved` read. Rotating gives what the request was actually after — every title gets a turn
    at the front, in the ranking's relative order — and gives it EVERY night rather than once per
    refresh cycle, because this function runs on the carried-forward path too.

    Picks missing the value being sorted on (``rating``/``year`` are None or 0 on rows delivered
    before those were recorded) sort last but keep their relative order, so such a row degrades to
    its existing ranking for one cycle rather than scrambling.

    ``ratings`` replaces the score used by the ``rating`` order — see ``_rated_by_source``. It is all
    or nothing on purpose: a partial map would mix two services' scales inside one row.
    """
    if order == "rating":
        # `ratings` overrides the TMDB score a Pick carries when the owner chose another service; it
        # covers every pick or none, so a row is never sorted on two services' scales (see
        # `_rated_by_source`).
        def score(pick: Pick) -> float:
            if ratings is not None:
                return ratings.get((pick.tmdb_id, pick.media_type), 0.0)
            return pick.rating or 0.0

        return sorted(picks, key=lambda p: -score(p))
    if order == "newest":
        return sorted(picks, key=lambda p: -(p.year or 0))
    if order == "shuffle":
        return sorted(picks, key=lambda p: _shuffle_key(row_slug, user_slug, run_day, p.tmdb_id))
    if order == "new_first":
        # Stable, so both groups keep the ranking's relative order inside themselves.
        arrived = new_keys or set()
        return sorted(picks, key=lambda p: 0 if (p.tmdb_id, p.media_type) in arrived else 1)
    if order == "rotate":
        if not picks:
            return picks
        # `run_day` and not a stored cursor: the front advances by the calendar, so a night that is
        # skipped (a failed run, a paused server) never leaves the row stuck on one title, and a
        # re-run on the same night reproduces the same order — the same property `shuffle` needs.
        offset = run_day % len(picks)
        return picks[offset:] + picks[:offset]
    return picks


def _shuffle_key(row_slug: str, user_slug: str, run_day: int, tmdb_id: int) -> int:
    """A stable per-(row, user, day, title) sort key for the ``shuffle`` order.

    blake2b, NOT the ``zlib.crc32`` used for the refresh phase, and not Python's ``hash`` (salted per
    process). CRC32 is LINEAR: every key here differs only in the day field, at the same offset, so
    incrementing the day XORs one identical constant into all of a row's checksums — which frequently
    leaves their relative order untouched. Measured before this was fixed: day 5 and day 6 shuffled a
    five-title row into the exact same sequence, i.e. the row would not have moved at all.
    """
    digest = hashlib.blake2b(f"{row_slug}|{user_slug}|{run_day}|{tmdb_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def row_recipe(policy: RowPolicy, spec: RowSpec) -> str:
    """A fingerprint of the settings that decide a row's CONTENTS, for change detection.

    Freshness exists to suppress churn when nothing has changed — not to delay a change the owner
    deliberately made. Without this, editing a setting that decides which titles a row holds waits
    behind that cadence: on a real server, raising "Recent releases" left 36 of 42 rows redelivering
    byte-identical picks for up to a fortnight, which is indistinguishable from the feature not
    working. `_seed_moved` already sets the precedent — the row's premise changed, so rebuild it.

    Contents only. `pick_order` decides presentation and `refresh_days` is the cadence itself, so
    neither belongs here: reordering a row must not force a rebuild, and changing the cadence must
    not trigger the very rebuild the cadence is there to schedule. Row NAME, poster and placement are
    likewise excluded — they change what a row looks like, never which titles are in it.

    EFFECTIVE values, not stored ones, so raising a global invalidates every row that inherits it.
    """
    return "|".join(
        str(part)
        for part in (
            spec.media,
            ",".join(sorted(str(k) for k in spec.library_keys)),
            ",".join(policy.effective_sources(spec)),
            policy.effective_recency(spec),
            policy.effective_watched_pct(spec),
            bool(spec.rewatch),
            bool(spec.unstarted_only),
            effective_max_seeds(spec, policy.cfg),
            effective_seed_window(spec),
            effective_cold_start(spec, policy.cfg),
        )
    )


def _rank_against_pool(picks: list[Pick], sub: list[Candidate]) -> list[Pick]:
    """Re-rank a refresh night's survivors and newcomers together, by the pool's CURRENT order.

    ``sub`` is already best-first (``ranking.pre_rank`` output), so a title's index in it is tonight's
    ranking. Without this the refresh branch concatenated ``kept + new``, which pinned last run's top
    two-thirds to the head of the row for ever: a newcomer could never place above a survivor however
    much better it scored, so on a 20-title row positions 1-13 never moved again. `ranking` already
    promises "the single best-scoring title is always kept first" — this is what makes that true once
    a row has history.

    A pick no longer in the pool sorts to the END, keeping its relative order there (Python's sort is
    stable) — it is still a valid delivery, just no longer a ranked candidate tonight. The caller
    then truncates to `k`, so such a pick is the first to lose its slot when the row is over-full,
    which is the right precedence: a ranked candidate outranks one the pool no longer holds.

    ORDERING ONLY. It must not decide MEMBERSHIP: sorting the merged list by pool index and then
    truncating to `k` evicts exactly the picks `diversify_by_seed` fought for, because pool order is
    pure score and a heavily-watched title's look-alikes dominate it. The caller therefore chooses
    its `k` first and hands only those here — see the refresh branch in `_build_section_picks`.
    """
    order = {(c.tmdb_id, c.media_type): i for i, c in enumerate(sub)}
    return sorted(picks, key=lambda p: order.get((p.tmdb_id, p.media_type), len(order)))


def _rating_key_resolver(seed_index: dict[int, int]) -> Callable[[WatchedItem], int | None]:
    """A resolver from a watched item to its tmdb_id, via ratingKey, across EVERY library.

    A user's watches resolve against every library, not just the delivery ones: what they watched in
    a second movie library is still what they watched.

    `seed_index` is keyed by ratingKey for that reason. Inverting a tmdb_id -> ratingKey index here
    instead would silently drop libraries: the same film in "Movies" and "4K Movies" has ONE tmdb id
    and TWO ratingKeys, so only the last library scanned would survive the inversion — and every
    watch in the other one would resolve to nothing, leaving the user seedless and their row empty.
    """

    def resolve(item: WatchedItem) -> int | None:
        return seed_index.get(item.rating_key) if item.rating_key else None

    return resolve


def _stamp_disposition(
    gather_stats: candidates_mod.GatherStats,
    *,
    dropped: list[tuple[Candidate, str]],
    in_library: list[Candidate],
    ranked: list[Candidate],
    recency: float = 0.0,
    year_now: int = 0,
) -> None:
    """Annotate the gather trace with each candidate's FATE, so the operator can follow every title
    from a source's returns to the row (or to the reason it fell out).

    Reads only the lists selection already produced (``dropped`` from filter_candidates, ``in_library``,
    ``ranked``) — it computes nothing new about which candidates win and mutates none of them. Two
    things are written onto ``gather_stats.trace``:

    * a per-source ``disposition`` tally: ``{kept, already_watched, not_in_your_libraries,
      excluded_genre, lost_ranking_cutoff}`` counts, and
    * a ``fate``/``fate_reason`` on each already-recorded per-seed return, keyed by tmdb_id, and
    * the numbers that DECIDED that fate — the title's year, its rating, and the release-date weight
      applied to it. Without them the trace could say a title lost the cut but never why, so "it
      picked a 2003 film over a 2024 one" had no answer on the page built to answer it.

    A candidate that survived filtering but lost the ``candidates_pre_rank`` cut is
    ``lost_ranking_cutoff``; one that made the pre-rank is ``kept`` (whether or not it ends in the
    final row — the per-library row build, downstream of here, decides that and is traced separately
    by the delivered-picks stage).

    KNOWN GAP: this is stamped once per POOL, at the server's ``recommendations.recency``. A row that
    OVERRIDES that weight re-takes its own cut (``RowPolicy.cut_at_recency``) after this has run, so
    for that row the two disagree — a title it delivers can read ``lost_ranking_cutoff`` here. Every
    row that inherits the global (the default, and every row on a server that never overrides it) is
    stamped exactly right. Fixing it properly means stamping per row, which needs ``dropped`` carried
    past the gather; until then the delivered-picks stage remains the authority on what actually
    landed in a row.
    """
    ranked_ids = {(c.tmdb_id, c.media_type) for c in ranked}
    in_library_ids = {(c.tmdb_id, c.media_type) for c in in_library}
    # Every candidate we know anything about, dropped ones included — a title filtered out still has
    # a year worth showing next to the reason it went.
    known: dict[tuple[int, MediaType], Candidate] = {
        (c.tmdb_id, c.media_type): c for c in [*(cand for cand, _ in dropped), *in_library]
    }
    drop_reason: dict[tuple[int, MediaType], str] = {}
    for cand, reason in dropped:
        drop_reason.setdefault((cand.tmdb_id, cand.media_type), reason)

    def fate_of(tmdb_id: int, media: MediaType) -> str:
        key = (tmdb_id, media)
        if key in ranked_ids:
            return "kept"
        if key in in_library_ids:
            return "lost_ranking_cutoff"  # survived filtering but lost the pre-rank cut
        # Defensive fallback: a returned id with no matching pooled candidate. Shouldn't occur —
        # every returned title is added to the pool, so it resolves to a real fate above.
        return drop_reason.get(key, "not_returned")

    for source in gather_stats.trace.get("sources", []):
        tally: dict[str, int] = {}
        for query in source.get("queries", []):
            qmedia = MediaType.SHOW if query.get("media") == "show" else MediaType.MOVIE
            for ret in query.get("returned", []):
                tmdb_id = int(ret.get("tmdb_id") or 0)
                verdict = fate_of(tmdb_id, qmedia)
                ret["fate"] = verdict
                cand = known.get((tmdb_id, qmedia))
                if cand is not None:
                    ret["year"] = cand.year
                    ret["rating"] = round(cand.rating, 1) if cand.rating else None
                    # The age multiplier this title was actually judged with. 1.0 when the setting is
                    # off or the title has no known year, which is the honest answer in both cases —
                    # and the UI omits it rather than printing a meaningless "x1.0".
                    ret["age_weight"] = round(ranking.recency_factor(cand.year, year_now, recency), 3)
                tally[verdict] = tally.get(verdict, 0) + 1
        if tally:
            source["disposition"] = tally

    # The web-search source records its proposals under trace["web"], not as per-seed `queries`, so it
    # needs the same fate stamp separately: which AI-proposed titles made this library's shortlist vs
    # fell out. Hallucinations (no TMDB match) never reach `proposals`, so they carry no fate — the UI
    # still strikes them through from the `unresolved` list.
    for proposal in gather_stats.trace.get("web", {}).get("proposals", []):
        pmedia = MediaType.SHOW if proposal.get("media") == "show" else MediaType.MOVIE
        proposal["fate"] = fate_of(int(proposal.get("tmdb_id") or 0), pmedia)


def row_library_index(
    ctx: EngineContext,
    spec: RowSpec,
    library_index: dict[MediaType, dict[int, int]],
) -> dict[MediaType, dict[int, int]]:
    """What THIS row may recommend: the index of the libraries it actually delivers into.

    An unpinned row keeps the union index (a title in any library of its type is deliverable). A row
    pinned to `library_keys` must be narrowed to those libraries — it was curated against the union,
    so a row pinned to a 200-title "Kids Movies" was choosing from the whole 5000-title movie
    catalogue, and delivery then dropped every pick that library didn't hold: a one-item row, or no
    row at all, reported as ok.
    """
    if not spec.library_keys:
        return library_index
    narrowed: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in sections_for_keys(ctx.delivery_sections, spec.library_keys):
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        narrowed[kind].update(ctx.section_index.get(section.key, {}))
    return narrowed


def _candidate_pool(
    ctx: EngineContext,
    seeds: list,
    library_index: dict[MediaType, dict[int, int]],
    *,
    excluded_genres: set[str],
    profile=None,
    sources: list[str] | None = None,
    media: str = "both",
    watched_exclusions: set[tuple[int, MediaType]] | None = None,
    recent_count: int | None = None,
    recency: float = 0.0,
) -> tuple[tuple[list[Candidate], list[Candidate], list[Candidate]], candidates_mod.GatherStats]:
    """Gather TMDB candidates for ``seeds`` and intersect them with the library.

    Returns ``((pool, in_library, ranked), gather_stats)`` — the 3-tuple of candidate lists, plus the
    AI token/Exa spend the gather incurred (for per-run cost accounting):

    * ``pool`` — every pooled candidate (used for request-demand bookkeeping before narrowing).
    * ``in_library`` — the ones the delivery libraries actually hold and this user may still see.
    * ``ranked`` — the pre-ranked candidates the curator chooses from.

    ``media`` narrows the pool BEFORE the pre-rank truncation. Filtering after it meant a
    movie-heavy watcher's shows-only row could lose every show to the 40-candidate cut and deliver
    nothing — a dead row on a green run. Identity is (tmdb_id, media_type), never the bare id — movie
    1399 and TV 1399 are different titles.

    (No staleness partition anymore: rows now carry their prior picks forward on non-refresh nights,
    so there's nothing to "hold back" — see ``_reusable_prior`` / ``_is_refresh_night``.)
    """
    # The titles this person has already watched (per the row's policy), not just the ~30 seeds — a
    # recommendation you've finished is the exact thing the row shouldn't surface. Falls back to the
    # seed set for callers that don't compute the full breakdown (e.g. shared rows).
    watched_ids = watched_exclusions if watched_exclusions is not None else {(s.tmdb_id, s.media_type) for s in seeds}
    # Blocked titles are dropped at SEED DERIVATION only (`derive_seeds(..., blocked=...)`, called by
    # this row's caller before `seeds` reaches here) — nothing downstream re-checks `blocked_seeds`,
    # so a blocked title may still surface if a different seed's similar-titles search suggests it.
    # Matches the UI's "Don't seed" wording, which promises exactly that and no more.
    gather_stats = candidates_mod.GatherStats()
    pool = candidates_mod.gather_candidates(
        ctx.tmdb,
        seeds,
        sources=sources if sources is not None else ctx.config.candidate_sources,
        curator=ctx.curator,
        profile=profile,
        trakt=ctx.trakt,
        search=ctx.search,
        web_search_mode=ctx.config.web_search_provider,
        web_search_cache=ctx.web_search_cache,
        recent_count=recent_count if recent_count is not None else ctx.config.recent_count,
        stats=gather_stats,
    )
    # `dropped` collects (candidate, reason) as filter_candidates works — observation only, it does
    # not change which candidates are kept.
    dropped: list[tuple[Candidate, str]] = []
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=excluded_genres,
        dropped=dropped,
    )
    in_library = _media_filter(valid, media)
    # Pre-rank EACH media type to its own cap, not the mixed pool to one cap — otherwise a 'both'
    # row whose pool skews one way (a mostly-TV watcher) truncates the other type away before the
    # per-media curate ever sees it, and that library's collection comes up empty.
    kinds = [MediaType.MOVIE, MediaType.SHOW] if media == "both" else [MediaType(media)]
    cap = ctx.config.candidates_pre_rank
    # `recency` is the weight the CALLER resolved, not `ctx.config.recency`. The per-person path
    # passes the server's value so every row that inherits it shares one cached cut (and re-cuts only
    # when it overrides); the shared-row path — which has no such cache — passes the row's own
    # `effective_recency` and gets the right cut first time.
    ranked = ranking.cut_for_recency(in_library, kinds, cap, recency, _run_year(ctx.run_day))
    # Stamp each traced return with its fate (kept as a candidate, or dropped and why), derived
    # entirely from the lists selection already produced above — so the trace can follow every title
    # in and out without altering a single delivered pick.
    _stamp_disposition(
        gather_stats,
        dropped=dropped,
        in_library=in_library,
        ranked=ranked,
        recency=recency,
        year_now=_run_year(ctx.run_day),
    )
    return (pool, in_library, ranked), gather_stats


def _add_step_tokens(report: UserRunReport, step: str, n: int) -> None:
    """Accumulate ``n`` AI tokens under a WHERE-it-went bucket on the user's report (no-op for 0)."""
    if n:
        report.llm_tokens_by_step[step] = report.llm_tokens_by_step.get(step, 0) + n


def _record_gather(report: UserRunReport, stats: candidates_mod.GatherStats, *, pool_label: str | None = None) -> None:
    """Fold a candidate-gather's AI cost into the user report: per-source tokens (also into the grand
    total), Exa searches, and Exa cache hits. Called once per pool COMPUTATION — a cache hit re-adds
    nothing to tokens, but IS counted in exa_cache_hits so the run shows what the cache saved.

    This is the ONLY AI cost now — the AI is used only to FIND titles (web search). Ranking the pool
    and writing each row's reason are done in code (``picker.build_picks``), so there is no per-row
    LLM spend to attribute anymore.

    ``pool_label`` names the pool this gather computed (e.g. "movie · Movies"); its trace is filed
    under ``report.trace["gathers"]`` so the UI can show what each distinct pool queried. Most users
    have a single pool shared by every row, so this is usually one entry.
    """
    for source, tokens in stats.tokens_by_source.items():
        report.llm_tokens += tokens
        _add_step_tokens(report, source, tokens)
    report.exa_searches += stats.exa_searches
    report.exa_cache_hits += stats.exa_cache_hits
    if stats.trace:
        report.trace.setdefault("gathers", []).append({"pool": pool_label or "", **stats.trace})


def _library_resolvers(ctx: EngineContext) -> tuple[Callable[[WatchedItem], str], Callable[[object], str]]:
    """Two lookups mapping a watch / a seed to the display NAME of the Plex library it lives in.

    Both are built from data the run already holds — no extra Plex reads. A server can have several
    movie or TV libraries with custom names, so the trace groups by real library, not media type
    alone (a "Movies" and a "4K Movies" library must not collapse into one). Returns ``("", "")``
    resolvers for anything unknown, which the UI falls back to a media-type label for.
    """
    section_titles = {str(s.key): getattr(s, "title", "") or "" for s in ctx.delivery_sections}
    rating_key_to_section = _sections_of(ctx, [])  # ratingKey -> section key, across all libraries
    tmdb_to_section = {  # tmdb_id -> section key (first library holding it; good enough for display)
        tmdb_id: str(section_key) for section_key, index in ctx.section_index.items() for tmdb_id in index
    }

    def library_of_watch(item: WatchedItem) -> str:
        return section_titles.get(rating_key_to_section.get(item.rating_key or -1, ""), "")

    def library_of_seed(s) -> str:
        return section_titles.get(tmdb_to_section.get(s.tmdb_id, ""), "")

    return library_of_watch, library_of_seed


def _record_cold_start_trace(report: UserRunReport, picks: list[Pick]) -> None:
    """File a minimal search stage for a COLD-START user: no TMDB/Trakt search ran, so the trace has
    no gathers — but without at least one it would be empty, the run page would show no "How we
    picked" button, and a cold user would look like they were skipped (they weren't). One synthetic
    ``cold_start`` source per media kind records what was pulled, so each library tab shows its own
    "Popular on this server" step. The delivered-picks stage shows the titles themselves.
    """
    counts: dict[MediaType, int] = {}
    for pick in picks:
        counts[pick.media_type] = counts.get(pick.media_type, 0) + 1
    for kind, count in counts.items():
        report.trace.setdefault("gathers", []).append(
            {
                "pool": f"{kind.value} · cold_start",
                "sources": [{"source": "cold_start", "status": "ok", "contributed": count, "detail": ""}],
            }
        )


_TRACE_HISTORY_SAMPLE = 40  # most recent watches to record in the trace (display only — full count is in counts)


def _record_history_trace(
    report: UserRunReport,
    history: list,
    specs: list[RowSpec],
    seeds_for,
    watched_movies: set[int],
    watched_shows: dict[int, tuple[int, int | None]],
    library_of_watch=lambda _item: "",
    library_of_seed=lambda _seed: "",
    ratings: RatingsPolicy | None = None,
) -> None:
    """File the history/seeds/watched stage of the trace: the most recent watches, the seeds derived
    from them (the widest set any row uses), and a watched summary. Display only.

    ``library_of_watch``/``library_of_seed`` resolve each item to its Plex library's display name so
    the UI can group by real library — a server can have several movie or TV libraries with custom
    names, so grouping by media type alone would be wrong. Both default to "" (unknown), which the UI
    falls back to a media-type label for.

    ``ratings`` is recorded, not applied — the seeds handed in were already filtered by it. It is
    here so a watch that did NOT become a seed can say why: a title silently missing from the seed
    list is the single hardest thing to explain about a run, and "they rated it 1 star" is the whole
    answer. It must be the SAME verdict the run filtered with, passed in rather than recomputed here:
    recomputing it from a threshold made the trace answer over the full history while the rows
    answered over their slices, so the explanation could name a title the run had kept, or stay
    silent about one it had dropped.

    Its summary is filed whole (`history.ratings`) because the OUTCOME cannot be read backwards from
    the watch list: no rated-out titles means the setting is off, or nobody rated anything low, or
    every rating on the account was tool-written and disbelieved — three very different runs that
    otherwise render as the same silence. Omitted entirely when no policy is passed, because the UI
    already degrades gracefully on a run that predates this: recording a DEFAULT there would publish
    "ratings were off" — a confident, possibly false statement — in place of "we didn't record it".

    Only ever per-person rows reach here. Shared rows are built by `_run_shared` from several people's
    pooled history, deliberately without `disliked` (one person's rating must not reshape a row
    everyone sees), and record no `history` stage at all (only `gathers`) — so this summary can never
    appear on one.
    """
    recent = sorted(history, key=lambda i: i.watched_at, reverse=True)[:_TRACE_HISTORY_SAMPLE]
    disliked = ratings.blocked if ratings else set()
    seeds = max((seeds_for(spec) for spec in specs), key=len, default=[])
    # True per-library watched totals over the FULL history — NOT the recent sample. The sample is
    # time-ordered and capped, so a heavy-TV watcher's Movies tab would sample only a handful of recent
    # movies; and a per-MEDIA total can't tell two same-type libraries apart (a "Movies" and a "4K
    # Movies" library would show the same number). Each watch is resolved to its library and distinct
    # titles are counted per media type (a show's episodes share one title, so they count once).
    by_library: dict[str, dict[str, set]] = {}
    for item in history:
        bucket = by_library.setdefault(library_of_watch(item), {"movie": set(), "show": set()})
        bucket[item.media_type.value].add(item.tmdb_id if item.tmdb_id is not None else item.title)
    report.trace["history"] = {
        "total": len(history),
        "recent": [
            {
                "title": i.title,
                "media": i.media_type.value,
                "library": library_of_watch(i),
                "year": i.year,
                "watched_at": i.watched_at.isoformat() if i.watched_at else None,
                # Their own Plex rating, 0..10, and whether it is what kept this watch out of the
                # seeds. `rating` is shown whenever they gave one; `rating_blocked` is the narrower
                # claim that it ACTED — false for a rating above the threshold, for a tool-written
                # one, and whenever the feature is off.
                "rating": i.user_rating,
                # The PAIR, matching what the exclusion is keyed on — an id-only lookup here would
                # label a show "rated low" because a movie shares its TMDB number, printing a
                # confident false reason next to a title the person never rated.
                "rating_blocked": i.tmdb_id is not None and (i.tmdb_id, i.media_type) in disliked,
            }
            for i in recent
        ],
        "watched_movies": len(watched_movies),
        "watched_shows": len(watched_shows),
        "watched_by_library": {
            lib: {"movie": len(b["movie"]), "show": len(b["show"])} for lib, b in by_library.items()
        },
    }
    if ratings is not None:
        # The rating policy this run actually used — not what Settings says now. A run is read weeks
        # later, and a setting changed since would otherwise rewrite the history of what happened.
        report.trace["history"]["ratings"] = {
            "enabled": ratings.enabled,
            "threshold": ratings.threshold,
            "trusted": ratings.trusted,
            # Both counted over the WHOLE history, unlike `rating_blocked` above, which can only speak
            # for the bounded recent sample. The UI has to say which scope it is quoting: the two
            # numbers on that card do not describe the same set of titles.
            "blocked": len(ratings.blocked),
            "rated": ratings.rated,
            "rated_human": ratings.rated_human,
        }
    report.trace["seeds"] = [
        {
            "title": s.title,
            "media": s.media_type.value,
            "library": library_of_seed(s),
            "tmdb_id": s.tmdb_id,
            "weight": round(s.weight, 3),
            # The two ingredients behind the weight, so the UI can say "watched 4x, last seen 3 days
            # ago" instead of an opaque bar — this is what makes the influence bar legible.
            "watch_count": s.watch_count,
            "recency_days": s.recency_days,
        }
        for s in sorted(seeds, key=lambda s: s.weight, reverse=True)
    ]


def _in_audience(user: UserProfile, spec: RowSpec) -> bool:
    return spec.audience is None or user.plex_account_id in spec.audience


def _is_muted(user: UserProfile, spec: RowSpec) -> bool:
    override = user.row_overrides.get(spec.slug)
    return bool(override and override.muted)


def builds_anything_for(user: UserProfile, cfg: EngineConfig) -> bool:
    """Whether this run will build any per-person row for this person.

    The same three conditions `_run_user` applies before it does any work — in the audience, not
    muted, and in this run's scope. Public because the server needs the answer BEFORE the run, to
    skip pre-filling watch history for someone the engine would only skip: a whole per-user PMS read
    saved. The gate belongs to the engine, so the server asks rather than keeping its own copy —
    a second copy is a rule that drifts silently the next time this one changes.

    Shared rows are excluded on purpose: they are built once for the server, not per person, so
    nobody's history is read on their account.
    """
    return any(
        _in_audience(user, spec) and not _is_muted(user, spec) and cfg.should_build(spec)
        for spec in cfg.per_person_rows()
    )


def _why_no_rows(user: UserProfile, cfg: EngineConfig) -> str:
    """Plain-English reason this person had no per-person row to build.

    "Skipped" on its own reads as a bug — a beta user turned their only row into a shared row, saw
    every user skipped with no collections created, and filed it as broken (issue #3). The answer is
    always in the configuration, so say which part of it.
    """
    per_person = cfg.per_person_rows()
    if not per_person:
        shared = len(cfg.shared_rows())
        return (
            f"There are no per-person rows to build. Every enabled row ({shared}) is a SHARED row, which is "
            "built once for the whole server from what several people have watched — not per person. "
            "Add a per-person row (Rows → New row) to give people their own."
            if shared
            else "No rows are enabled, so there was nothing to build."
        )
    if not any(_in_audience(user, spec) for spec in per_person):
        return "This person isn't in the audience of any per-person row."
    if all(_is_muted(user, spec) for spec in per_person if _in_audience(user, spec)):
        return "Every per-person row they're in is muted for this person."
    return "None of this person's rows were due to rebuild in this run."


def _why_cold_skipped(user: UserProfile, cfg: EngineConfig, specs: list[RowSpec], removed: int) -> str:
    """Plain-English reason a cold-start user got no row, for the same reason `_why_no_rows` exists:
    a bare status word reads as a failure, and this one is a deliberate setting.

    Args:
        user: The person, for their history count.
        cfg: The run's config, for the threshold they fell short of.
        specs: The rows this run was going to build for them — not every row they are in.
        removed: How many COLLECTIONS this skip deleted — a delta measured across it, never a total,
            since the muted/retired sweep appends to the same diff earlier in the run. A row lives in
            one collection per library, so this counts copies, and it is 0 for a row that was left
            alone (unrenderable title, no ledger key) or that had nothing on Plex to begin with.
    """
    # Scoped to THIS run's rows, not "every row they're in": each row has its own cron, so a
    # scheduled run usually carries one, and their other rows build on their own schedules.
    one_row = len(specs) == 1
    removal = ""
    if removed:
        copies = "copy" if removed == 1 else "copies"
        was = "was" if removed == 1 else "were"
        removal = f", so {removed} {copies} of {'it' if one_row else 'them'} already on Plex {was} removed"
    return (
        f"Not enough watch history yet — {len(user.history)} of {cfg.min_history} titles. "
        f"The {'row' if one_row else f'{len(specs)} rows'} due in this run "
        f"{'is' if one_row else 'are'} set to build nothing until then{removal}. "
        f"{'It comes back on its own' if one_row else 'They come back on their own'} "
        "once this person crosses the threshold."
    )


def _ledger_keys(ctx: EngineContext, user: UserProfile, spec: RowSpec) -> dict[str, int]:
    """{section key -> ratingKey} for THIS user's copy of THIS row, from the delivery ledger.

    The only handle on a `{top_seed}` row, whose title was different every run and so matches nothing
    computed from config. See `remove_row` for why identity is safe here.
    """
    return {
        section_key: key
        for (slug, row_slug, section_key), key in ctx.delivered_keys.items()
        if slug == user.slug and row_slug == spec.slug
    }


def _drop_cold_skipped_rows(
    ctx: EngineContext,
    user: UserProfile,
    cfg: EngineConfig,
    specs: list[RowSpec],
    report: UserRunReport,
) -> list[RowSpec]:
    """Drop the rows this cold-start user's config says not to build, removing any copy already on Plex.

    "Skip" has to mean GONE, not merely "not refreshed". Someone warm last month already has this row
    on their Home, and a history that thins out (or a Tautulli outage) would otherwise strand it there
    for ever, going stale, with nothing that ever cleans it up — the row would outlive the taste it was
    built from. Removal only ever makes the server more private, so it is safe wherever it lands.

    Returns the rows that should still be built.
    """
    keep: list[RowSpec] = []
    for spec in specs:
        if effective_cold_start(spec, cfg) != "skip":
            keep.append(spec)
            continue
        logger.info(
            "{}: row '{}' not built — {} watched titles, below the minimum of {}",
            user.username,
            spec.slug,
            len(user.history),
            cfg.min_history,
        )
        # write_lock: same as the muted/retired sweep — every Plex mutation is serialized when users
        # run concurrently. Scans EVERY library, not the run's delivery sections, so a copy left in a
        # library the row no longer targets goes too.
        with ctx.write_lock:
            removed_in = remove_row(
                ctx.plex,
                user,
                cfg,
                spec,
                dry_run=cfg.dry_run,
                diff=report.diff if report.diff is not None else CollectionDiff(),
                sections=ctx.plex.sections(),
                delivered_keys=_ledger_keys(ctx, user, spec),
            )
        _forget(report, spec, removed_in)
    return keep


def _forget(report: UserRunReport, spec: RowSpec, removed_in: list[str]) -> None:
    """Note the ledger entries a removal made dead, for the adapter to prune on persist.

    Matters most on the paths that REPEAT: a cold-skipped user is skipped again every night, so a key
    left behind here is re-presented for as long as they stay cold, and Plex reuses ratingKeys.
    """
    report.removed_deliveries.extend({"row_slug": spec.slug, "library_key": key} for key in removed_in)


def _remove_muted_and_retired(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, report: UserRunReport) -> None:
    """Remove this user's rows that were muted or disabled since the last run.

    A row muted or switched off in the UI is gone from ``cfg.rows``, but its collection still sits on
    this person's Home (excluded from everyone else, so private — just not gone). Removing it makes
    "muted"/"disabled" mean *gone*. This runs before the "no active rows -> return" check so a user
    whose every row was switched off is still cleaned up, and only ever makes the server MORE private,
    so it happens regardless of whether the user has any row this time.
    """
    diff = report.diff if report.diff is not None else CollectionDiff()
    muted = [s for s in cfg.per_person_rows() if _in_audience(user, s) and _is_muted(user, s)]
    retired = [s for s in cfg.retired_rows if not s.shared and _in_audience(user, s)]
    for spec in (*muted, *retired):
        # write_lock: a Plex mutation (and the collections-cache read/invalidate inside it) must be
        # serialized when users run concurrently — only reads + LLM overlap (Stage 3).
        with ctx.write_lock:
            # Scan EVERY library, not the run's (now targeting-scoped) delivery_sections: a muted row
            # whose library_keys later dropped a library can still have a stale copy there, and a
            # muted row must leave them all. plex.sections() is cached, so this is cheap.
            removed_in = remove_row(
                ctx.plex,
                user,
                cfg,
                spec,
                dry_run=cfg.dry_run,
                diff=diff,
                sections=ctx.plex.sections(),
                # Closes the gap this function's caller documents: a muted `{top_seed}` row could not
                # be title-matched, so it survived every run — private, but never actually gone. The
                # ledger identifies it without guessing at a title.
                delivered_keys=_ledger_keys(ctx, user, spec),
            )
        _forget(report, spec, removed_in)


# A row's candidate pool, as `_candidate_pool` returns it: (pool, in_library, ranked).
Pool = tuple[list[Candidate], list[Candidate], list[Candidate]]


@dataclass
class RowPolicy:
    """How ONE person's rows resolve their settings, and the candidate pools they share.

    Every ``effective_*`` method answers "what does THIS row use for this setting", resolving in the
    one direction the whole app resolves in: the person's per-row override, then the row's own
    value, then the run's default. ``pools_for`` memoises the expensive candidate gather, so rows
    that resolve to the same ``pool_key`` pay for it once.

    These were nested closures inside ``_run_user``, over mutable locals — two of them mutated in
    place *so that a closure would observe the change*. That is now explicit state, and the ORDER
    still matters: ``load_watched_breakdown`` (both branches) fills ``watched_movies`` and
    ``watched_shows``, ``mark_finished_titles`` (the non-cold branch only) fills ``watched_titles``,
    and both must run before the first ``pools_for`` call. ``pool_exclusions`` reads all three, and
    a pool built ahead of them would exclude nothing and hand the row titles it must not use.
    """

    ctx: EngineContext
    user: UserProfile
    cfg: EngineConfig
    # Every row being built for this person this run. Read by `pools_for` to decide whether a pool's
    # label needs its seed count to tell it apart from a sibling's.
    specs: list[RowSpec]
    library_index: dict[MediaType, dict[int, int]]
    report: UserRunReport
    # A watched item -> its tmdb_id, via ratingKey, across EVERY library (see `_rating_key_resolver`).
    resolve: Callable[[WatchedItem], int | None]

    # This person's watched breakdown: every watched movie, and each show's watched-vs-total episode
    # counts as Plex records them for this user, marks included (one WatchedItem per title, no
    # per-play accumulation). Filled for BOTH branches: non-cold pools read it to exclude finished
    # titles, and either branch's trace shows "watched N movies / M shows" — honest even for a thin
    # cold-start history.
    watched_movies: set[int] = field(default_factory=set)
    watched_shows: dict[int, tuple[int, int | None]] = field(default_factory=dict)
    # The FINISHED (tmdb_id, media_type) titles derived from that breakdown: read by `pools_for` (a
    # 0% row hard-excludes them) and by the per-row watched cap (>0). Left empty on the cold path,
    # which builds no pool and applies no cap.
    watched_titles: set[tuple[int, MediaType]] = field(default_factory=set)

    # A candidate pool per DISTINCT effective source-set among this user's rows. Rows that share
    # sources (the common case — every row inheriting the global set) reuse one pool; a row that
    # picks its own sources gets its own. Keyed by `pool_key`, memoised across the user.
    pool_cache: dict[tuple, Pool] = field(default_factory=dict)
    # (pool_key, recency) -> that pool re-cut at a row's overridden release-date weight. Empty on a
    # server where every row inherits the global, which is the default shape.
    recency_cuts: dict[tuple, list[Candidate]] = field(default_factory=dict)
    pool_failures: dict[tuple, str] = field(default_factory=dict)  # pool key -> why every source for it failed
    seed_cache: dict[tuple, list] = field(default_factory=dict)

    @cached_property
    def ratings(self) -> RatingsPolicy:
        """What Plex ratings do to this person's seeds tonight — the verdict AND the reasons behind it.

        Decided ONCE over their whole history. Once, and over everything, is the load-bearing part.
        `seeds_for` hands `derive_seeds` a row's SLICE of history (narrowed by the row's media and
        libraries), and whether a person's ratings can be trusted at all is an account-level judgement
        that abstains on a small sample — so deciding it per row let a movies-only row and a TV-only
        row reach opposite verdicts about the same person, and the run trace (which reads the full
        history) disagree with both.
        """
        return ratings_policy(self.user.history, self.cfg.dislike_threshold)

    @property
    def disliked(self) -> set[tuple[int, MediaType]]:
        """Titles this person rated low in Plex — the seeds `ratings` says to drop."""
        return self.ratings.blocked

    def load_watched_breakdown(self) -> None:
        """Fill ``watched_movies``/``watched_shows`` from this person's history.

        Must run before any pool is built — `pool_exclusions` reads both.
        """
        for item in self.user.history:
            tid = item.tmdb_id if item.tmdb_id is not None else self.resolve(item)
            if tid is None:
                continue
            if item.media_type is MediaType.MOVIE:
                self.watched_movies.add(tid)
            else:
                self.watched_shows[tid] = (item.viewed_leaf_count or item.watch_count, item.leaf_count)

    def mark_finished_titles(self) -> None:
        """Derive the finished-title set from the breakdown, once. Must run before any pool is built."""
        self.watched_titles |= _watched_titles(self.watched_movies, self.watched_shows, self.cfg.watched_show_pct)

    def effective_watched_pct(self, spec: RowSpec) -> float:
        return spec.watched_pct if spec.watched_pct is not None else self.cfg.watched_pct

    def excludes_watched(self, spec: RowSpec) -> bool:
        """Whether this row's POOL drops watched titles outright (rather than capping at delivery).

        A rewatch row must never exclude them — they are what it is built from — so it keeps the pool
        that includes them even at watched_pct 0.
        """
        return self.effective_watched_pct(spec) == 0 and not spec.rewatch

    def zero_pct_exclusions(self) -> set[tuple[int, MediaType]]:
        """What a 0% row must not contain: anything this person has TOUCHED, not merely finished.

        The slider says "already-watched titles: 0%", and until 1.2 that quietly meant "0% FINISHED",
        where a show only counted once they had seen 80% of it (or a length-scaled floor of ~3
        episodes). Plex itself has no such notion: its watched filter returns a show from the first
        episode, and a live probe of a real server found it returning shows as little as 1.1% watched
        (2 of 176). So a show someone was two episodes into was, to a 0% row, a fresh discovery — and
        five of ten started shows on that server were eligible to be recommended straight back.

        Now the two agree: at 0%, started IS watched. `_watched_titles` survives for the >0 cap, where
        "finished" still has to mean something definite for `floor(k * pct)` to be meaningful.

        A UNION rather than a swap: `_started_shows` needs ``viewed > 0``, while `_watched_titles`
        also counts a show whose episode total Plex could not report at all. Dropping the latter would
        quietly re-admit those.
        """
        return self.watched_titles | _started_shows(self.watched_shows)

    def pool_exclusions(self, spec: RowSpec) -> set[tuple[int, MediaType]] | None:
        """Titles this row's pool must not contain, or None when this row has no exclusion rule.

        The None sentinel is NOT "nothing to exclude" — `_candidate_pool` reads it as "this caller
        didn't compute a watched breakdown, so fall back to excluding the seeds". An EMPTY SET is the
        meaningful other thing: "I did compute it, and there is nothing finished." So a row with a rule
        returns its set even when empty, and only a row with no rule at all returns None. Collapsing
        the two (`return excluded or None`) quietly changed every 0% row belonging to someone with no
        finished titles — a TV-only viewer part-way through everything — from excluding nothing to
        excluding their seeds.
        """
        rule = False
        excluded: set[tuple[int, MediaType]] = set()
        if self.excludes_watched(spec):
            excluded |= self.zero_pct_exclusions()
            rule = True
        # `and spec.media != "movie"` mirrors `pool_key` EXACTLY. `_started_shows` only ever yields
        # SHOW keys, so on a movies row this contributes nothing — but setting `rule` anyway returned
        # an empty SET where an identical sibling row returns None, and `_candidate_pool` reads None
        # as "exclude the seeds" and a set as "exclude exactly this". Same key, two different pools:
        # whichever row computed first would win, and the other could be handed back its own seeds.
        # Today the API refuses that combination, but a guard in another module is not what should be
        # keeping this correct.
        if spec.unstarted_only and spec.media != "movie":
            excluded |= _started_shows(self.watched_shows)
            rule = True
        return excluded if rule else None

    def effective_refresh_days(self, spec: RowSpec) -> int:
        """How often this row re-selects its titles, in days — forced to nightly for a row that
        follows a watch.

        A row whose title names a watch (or that cycles between several) is ABOUT recency: at the
        default of 8 days it re-checks its seed barely once a week, so it goes on claiming "Because
        you watched X" long after the person moved on, and a cycling row advances a step a fortnight
        instead of a day. Reported as broken twice on issue #57, because from the outside it is
        indistinguishable from broken.

        Forced rather than merely defaulted, and forced over an explicit stored value too, because the
        row editor HIDES the cadence control for these rows — honouring a slow value someone saved
        before that would leave a row stuck with nothing in the UI to explain it or undo it.
        """
        if _names_a_seed(spec, self.user, self.cfg) or effective_seed_window(spec) > 1:
            return 1
        return spec.refresh_days if spec.refresh_days is not None else self.cfg.refresh_days

    def effective_recency(self, spec: RowSpec) -> float:
        """How much this row weights a title's release date. Row's own value, else the global.

        Delegates to the module-level helper rather than re-inlining the expression, so the
        per-person and shared paths cannot resolve it differently — they already did once, and the
        shared row was the one that lost.

        No forcing clause (unlike ``effective_refresh_days``): every row is free to hold any value,
        including an explicit 0.0 on a server whose global is high — that is how one person gets a
        "New & Notable" row and a "Hidden Gems" row at the same time.
        """
        return effective_recency(spec, self.cfg)

    def cut_at_recency(self, spec: RowSpec, in_library: list[Candidate], recency: float) -> list[Candidate]:
        """This row's own ``candidates_pre_rank`` cut, taken at its overridden release-date weight.

        Memoised per (pool, recency) because rows commonly agree: two "New & Notable" rows sharing a
        gather and a setting should sort the list once, not once each.
        """
        key = (self.pool_key(spec), recency)
        if key not in self.recency_cuts:
            kinds = [MediaType.MOVIE, MediaType.SHOW] if spec.media == "both" else [MediaType(spec.media)]
            self.recency_cuts[key] = ranking.cut_for_recency(
                in_library, kinds, self.cfg.candidates_pre_rank, recency, _run_year(self.ctx.run_day)
            )
        return self.recency_cuts[key]

    def effective_recent_count(self, spec: RowSpec) -> int:
        # This person's per-row override wins, then the row's own setting, then the global default —
        # the same user -> row -> global direction the row size resolves in. pool_key already folds
        # this value in for web rows, so two of this person's rows that differ in it don't share a pool.
        override = self.user.row_overrides.get(spec.slug)
        if override and override.recent_count is not None:
            return override.recent_count
        return spec.recent_count if spec.recent_count is not None else self.cfg.recent_count

    def effective_sources(self, spec: RowSpec) -> tuple[str, ...]:
        # Sorted so two rows with the same sources in a different order share ONE pool (gather is
        # set-based) — otherwise they'd each rebuild it, re-hitting rate-limited/LLM sources and, for
        # the non-deterministic llm_* sources, possibly diverging despite identical configuration.
        return effective_row_sources(spec, self.cfg.candidate_sources)

    def seeds_for(self, spec: RowSpec) -> list:
        """This row's seeds, from the watches its own libraries hold. Memoised per (media,
        libraries, max_seeds) so rows that target the same thing derive them once.

        max_seeds is part of the key rather than a slice of a shared list because `derive_seeds`
        balances across media types (each present type keeps >= a third of the budget), so a
        5-seed list is not the first 5 of a 30-seed one.

        A CYCLING row keys on its window and its own cycle offset too. The offset folds in the row
        slug, so two cycling rows that match on everything else still derive separately — without it
        they would share one entry and land on the same watch, which is the opposite of what someone
        turning this on asked for. A non-cycling row keys exactly as it always did (window 1, offset
        0), so the common case still shares one derivation across rows."""
        window = effective_seed_window(spec)
        offset = seed_cycle_offset(spec.slug, self.user.slug, self.ctx.run_day) if window > 1 else 0
        key = (
            spec.media,
            tuple(sorted(str(k) for k in spec.library_keys)),
            effective_max_seeds(spec, self.cfg),
            window,
            offset,
        )
        if key not in self.seed_cache:
            relevant = _history_for_row(self.ctx, self.user.history, spec)
            self.seed_cache[key] = derive_seeds(
                relevant,
                self.resolve,
                max_seeds=effective_max_seeds(spec, self.cfg),
                blocked=self.user.blocked_seeds,
                window=window,
                cycle_offset=offset,
                # Per-person rows only. The shared-row call below deliberately passes nothing.
                # Decided once over the whole history, NOT from this row's slice — see `disliked`.
                disliked=self.disliked,
            )
        return self.seed_cache[key]

    def pool_key(self, spec: RowSpec) -> tuple:
        # Sources alone is not enough. A row's media and its libraries both change which candidates
        # survive — and both now narrow the pool BEFORE the pre-rank truncation, so two rows that
        # differ in either must not share a pool. Rows that differ in none of the three (the common
        # case: everything inheriting the defaults) still share exactly one.
        return (
            self.effective_sources(spec),
            spec.media,
            tuple(sorted(str(k) for k in spec.library_keys)),
            # Only whether the pool hard-excludes watched titles changes the CANDIDATES: a 0% row
            # drops them from the pool; any >0 row keeps them and caps at delivery. Two >0 rows (20%
            # and 50%) share one pool and differ only in their cap, so they must not key apart.
            # A rewatch row keeps watched titles even at 0%, so it must key with the >0 rows — hence
            # `excludes_watched`, not the raw percentage.
            self.excludes_watched(spec),
            # An "unstarted shows only" row removes every started series from the POOL, so it cannot
            # share one with a row that keeps them — it would be handed candidates it must not use.
            #
            # Three ways this contributes nothing, and keying on it anyway costs a whole extra
            # TMDB/LLM gather per person per night:
            #   * a movies-only row — `_started_shows` yields only SHOW keys, which can never match
            #     anything in that pool;
            #   * a 0% row — since 1.2 `zero_pct_exclusions` already unions the started shows in, so
            #     the two rows' exclusion sets are byte-identical. Without this term a default 0% row
            #     and a 0% + unstarted_only row (the commonest pairing, now that the toggle is
            #     reachable on "films and shows" rows) each paid for their own gather for no
            #     difference in candidates.
            #   * a rewatch row — `excludes_watched` is false there, so the sets differ and the key
            #     must still split; that is why the check is `excludes_watched`, not `pct == 0`.
            # Over-eager separation is only ever a cost, never a leak — but it is a real cost.
            spec.unstarted_only and spec.media != "movie" and not self.excludes_watched(spec),
            # recent_count changes how many titles the WEB-SEARCH source searches, so its candidates
            # differ — but only for rows that actually use llm_web. Key on it only then, so two non-web
            # rows differing solely in recent_count still share one pool (no wasted TMDB/curate gather).
            self.effective_recent_count(spec) if "llm_web" in self.effective_sources(spec) else 0,
            # The SEEDS themselves, because max_seeds changes what every source searches from — not
            # just the web one. Keyed on the resulting seed list rather than the budget so two rows
            # whose different budgets yield the same seeds (a thin history: 12 watches, budgets of 20
            # and 30) still share one pool instead of paying for two gathers. seeds_for is memoised,
            # so asking for them here costs nothing. Order counts, not just membership: it feeds the
            # web-search slice and the trace sample, so two orderings of one set split the pool —
            # a wasted gather at worst, never a wrong share.
            tuple((seed.tmdb_id, seed.media_type) for seed in self.seeds_for(spec)),
        )

    def pools_for(self, spec: RowSpec) -> Pool | None:
        """This row's pool, or None when every source it uses is down.

        Per ROW, not per user: a row pinned to a single source (a Trakt-only row while Trakt 502s)
        must not take the person's other rows down with it — those rows have working sources and a
        row they can still fill.
        """
        key = self.pool_key(spec)
        if key in self.pool_failures:
            return None
        if key not in self.pool_cache:
            try:
                self.pool_cache[key], gather_stats = _candidate_pool(
                    self.ctx,
                    self.seeds_for(spec),
                    row_library_index(self.ctx, spec, self.library_index),
                    excluded_genres=self.user.excluded_genres,
                    profile=self.user,
                    sources=list(key[0]),
                    recent_count=self.effective_recent_count(spec),
                    media=spec.media,
                    # See `pool_exclusions` for the full rules (0% vs >0, rewatch, unstarted-only) and
                    # for what the None sentinel means here.
                    watched_exclusions=self.pool_exclusions(spec),
                    # The SERVER's value, deliberately: this pool is shared between rows (`pool_key`
                    # does not split on recency, so the gather is paid for once), and a row that
                    # overrides it re-cuts the cached `in_library` in `cut_at_recency`.
                    recency=self.cfg.recency,
                )
            except Exception as e:
                self.pool_failures[key] = f"{type(e).__name__}: {e}"
                logger.warning("{}: row '{}' has no working candidate source ({})", self.user.username, spec.slug, e)
                return None
            # Once per pool computation (this cache miss) — the gather's AI cost belongs to this user.
            # Label the pool by its media + sources, and by its seed count when this person's rows
            # differ there: two rows sharing media and sources but not their seed budget are two real
            # gathers that would otherwise record under one identical name, leaving the trace unable to
            # say which is which. This names them in the RECORD; the trace page currently merges a
            # library's gathers into one source list and never renders the label, so nothing changes on
            # screen until that page grows a per-row axis. The media prefix must stay first —
            # `poolCoversMedia` splits on " · " to decide which library a gather belongs to.
            seed_n = len(self.seeds_for(spec))
            pool_label = f"{spec.media} · {', '.join(key[0])}"
            if any(len(self.seeds_for(other)) != seed_n for other in self.specs):
                pool_label += f" · {seed_n} seed{'' if seed_n == 1 else 's'}"
            _record_gather(self.report, gather_stats, pool_label=pool_label)
        return self.pool_cache[key]


def _cold_start(
    policy: RowPolicy,
    library_of_watch: Callable[[WatchedItem], str],
    library_of_seed: Callable[[object], str],
) -> list[Pick]:
    """Popular-on-this-server picks for someone whose history is too thin to seed from, plus the
    trace that keeps them from reading as skipped. Returns the base picks each row then slices."""
    ctx, user, report = policy.ctx, policy.user, policy.report
    # Enough picks for the LARGEST row this user is in; each row then takes its own k.
    base_cold = _cold_start_picks(ctx, user, policy.cfg, k=max(spec.size for spec in policy.specs))
    report.status = "cold_start"
    # File the trace even though no TMDB/Trakt search ran: their (thin) watches as the first stage —
    # NO seeds, because nothing was searched from them (the point of cold start) — and a synthetic
    # cold_start search stage. Without this the trace is empty, the run page shows no "How we picked"
    # button, and a cold user reads as skipped when they weren't (the reported Cassie bug).
    _record_history_trace(
        report,
        user.history,
        policy.specs,
        lambda _spec: [],
        policy.watched_movies,
        policy.watched_shows,
        library_of_watch=library_of_watch,
        library_of_seed=library_of_seed,
        ratings=policy.ratings,
    )
    _record_cold_start_trace(report, base_cold)
    _emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": 0})
    return base_cold


def _warm_start(
    policy: RowPolicy,
    demand: requests_mod.DemandMap | None,
    library_of_watch: Callable[[WatchedItem], str],
    library_of_seed: Callable[[object], str],
) -> None:
    """Derive this person's seeds, build every row's candidate pool up front, and record what came
    back: the trace, the run counts, and (when requests are on) the titles no library holds.

    Raises when EVERY row's sources are down — that is a failed user, not a quiet "ok".
    """
    ctx, user, specs, report = policy.ctx, policy.user, policy.specs, policy.report
    # Reported as the widest seed set any of this person's rows uses — the "both media, every
    # library" case when they have one, so the number still means "how much of their history fed
    # tonight's rows" rather than one arbitrary row's slice.
    report.counts.seeds = max((len(policy.seeds_for(spec)) for spec in specs), default=0)
    # The finished-title set, derived once: read by pools_for (0% hard-exclude) and the per-row
    # watched cap (>0). Must land BEFORE the first pool is built.
    policy.mark_finished_titles()
    _record_history_trace(
        report,
        user.history,
        specs,
        policy.seeds_for,
        policy.watched_movies,
        policy.watched_shows,
        library_of_watch=library_of_watch,
        library_of_seed=library_of_seed,
        ratings=policy.ratings,
    )
    _emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": report.counts.seeds})
    for spec in specs:  # build every row's pool up front so counts and demand see them all
        policy.pools_for(spec)
    # Only if EVERY row's sources are down do we know nothing about this person: that's a failed
    # user, not a quiet "ok" that leaves yesterday's rows in place. One dead row among several
    # is just that one row.
    if policy.pool_failures and not policy.pool_cache:
        raise RuntimeError("; ".join(sorted(policy.pool_failures.values())))
    # Counts are the distinct union across pools (a title in two rows' pools is one candidate).
    pools = policy.pool_cache.values()
    report.counts.candidates = len({(c.tmdb_id, c.media_type) for p in pools for c in p[0]})
    report.counts.in_library = len({(c.tmdb_id, c.media_type) for p in pools for c in p[1]})
    # Union the per-row re-cuts in too: a row overriding the release-date weight reaches titles the
    # shared cut dropped, and counting only `pools` under-reports what was actually pre-ranked.
    report.counts.pre_ranked = len(
        {(c.tmdb_id, c.media_type) for p in pools for c in p[2]}
        | {(c.tmdb_id, c.media_type) for cut in policy.recency_cuts.values() for c in cut}
    )
    if demand is not None:
        _record_demand(policy, demand)
    report.status = "ok"


def _record_demand(policy: RowPolicy, demand: requests_mod.DemandMap) -> None:
    """Record what this user wanted that the server doesn't have, for the run-wide request pass.

    A missing title is attributed to exactly the rows whose pool surfaced it: it gets the user's own
    request tag plus the tag of each such row. Deduped per user so demand counts them once.
    """
    ctx, user, cfg = policy.ctx, policy.user, policy.cfg
    user_tag = {user.request_tag} if user.request_tag else set()
    first_seen: dict[tuple[int, MediaType], Candidate] = {}
    title_tags: dict[tuple[int, MediaType], set[str]] = {}
    title_why: dict[tuple[int, MediaType], list[RequestWhy]] = {}
    for spec in policy.specs:
        pools = policy.pools_for(spec)
        if pools is None:
            continue
        # The row's own name (the same one the user sees), so the inbox can say WHICH row a
        # request came from. Fill the placeholders the template may carry.
        row_template = resolve_row_template(spec, user, cfg)
        # A missing title still has a media type, so {library_name} renders as the library that
        # type would land in ("TV Shows" for a missing show). Keyed by media type; the first
        # library of that type wins when the row spans several.
        media_library: dict[MediaType, str] = {}
        for section in target_sections(ctx.delivery_sections, spec):
            media_library.setdefault(section_kind(section), getattr(section, "title", "") or "")
        for c in requests_mod.collect_missing(pools[0], policy.library_index):
            key = (c.tmdb_id, c.media_type)
            first_seen.setdefault(key, c)
            tags = title_tags.setdefault(key, set())
            tags |= user_tag  # the user wanted it, whatever the row's media
            # ...but a row's tag only applies to titles that row could actually show, so a
            # shows-only row never tags a missing movie (its pool holds both until delivery).
            if spec.request_tag and spec.media in ("both", c.media_type.value):
                tags.add(spec.request_tag)
            # Provenance for the inbox: this row surfaced it for this user, seeded by the
            # strongest history title behind the candidate ("because you watched …").
            seed_title = c.top_seed.title if c.top_seed else ""
            row_name = row_template.replace("{user}", user.display_name).replace(
                "{top_seed}", seed_title or "your favourites"
            )
            # {library_name} renders as the library this title's media type lands in; blank (an
            # unknown media type) collapses the gap ("✨  Picked for You" -> "✨ Picked for You").
            if "{library_name}" in row_name:
                library_name = media_library.get(c.media_type, "")
                row_name = " ".join(row_name.replace("{library_name}", library_name).split())
            entry = RequestWhy(
                user=user.username,
                row=row_name,
                seed=seed_title,
                source=(sorted(c.sources)[0] if c.sources else ""),
            )
            why = title_why.setdefault(key, [])
            if entry not in why:
                why.append(entry)
    # `demand` is the run-wide shared map; the per-user tally above is local, so only this
    # merge needs the lock (Stage 3 parallel runs).
    with ctx.write_lock:
        for key, cand in first_seen.items():
            requests_mod.accumulate(demand, [cand], tags=title_tags[key], wanter=user.username, why=title_why[key])


def _build_section_picks(
    policy: RowPolicy,
    spec: RowSpec,
    targets: list,
    k: int,
    *,
    cold: bool,
    base_cold: list[Pick],
    pool_for_row: list[Candidate],
) -> dict[str, list[Pick]]:
    """This row's picks for each library it targets — carried forward, refreshed, or built fresh.

    A row runs PER LIBRARY, not per media type: each library gets its own full collection of k,
    curated from that library's own contents. So a server with two movie libraries (Movies + 4K)
    gets a full row in EACH, and a mostly-TV watcher still gets a full movie row and a full show row
    (the "one movie in Picked for You" bug, SFLIX 2026-07-15).

    ``pool_for_row`` is this row's pre-ranked candidates; on the cold path it is empty and unread,
    and ``base_cold`` is sliced instead.
    """
    ctx, user = policy.ctx, policy.user
    section_picks: dict[str, list[Pick]] = {}
    refresh_days = policy.effective_refresh_days(spec)
    for section in targets:
        kind = section_kind(section)
        # tmdb_id -> ratingKey for THIS library only; a candidate not in this library isn't a
        # valid pick for it, however well it ranks for the row overall.
        if cold:
            # Pulled from THIS library, at this row's full size. Two bugs lived in the old version,
            # both invisible because they only hit users with too little history to notice:
            #
            #   * `_cold_start_picks` split `k` across `sections_by_type()` and each section then
            #     took only its own share, so on any server with both a movie and a TV library —
            #     nearly all — every cold row came back HALF SIZE.
            #   * the picks came from the representative library for the media type, not the row's
            #     own, so a library-pinned row kept only the intersection and could deliver nothing
            #     at all, reported as a green run.
            #
            # `targets` already honours `library_keys`, so taking `k` from `section` fixes both.
            cands = [
                Pick(
                    tmdb_id=tmdb_id,
                    rating_key=item.ratingKey,
                    title=item.title,
                    rank=i + 1,
                    reason="Popular on this server",
                    media_type=kind,
                    sources=["cold_start"],  # no history to work from — say so rather than imply a match
                )
                for i, (tmdb_id, item) in enumerate(ctx.plex.top_rated(section, k))
            ]
            if not cands:  # a library with nothing rated falls back to the per-user pull
                cands = [p for p in base_cold if p.media_type is kind][:k]
            ranked_cold = [replace(p, rank=i + 1) for i, p in enumerate(cands)]
            # Ordered like any other row: a cold-start user who set their row to Shuffled expects it
            # to shuffle, and "their history is thin" is no reason to hand back a different feature.
            # No `ratings` override — a cold row is drawn from the library's top-rated, and spending
            # MDBList quota on a placeholder row is not worth it.
            section_picks[section.key] = _apply_order(
                ranked_cold, spec.pick_order, row_slug=spec.slug, user_slug=user.slug, run_day=ctx.run_day
            )
            # Recorded here too: the cold branch returns early, and a row missing from the trace
            # reads as "not built" rather than "built from the server's top-rated".
            policy.report.trace.setdefault("selection", []).append(
                {
                    "row": spec.slug,
                    "library": getattr(section, "title", str(section.key)),
                    "decision": "cold_start",
                    "size": k,
                    "delivered": len(ranked_cold),
                    "candidates": len(cands),
                    "pick_order": spec.pick_order,
                }
            )
            continue
        sec_idx = ctx.section_index.get(section.key, {})
        pct = policy.effective_watched_pct(spec)
        sub = [c for c in pool_for_row if c.media_type is kind and c.tmdb_id in sec_idx]
        # str(section.key): previous_picks is keyed by the PickRow.section_key STRING column, so the
        # live section key (which may not be a str) must be coerced or carry-forward silently misses.
        prior_valid = _reusable_prior(
            ctx.previous_picks.get((user.slug, spec.slug, str(section.key)), []),
            kind,
            sec_idx,
            # The SAME set the 0% pool excludes, so a carried-forward pick can't survive a rule that
            # would have kept it out of the pool. Consulted only when `pct <= 0`; passing the wider
            # set unconditionally keeps the two definitions from drifting apart.
            policy.zero_pct_exclusions(),
            pct,
            keep_watched=spec.rewatch,
            # Independent of `pct`: see `_reusable_prior`. Movies are exempt because a movie with any
            # view is already finished, so "started" is not a distinct state there.
            started=(
                frozenset(_started_shows(policy.watched_shows))
                if spec.unstarted_only and spec.media != "movie"
                else frozenset()
            ),
        )
        recipe = row_recipe(policy, spec)
        was = ctx.previous_recipes.get((user.slug, spec.slug, str(section.key)), "")
        recipe_changed = bool(was) and was != recipe
        refresh = _is_refresh_night(spec.slug, user.slug, ctx.run_day, refresh_days)
        # Hoisted out of the refresh branch because the `new_first` order needs it on every path: a
        # pick is "new" if this row was not already carrying it, whichever branch produced it.
        prior_ids = {(p.tmdb_id, p.media_type) for p in prior_valid}

        # Decided here, before the clearing below rewrites `prior_valid` — after it, a settings
        # change is indistinguishable from a first build.
        if not prior_valid:
            decision = "rebuilt"
        elif recipe_changed:
            decision = "settings_changed"
        elif not refresh:
            decision = "carried_forward"
        else:
            decision = "refreshed"

        if prior_valid and recipe_changed:
            # The owner changed a setting that decides this row's CONTENTS, so rebuild it now
            # whatever the cadence says. Freshness suppresses churn when nothing changed; a
            # deliberate edit is not churn, and making it wait up to a fortnight reads as the setting
            # being broken — on a real server, raising "Recent releases" left 36 of 42 rows
            # redelivering byte-identical picks. Unknown (a row built before recipes were recorded)
            # is deliberately NOT a mismatch — see `EngineContext.previous_recipes`.
            #
            # Dropping the prior entirely rather than setting `refresh`: the refresh branch keeps
            # two-thirds of the OLD row, so a row switched to "prefer recent releases" would carry
            # most of its old titles forward and look like the change half-worked.
            prior_valid = []
            prior_ids = set()

        if prior_valid and not refresh:
            # Not this row's refresh night: redeliver last run's picks unchanged, so delivery's
            # unchanged-skip avoids the Plex write. Pad only if a title has since left the library,
            # so the row stays full.
            #
            # What a slower cadence does NOT save is LLM tokens, though this said it did for a long time and
            # the claim survived into a cost warning to the owner. The candidate gather — the only
            # thing that calls a curator — runs in `pools_for` ABOVE this function, before the
            # refresh check, so it happens on every run at every cadence. `build_picks` is pure
            # ranking. A slower cadence buys Plex writes, nothing else.
            sec_picks = prior_valid[:k]
            if len(sec_picks) < k and sub:
                sec_picks = _pad_picks(sec_picks, sub, k)
        elif prior_valid and not _seed_moved(spec, prior_valid, sub, policy.user, policy.cfg):
            # Refresh night: keep the strongest ~two-thirds by RANK (match quality — `prior_valid` is
            # ordered by the persisted rank column, not by how the row was displayed), and swap the
            # rest for genuinely-new titles.
            # Pick only from candidates NOT already in the row so a just-rotated-out title can't
            # bounce straight back — the internal anti-immediate-repeat guard that replaced staleness_runs.
            keep_n = min(len(prior_valid), round(_KEEP_FRACTION * k))
            kept = prior_valid[:keep_n]
            fresh_pool = [c for c in sub if (c.tmdb_id, c.media_type) not in prior_ids]
            new_picks = picker.build_picks(fresh_pool, k)
            newcomers = [p for p in new_picks if (p.tmdb_id, p.media_type) not in prior_ids]
            # Take only what there is ROOM for, before ordering. Handing the whole merged list to
            # `_rank_against_pool` and truncating there let pool order — pure score — decide who
            # stayed, which is the ordering `diversify_by_seed` exists to defeat: `new_picks` comes
            # back diversified and the truncation threw that away, collapsing the row onto the one
            # heavily-watched taste whose look-alikes lead the pool. Measured over 10 slots, a
            # bootstrap spread of 7/6/2 across three seeds returned as 15/0/0 on the first refresh,
            # and stayed there — the collapsed row is what carries forward to the next one.
            sec_picks = _rank_against_pool(kept + newcomers[: max(0, k - len(kept))], sub)
            if len(sec_picks) < k:
                sec_picks = _pad_picks(sec_picks, fresh_pool, k)
            # `_seed_moved` above asked whether the POOL still leads with the seed this row is named
            # after. That is not quite the question the title asks: the name renders from the
            # best-matching DELIVERED pick, and re-ranking survivors against newcomers can put a
            # differently-seeded newcomer first even when the pool's own top seed never moved. Left
            # here, the row would rename itself while still carrying the old seed's picks — the exact
            # stale claim `_seed_moved` exists to prevent. Only reachable above one seed, which is
            # why the single-seed tests never saw it.
        else:
            # Bootstrap: this row+library has never been built (or its picks predate row/library
            # stamping) — build a fresh full row, exactly like a first run. Also reached on a refresh
            # night when `_seed_moved` says the seed this row is NAMED after has changed: carrying
            # anything forward would leave the title claiming a watch the contents no longer answer to.
            if not sub:
                # Say so: this is the ONLY exit that leaves a library with no collection and no
                # trace entry, so without a line here "why is my Movies row empty?" cannot be
                # answered from the run page at all. The commonest cause is a seed budget too
                # small to cover both media types (a media=both row at max_seeds=1 seeds one
                # type, so the other's pool is empty) — the row editor steers away from that,
                # but a hand-set budget or an API edit can still land there.
                logger.info(
                    "{}: row '{}' has no candidates for section '{}' — nothing to build there",
                    user.username,
                    spec.slug,
                    getattr(section, "title", section.key),
                )
                continue
            sec_picks = picker.build_picks(sub, k)
            if len(sec_picks) < k:
                sec_picks = _pad_picks(sec_picks, sub, k)

        if spec.rewatch:
            # A rewatch row wants the opposite of the cap: finished titles FIRST. Checked before
            # `pct` because a rewatch row left at the default 0% would otherwise skip both branches
            # and deliver whatever order the ranking happened to produce.
            sec_picks = _prefer_watched(sec_picks, sub, policy.watched_titles, k)
        elif pct > 0:
            # Let at most `pct` of this library's row be already-finished titles; backfill the
            # rest from its fresh candidates. (At pct == 0 the pool already dropped finished ones.)
            sec_picks = _apply_watched_cap(sec_picks, sub, policy.watched_titles, k, pct)
        # `rank` is stamped from the SELECTION order and the display order is applied after it, so the
        # two never collapse into one another. Everything that asks "which pick is this row's best
        # match" reads rank — `render_row_name`, which the `{top_seed}` title comes from, and
        # `_seed_moved`, which reads it back through `previous_picks` (ordered by the rank column).
        # Ordering last WITHOUT this made both of those answer "whichever pick sorted first tonight":
        # a `{top_seed}` row ordered by rating renamed itself after a different seed, and a shuffled
        # one re-derived `_seed_moved` off an arbitrary pick and rebuilt itself every refresh night.
        ranked = [replace(p, rank=i + 1, recipe=recipe) for i, p in enumerate(sec_picks[:k])]
        # Only a row actually sorting on rating pays for the lookups, and only for its own k picks.
        ratings = _rated_by_source(ranked, ctx) if spec.pick_order == "rating" else None
        # Derived from the FINAL list rather than from the refresh branch's `new_picks`, because the
        # watched cap and the rewatch reordering above can both backfill titles from `sub` that the
        # branch never saw — those are new to the row too, and `new_first` has to lead with them.
        new_keys = {(p.tmdb_id, p.media_type) for p in ranked if (p.tmdb_id, p.media_type) not in prior_ids}
        # What happened to this row tonight, and the settings that decided it. Without this the run
        # page can say what a row HOLDS but never why it holds it — and the commonest question ("I
        # changed a setting, why did nothing move?") is answered by a line that was not recorded
        # anywhere: most nights a row is redelivered untouched and the report looked identical to a
        # rebuild. Cheap to write (a dict per row per library) and read straight from the values the
        # branch above already computed, so it cannot drift from what actually happened.
        policy.report.trace.setdefault("selection", []).append(
            {
                "row": spec.slug,
                "library": getattr(section, "title", str(section.key)),
                "decision": decision,
                "size": k,
                "delivered": len(ranked),
                "candidates": len(sub),
                "cut_cap": ctx.config.candidates_pre_rank,
                "carried": len(prior_valid),
                "new": len(new_keys),
                "refresh_night": refresh,
                "rebuild_every_days": refresh_days or None,  # 0 = frozen, never rebuilt
                "recency": policy.effective_recency(spec),
                "watched_pct": pct,
                "pick_order": spec.pick_order,
                "rewatch": bool(spec.rewatch),
                "unstarted_only": bool(spec.unstarted_only),
            }
        )
        # "best" is a no-op, which is what keeps the rewatch/watched-cap orderings above intact
        # unless the owner explicitly asked for a different one.
        section_picks[section.key] = _apply_order(
            ranked,
            spec.pick_order,
            row_slug=spec.slug,
            user_slug=user.slug,
            run_day=ctx.run_day,
            ratings=ratings,
            new_keys=new_keys,
        )
        _log_row_provenance(user, spec, section, section_picks[section.key], sub, k)
    return section_picks


def _deliver_row(
    policy: RowPolicy,
    spec: RowSpec,
    picks: list[Pick],
    section_picks: dict[str, list[Pick]],
    *,
    sole_row: bool,
    stored_labels: dict[str, str],
    order_work: list[tuple] | None,
) -> None:
    """Write one row's collections to Plex, under the write-lock and with an idempotent retry.

    write_lock: the Plex collection writes AND the shared stored_labels mutation inside deliver_rows
    must be serial across users — the leak-safe half of Stage 3 parallelism. Timed on both sides so a
    slow run can be split into lock-CONTENTION (waiting behind another user's write) vs real WORK
    (this user's own PMS calls) — the two look identical in wall-clock otherwise, and only the second
    is fixable by making the writes cheaper (perf diag 2026-07-19).

    Delivery is upsert-idempotent (re-reads current membership, re-applies only the delta), so a PMS
    timeout retries JUST this write, NOT the expensive gather+curate that produced ``picks``. Each
    attempt re-acquires the write-lock and the backoff sleep happens OUTSIDE it, so a stalled user
    never holds the lock while waiting. This replaced a whole-user retry that re-ran the LLM and a
    full re-gather on a single Plex hiccup (SFLIX run 3: ~2795s for danvex before it failed,
    2026-07-19).
    """
    ctx, user, cfg, user_report = policy.ctx, policy.user, policy.cfg, policy.report
    # A delivery RETRY re-runs deliver_rows for the whole row, which appends one breakdown entry
    # per library — so a mid-row timeout (library 1 delivered, library 2 stalls) would record
    # library 1 twice on the retry. Reset the per-row breakdown to its pre-attempt length on each
    # attempt so the audit stays idempotent too, not just the Plex writes (rule 10). user_report.diff
    # needs no reset: it is None during delivery (only populated from swept rows after _run_user).
    breakdown_mark = len(user_report.breakdown)

    def _deliver_locked() -> None:
        del user_report.breakdown[breakdown_mark:]  # drop any entries a prior failed attempt appended
        lock_wait_start = time.monotonic()
        with ctx.write_lock:
            work_start = time.monotonic()
            claimed_this_run = _claimed_this_run(user_report)
            deliver_rows(
                ctx.plex,
                user,
                picks,
                cfg,
                spec,
                sole_row=sole_row,
                # {section key -> ratingKey} for THIS row and user: which object delivery should
                # retitle rather than rebuild when the title has moved on.
                #
                # Minus anything THIS RUN has already delivered to. Plex ratingKeys are rowids and
                # get reused: the sweep can free row A's id at the top of a run, row B create and
                # be handed it, and row A then match B's brand-new collection and retitle it. The
                # breakdown is the record of what this run has already written, so excluding it
                # closes that window outright.
                delivered_keys={
                    section_key: key
                    for (u, r, section_key), key in ctx.delivered_keys.items()
                    if u == user.slug and r == spec.slug and (section_key, key) not in claimed_this_run
                },
                dry_run=cfg.dry_run,
                stored_labels=stored_labels,
                diff=user_report.diff,
                sections=ctx.delivery_sections,
                section_index=ctx.section_index,
                section_picks=section_picks,
                breakdown=user_report.breakdown,
                poster_artist=ctx.poster_artist,
                order_work=order_work,
            )
            logger.debug(
                "{}: row '{}' delivery — waited {:.1f}s for write-lock, wrote {} librar(ies) in {:.1f}s",
                user.username,
                spec.slug,
                work_start - lock_wait_start,
                len(section_picks),
                time.monotonic() - work_start,
            )

    _retry_idempotent(_deliver_locked, label=f"{user.username} delivery of {spec.slug!r}")


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    demand: requests_mod.DemandMap | None = None,
    order_work: list[tuple] | None = None,
) -> bool:
    """Deliver every per-person row this user is in the audience of. Candidates are computed once
    and reused across rows; each row curates and delivers with its own size/media/recipe. Returns
    True when at least one row was delivered (a candidate for promotion).

    When ``demand`` is provided (requests are on), the candidates this user wanted but no delivery
    library holds are folded into it, so the run-wide request pass can ask Sonarr/Radarr for them.
    """
    cfg = ctx.config

    user_report.diff = CollectionDiff()
    _remove_muted_and_retired(ctx, user, cfg, user_report)

    # Every row that could still have a COLLECTION under this user's label — the predicate `sole_row`
    # actually needs, since it licenses delivery to treat a title mismatch as an in-place rename and
    # that is only safe when one collection could plausibly be there.
    #
    # Deliberately NOT filtered by scope or by mute:
    #   * scope — every row has its own cron, so every scheduled run is scoped. Counting the built set
    #     made row A's 3am cron claim "this user has one row", find row B's collection alone in that
    #     library (all their rows share one label; only the title tells them apart) and rebuild it as
    #     row A. Row B destroyed nightly, reported as an ordinary delivery.
    #   * mute — `_remove_muted_and_retired` runs just above and now reaches even a `{top_seed}` row,
    #     via its delivery-ledger ratingKey. But only when the ledger HAS one: a row delivered before
    #     the ledger existed, or one whose entry two rows both claim (dropped as ambiguous), is still
    #     left alone by design. Its collection is then still on the server, and excluding it here
    #     re-opens the same takeover through a different door.
    owned = [spec for spec in cfg.per_person_rows() if _in_audience(user, spec)]
    specs = [s for s in owned if not _is_muted(user, s) and cfg.should_build(s)]
    # The same three conditions, recorded per row rather than collapsed into one sentence. `reason`
    # explains the person; this attributes the decision to the ROW, which is what a rows-first run
    # view needs to place someone under the rows they were skipped for. Written on every path — a
    # user who builds successfully is in the tree too.
    user_report.rows_considered = {
        spec.slug: (
            "not_in_audience"
            if not _in_audience(user, spec)
            else "muted"
            if _is_muted(user, spec)
            else "due"
            if cfg.should_build(spec)
            else "not_due"
        )
        for spec in cfg.per_person_rows()
    }
    if not specs:
        # Mark the STATUS too, not just the live event: the pipeline's terminal event said "skipped"
        # while the persisted row kept its default "pending", so a reload showed a user stuck
        # mid-run forever.
        user_report.status = "skipped"
        user_report.reason = _why_no_rows(user, cfg)
        return False
    _emit(ctx, user.slug, "history", {})
    # Reuse a history the CALLER already filled, exactly as the shared-row path does. The server
    # pre-fills it from its watched-title cache, which turns the run's second complete per-user read
    # of the night into a no-op; a direct engine run leaves it empty and reads here as before.
    user.history = user.history or ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    # Decided BEFORE the policy is built, because a row set to skip a cold start is not this person's
    # row tonight at all: it must not seed a pool, size `base_cold`, or appear in the trace.
    cold = len(user.history) < cfg.min_history
    if cold:
        due = specs
        # A DELTA, not a total: `_remove_muted_and_retired` above appends to this same diff, so a
        # total would credit the skip with a muted row's deletion and tell the owner the skip removed
        # something when it removed nothing.
        deleted_before = len(user_report.diff.deleted) if user_report.diff else 0
        specs = _drop_cold_skipped_rows(ctx, user, cfg, specs, user_report)
        if not specs:
            # Every row they have skips a cold start. Their status stays `cold_start`, NOT "skipped":
            # the reason there is no row is that their history is thin, and `user.cold_start` (which
            # run_persistence derives from exactly this status) is what the Users page reads to say so.
            # Reporting "skipped" would clear that flag and leave the UI unable to explain the absence.
            user_report.status = "cold_start"
            deleted_now = len(user_report.diff.deleted) if user_report.diff else 0
            user_report.reason = _why_cold_skipped(user, cfg, due, deleted_now - deleted_before)
            return False

    policy = RowPolicy(
        ctx=ctx,
        user=user,
        cfg=cfg,
        specs=specs,
        library_index=library_index,
        report=user_report,
        resolve=_rating_key_resolver(seed_index),
    )
    policy.load_watched_breakdown()
    library_of_watch, library_of_seed = _library_resolvers(ctx)

    base_cold: list[Pick] = []
    if cold:
        base_cold = _cold_start(policy, library_of_watch, library_of_seed)
    else:
        _warm_start(policy, demand, library_of_watch, library_of_seed)

    if not ctx.plex.sections_by_type():
        raise RuntimeError("no movie or show library found for delivery")

    # One diff and label map for the whole user, accumulated across their rows (already holding any
    # muted-row deletions from above). Handed to delivery rather than returned from it: a row can
    # half-succeed across libraries, and a row that was created and labelled must reach
    # `stored_labels` even if a later write blows up — otherwise nobody's share filter excludes it
    # and it is visible to everyone (the leak we exist to fix).
    all_picks: list[Pick] = []
    delivered_any = False

    for spec in specs:
        # A per-row override lets this one person resize or restyle this one row; each field falls
        # through to the row's own setting when unset. Row beats global — the same direction the
        # name template and the curation recipe resolve in.
        override = user.row_overrides.get(spec.slug)
        k = (override.size if override and override.size else None) or spec.size or cfg.row_size
        targets = target_sections(ctx.delivery_sections, spec)
        pool_for_row: list[Candidate] = []
        if not cold:
            # This row's own pool: its sources, its media and its libraries — already narrowed to
            # all three BEFORE the pre-rank truncation, so nothing this row could show was cut by
            # candidates it could never show.
            pools = policy.pools_for(spec)
            if pools is None:
                continue  # every source this row uses is down; its siblings still deliver
            _pool, in_library, pool_for_row = pools
            # A row that overrides the server's release-date weight needs its OWN truncation, not
            # just its own ordering: the cut decides which candidates a row may select from at all,
            # so re-ordering what the global's cut left would cap the setting at whatever survived
            # it. Re-taken from `in_library` — the cached pre-cut list — so this costs a sort, never
            # another gather. A row that inherits reuses the pool's cut untouched.
            recency = policy.effective_recency(spec)
            if recency != ctx.config.recency:
                pool_for_row = policy.cut_at_recency(spec, in_library, recency)
            row_label = spec.name_template or spec.slug
            _emit(ctx, user.slug, "curating", {"candidates": len(pool_for_row), "row": row_label})
        section_picks = _build_section_picks(
            policy, spec, targets, k, cold=cold, base_cold=base_cold, pool_for_row=pool_for_row
        )
        # Stamp each pick with the row AND the library it belongs to, so the user page can group picks
        # per row and the effectiveness report can split a multi-library row into one line per library.
        library_names = {section.key: getattr(section, "title", "") or "" for section in targets}
        section_picks = {
            key: [
                replace(p, collection_slug=spec.slug, section_key=key, library=library_names.get(key, "")) for p in sp
            ]
            for key, sp in section_picks.items()
        }
        # Record the exact title delivery will write for EACH library, so the promote phase can apply
        # this row's placement/pin. Per library, because a {top_seed} OR {library_name} title differs
        # library to library. Must match delivery's `render_row_name(..., library_name) + marker` — same
        # section title in, or promote would look for a row delivery never wrote (it'd stay unhidden).
        title_template = resolve_row_template(spec, user, cfg)
        marker = row_marker(user.plex_account_id)
        for section_key, sp in section_picks.items():
            if sp:
                title = render_row_name(title_template, user, sp, library_name=library_names.get(section_key, ""))
                user_report.placement_titles[title + marker] = spec.slug
        picks = [pick for sp in section_picks.values() for pick in sp]
        all_picks.extend(picks)
        _emit(ctx, user.slug, "delivering", {"picks": len(picks), "row": spec.name_template or spec.slug})
        _deliver_row(
            policy,
            spec,
            picks,
            section_picks,
            sole_row=len(owned) == 1,
            stored_labels=stored_labels,
            order_work=order_work,
        )
        delivered_any = delivered_any or bool(picks)

    user_report.picks = all_picks
    user_report.counts.picks = len(all_picks)
    if not all_picks:
        # "My row is empty / hasn't changed" is the most common thing an operator gets asked, and
        # the answer is always somewhere in this chain — no watch history, no seeds from it, no
        # candidates from the sources, or nothing of the candidates actually in their libraries.
        # Without the counts this line said only that it happened, sending the operator to the trace.
        counts = user_report.counts
        logger.warning(
            "{}: no picks produced — existing rows are left as they are "
            "(history={} seeds={} candidates={} in_library={}){}",
            user.username,
            counts.history,
            counts.seeds,
            counts.candidates,
            counts.in_library,
            f" — {user_report.reason}" if user_report.reason else "",
        )
    return delivered_any  # nothing delivered -> nothing to promote


def _claimed_this_run(user_report) -> set[tuple[str, int]]:
    """(library_key, ratingKey) pairs this run has already delivered to for this user.

    Delivery may retitle a collection the ledger names — so a key that has ALREADY been written this
    run must not be offered to a second row, or the later row takes over the earlier one's brand-new
    collection. Reachable because Plex ratingKeys are reused rowids and the sweep frees ids mid-run.
    """
    return {
        (str(entry.get("library_key") or ""), int(entry.get("rating_key") or 0))
        for entry in (user_report.breakdown or [])
        if entry.get("rating_key")
    }


def _run_shared(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report,
    order_work: list[tuple] | None = None,
) -> tuple[UserRunReport, UserProfile | None]:
    """Deliver one shared 'popular on this server' row from AGGREGATE history.

    Owns its own report row and its own error handling, so one shared row failing never stops the
    others and never leaves the run unaudited. Returns ``(user_report, agg)`` — the synthetic
    profile is a promotion candidate when a row was delivered, else None.
    """
    started = time.monotonic()
    slug = f"{SHARED_SLUG_PREFIX}_{spec.slug}"
    user_report = UserRunReport(username=f"Shared · {spec.slug}", slug=slug)
    report.users.append(user_report)
    try:
        agg = _shared_row(ctx, spec, users, seed_index, library_index, stored_labels, user_report, slug, order_work)
    except Exception as e:  # one shared row's failure never stops the next (rule 6 resume-safety)
        user_report.status = "error"
        user_report.error = f"{type(e).__name__}: {e}"
        logger.exception("shared row '{}': failed", spec.slug)
        agg = None
    finally:
        user_report.duration_s = round(time.monotonic() - started, 2)
        # A shared row has no per-user terminal event, so a skip left the activity feed showing it
        # mid-flight forever and its reason nowhere on screen (issue #3). Emit its outcome like any
        # other participant in the run.
        if user_report.status == "skipped":
            _emit(ctx, slug, "skipped", {}, user_report.reason)
    return user_report, agg


def _shared_row(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    slug: str,
    order_work: list[tuple] | None = None,
) -> UserProfile | None:
    """Build and deliver the shared row's picks (the body ``_run_shared`` guards).

    A title only qualifies once at least ``spec.min_watchers`` distinct people in the audience have
    watched it, so no single person's viewing can reach a public row. Reasons are aggregate-framed —
    never "because you watched X", since there is no single "you".
    """
    cfg = ctx.config
    audience = [u for u in users if spec.audience is None or u.plex_account_id in spec.audience]
    if not audience:
        user_report.status = "skipped"
        user_report.reason = "Nobody in this row's audience is enabled, so there was no history to build it from."
        return None

    base_resolve = _rating_key_resolver(seed_index)

    def resolve(item: WatchedItem) -> int | None:
        return item.tmdb_id or base_resolve(item)

    # Count DISTINCT watchers per title across the audience; keep only titles enough people watched.
    watchers: dict[tuple[int, MediaType], set[int]] = {}
    example: dict[tuple[int, MediaType], WatchedItem] = {}
    for user in audience:
        # Reuse the history _run_user already fetched (same min_completion) rather than re-fetching
        # it per shared row — that was S*A redundant Tautulli/PMS calls. Fall back to a fetch only
        # when it's empty (a user with genuinely none, or whose per-user pass errored before fetching).
        user_history = user.history or ctx.history_source.fetch(user, min_completion=cfg.min_completion)
        for item in user_history:
            tmdb_id = resolve(item)
            if tmdb_id is None:
                continue
            key = (tmdb_id, item.media_type)
            watchers.setdefault(key, set()).add(user.plex_account_id)
            example.setdefault(key, item)
    # Hard floor of 2, regardless of config: a public row must never be shaped by one person's
    # viewing, so a title needs at least two distinct watchers even if the row was set to 1.
    threshold = max(2, spec.min_watchers)
    agg_history = [example[key] for key, who in watchers.items() if len(who) >= threshold]
    user_report.counts.history = len(agg_history)

    agg = UserProfile(
        username="Everyone",
        plex_account_id=0,
        user_type=UserType.SHARED,
        slug=slug,
        history=agg_history,
        # The server-wide list, never the union of everyone's personal blocks: a shared row is public,
        # and letting one person's "don't seed this" reshape what everyone else sees would turn an
        # individual preference into a server-wide edit nobody else can see or undo.
        blocked_seeds=set(cfg.blocked_shared_seeds),
    )
    if not agg_history:
        user_report.status = "skipped"
        # The commonest cause by far is a shared row whose audience is smaller than the floor, where
        # it is arithmetically unreachable — so say which it is rather than leaving someone to
        # conclude the app is broken (issue #3).
        #
        # "in this row's audience" is the honest phrase, NOT "enabled": `users` has already been
        # narrowed to people who are enabled AND not paused, then narrowed again by spec.audience.
        # Saying "only 1 user is enabled" to someone looking at ten enabled users on the Users page
        # is exactly the kind of confidently-wrong explanation that sends them back to the tracker.
        who = f"{len(audience)} {'person' if len(audience) == 1 else 'people'}"
        user_report.reason = (
            f"No title has been watched by {threshold} or more of the {who} in this row's audience yet. "
            f"A shared row is built only from titles several people have watched, so it needs {threshold} "
            f"of them with some viewing in common."
            if len(audience) >= threshold
            else f"A shared row needs at least {threshold} people with overlapping viewing, but only {who} "
            f"{'is' if len(audience) == 1 else 'are'} in this row's audience and active in runs (enabled, "
            f"not paused) — so it can never build. Add more people to the audience, or make this a "
            f"per-person row so each of them gets their own."
        )
        logger.info("shared row '{}': no title watched by >= {} people yet", spec.slug, threshold)
        return None

    # A shared row cycles off the row alone — its "owner" is the audience, not a person, so there is no
    # user slug to stagger by. Everyone sees the same shared row, so there is nothing to stagger.
    # "Popular on this server" is a COUNT, not a recommendation — so nothing is searched for and no
    # LLM is asked.
    #
    # It used to derive SEEDS from the pooled history and run the same TMDB-similar + web-search
    # pipeline a per-person row uses. `_candidate_pool` excludes the seeds from its own results (a
    # recommendation you have already watched is the thing a row shouldn't surface), and here the
    # seeds ARE the popular titles — so the most-watched titles on the server were structurally
    # barred from the row named after them, and every pick was a similar-title suggestion hard-stamped
    # "Popular on this server". On a live server that cost ~10k AI tokens and a minute a night to
    # produce a list this `sorted()` answers exactly (owner decision, 2026-08-13).
    # The owner's server-wide block list still applies. It used to act at seed derivation only, which
    # meant a blocked title could reappear via another seed's similar-titles; with no search left
    # there is one place to apply it and blocking now simply keeps the title out — which is what the
    # setting has always claimed to do.
    blocked = set(cfg.blocked_shared_seeds)
    ranked_titles = sorted(
        ((key, len(who)) for key, who in watchers.items() if len(who) >= threshold and key[0] not in blocked),
        # Watcher count, then title as a stable tiebreak so a re-run reproduces the same row rather
        # than reshuffling everything that drew level.
        key=lambda kv: (-kv[1], example[kv[0]].title.lower()),
    )
    k = spec.size
    targets = target_sections(ctx.delivery_sections, spec)
    library_names = {section.key: getattr(section, "title", "") or "" for section in targets}
    # Built PER LIBRARY, exactly like a per-person row: each targeted library gets its own full k from
    # its own contents, so a 'both' row can never come back all-movies-no-shows.
    section_picks: dict[str, list[Pick]] = {}
    for section in targets:
        kind = section_kind(section)
        sec_idx = ctx.section_index.get(section.key, {})
        sec_picks: list[Pick] = []
        for (tmdb_id, media_type), count in ranked_titles:
            if media_type is not kind:
                continue
            # In THIS library, or it is not on offer: the row is what the server has and people
            # watched, never something it would have to acquire.
            rating_key = sec_idx.get(tmdb_id)
            if rating_key is None:
                continue
            item = example[(tmdb_id, media_type)]
            sec_picks.append(
                Pick(
                    tmdb_id=tmdb_id,
                    rating_key=rating_key,
                    title=item.title,
                    rank=len(sec_picks) + 1,
                    # The real number, not a fixed label. It is the entire reason the title is here,
                    # and the old constant "Popular on this server" was untrue of every pick it sat on.
                    reason=f"{count} people watched it",
                    media_type=media_type,
                    collection_slug=spec.slug,
                    section_key=section.key,
                    library=library_names.get(section.key, ""),
                    # No `sources`: the reason already says "19 people watched it", which IS the
                    # provenance, and a second line reading "suggested by watched" only repeated it
                    # more clumsily. seed_* stay None too, so a {top_seed} name template still has
                    # nothing to surface and a shared row can never be titled after one person.
                    year=item.year,
                )
            )
            if len(sec_picks) >= k:
                break
        if sec_picks:
            # `pick_order` is presentation and still applies. "Highest rated" has no score to sort on
            # here — no TMDB lookup happens — so it leaves the popularity order alone, which is the
            # honest fallback for a row whose ranking IS the count.
            section_picks[section.key] = _apply_order(
                sec_picks, spec.pick_order, row_slug=spec.slug, user_slug="", run_day=ctx.run_day
            )
    picks = [pick for sp in section_picks.values() for pick in sp]

    user_report.picks = picks
    user_report.counts.picks = len(picks)
    user_report.status = "ok"
    user_report.diff = CollectionDiff()
    _emit(ctx, slug, "delivering", {"picks": len(picks)})
    deliver_rows(
        ctx.plex,
        agg,
        picks,
        cfg,
        spec,
        sole_row=True,  # one shared row per label
        dry_run=cfg.dry_run,
        stored_labels=stored_labels,
        diff=user_report.diff,
        sections=ctx.delivery_sections,
        section_index=ctx.section_index,
        section_picks=section_picks,
        breakdown=user_report.breakdown,
        order_work=order_work,
    )
    return agg if picks else None


# Below this, a title is in the pool because TMDB mentioned it somewhere near the bottom of a list,
# not because it resembles anything the person watched. A row of four genuinely-similar titles is
# worth more than ten where six are filler — and filler is what a beta user saw when a medical drama
# produced The Sandman, Servant and Torchwood, each captioned "Because you watched The Pitt".
# Sources with no ranking of their own sit at the neutral 1.0 and so are never filtered out here.
MIN_FILLER_AFFINITY = 0.35


def _log_row_provenance(
    user: UserProfile,
    spec: RowSpec,
    section,
    picks: list[Pick],
    pool: list[Candidate],
    wanted: int,
) -> None:
    """Explain a finished row in the log: what went in, and what was rejected as too loose.

    A beta user reported a medical-drama row full of fantasy, and answering "why?" meant querying
    TMDB by hand — nothing in the log said where any pick came from or how strong the claim was. One
    DEBUG block per row makes the same question answerable from a downloaded log.
    """
    label = f"{user.username}/{spec.slug}@{getattr(section, 'title', '?')}"
    if not picks:
        logger.debug("{}: no picks — {} candidates, none worth delivering", label, len(pool))
        return
    logger.debug("{}: {} picks from {} candidates (row size {})", label, len(picks), len(pool), wanted)
    for pick in picks:
        logger.debug(
            "  #{} {} — {} · {} · affinity {:.2f}",
            pick.rank,
            pick.title,
            f"seed {pick.seed_title}" if pick.seed_title else "no seed",
            "+".join(pick.sources) or "source not recorded",
            pick.affinity,
        )
    if len(picks) < wanted:
        # The row is deliberately short: `_pad_picks` refused to fill it from the tail. Say so, or
        # it reads as a bug — a short row is the fix working, not the pipeline failing.
        too_loose = [c for c in pool if c.affinity < MIN_FILLER_AFFINITY]
        logger.info(
            "{}: row is {} short of {} — {} candidate(s) were too loosely related to deliver{}",
            label,
            wanted - len(picks),
            wanted,
            len(too_loose),
            f" (closest rejected: {max(too_loose, key=lambda c: c.affinity).title})" if too_loose else "",
        )


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up a short row from the ranked pool (never invents titles).

    Only from candidates whose source actually vouched for them: padding is where a weak association
    turns into a delivered row, so the row is allowed to come up short instead.
    """
    have = {(p.tmdb_id, p.media_type) for p in picks}  # movie 1399 and TV 1399 are different titles
    worth_it = [c for c in ranked if c.affinity >= MIN_FILLER_AFFINITY]
    if len(worth_it) < len(ranked):
        logger.debug(
            "padding: {} of {} candidates were too loosely related to deliver",
            len(ranked) - len(worth_it),
            len(ranked),
        )
    fillers = picker.build_picks([c for c in worth_it if (c.tmdb_id, c.media_type) not in have], k - len(picks))
    out = list(picks)
    for f in fillers:
        out.append(replace(f, rank=len(out) + 1))
    return out


def _cold_start_picks(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, k: int = 0) -> list[Pick]:
    """ "Popular on <server>" fallback for a user with thin history: top-rated titles.

    Splits the picks across ``sections_by_type()`` — one representative library per media type, not
    every library on the server — so a movies-only cold start doesn't hand delivery a pick list with
    no shows in it, leaving a TV watcher with a row of films they never asked for on a thin-history
    night (a Tautulli outage is enough).
    """
    sections = ctx.plex.sections_by_type()
    if not sections:
        return []
    k = k if k else cfg.row_size
    share = max(1, k // len(sections))

    picks: list[Pick] = []
    for index, (kind, section) in enumerate(sections.items()):
        # The last library takes the remainder, so `row_size` titles are delivered, not k - k % n.
        wanted = k - len(picks) if index == len(sections) - 1 else min(share, k - len(picks))
        if wanted <= 0:
            break
        for tmdb_id, item in ctx.plex.top_rated(section, wanted):
            picks.append(
                Pick(
                    tmdb_id=tmdb_id,
                    rating_key=item.ratingKey,
                    title=item.title,
                    rank=len(picks) + 1,
                    reason="Popular on this server",
                    media_type=kind,
                    sources=["cold_start"],  # no history to work from — say so rather than imply a match
                )
            )
    return picks
