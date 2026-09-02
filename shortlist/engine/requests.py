"""Request missing picks: ask for titles the curator wanted that the library lacks.

The engine already drops every candidate that isn't in a delivery library (``filter_candidates``);
this module keeps a record of those drops instead, ranks them by how many people wanted them and how
well-regarded they are, and — only when the owner has turned requests on — asks for the top few. It
never touches Plex, so it lives entirely outside the privacy machinery: a request pass can fail
without affecting a single row's visibility.

Two routes, chosen by ``RequestConfig`` and never both at once: straight to Radarr/Sonarr
(``clients/arr.py``), or as a request in Overseerr/Jellyseerr (``clients/seerr.py``), which then
drives those apps under its own rules. Each seam below branches once, at the top.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import NamedTuple

from loguru import logger

from shortlist.engine.clients.arr import ArrError, RadarrClient, SonarrClient
from shortlist.engine.clients.mdblist import (
    RATING_CACHE_TTL_S,
    VOTE_SOURCES,
    MdbListClient,
    MdbListRateLimitError,
)
from shortlist.engine.clients.seerr import SeerrClient, SeerrError
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import (
    OTHER_LANGUAGE_BAR_GAP,
    ArrTarget,
    Candidate,
    MediaType,
    MissingTitle,
    RequestConfig,
    RequestOutcome,
    RequestReport,
    RequestWhy,
    SeerrTarget,
)
from shortlist.engine.request_alloc import allocate

# When gating on a non-TMDB source, only so many candidates are rated on MDBList per run, so a large
# missing pool can't blow the daily cap (each title's whole rating set is cached, so re-runs mostly
# hit the cache). It scales with `requests.max_per_run` so that raising the cap really does let a run
# look harder — but it does NOT bottom out at the cap, and that floor is the important part.
#
# `max_per_run` means "how many to auto-send per run"; everything qualifying beyond it overflows into
# the Waiting inbox rather than being lost. So it is a SEND rate, and the owner sets it low on purpose
# — 3 a night is a perfectly sane answer to "how much should this add to my library". Deriving the
# evaluation depth from it alone made a modest send rate also mean "and barely look", which is not
# what the setting says and not what anyone choosing 3 is asking for. The floor is what keeps the two
# apart: a run figures out the field properly, sends `max_per_run` of it, and queues the rest.
#
# The floor WAS 20, chosen to keep the then-default (5 x 4) byte-identical. In production that turned
# out to be far too shallow: at the observed ~10% pass rate, 20 lookups yield ~2 qualifying titles,
# and against a pool of thousands the run simply never saw anything good (2026-08-13..18, five days
# at `0 qualifying`). 100 costs ~10% of MDBList's free daily allowance for a nightly run and gives
# the gate enough to actually find something.
#
# The quota is protected by the rate-limit fallback below (a 429 stops the gate, falls back to TMDB
# for the whole run and raises an owner notification), not by quietly honouring a smaller number than
# the owner asked for.
_LOOKUP_HEADROOM = 4
_MIN_LOOKUP_POOL = 100


def _walk_limit(budget: int) -> int:
    """A hard stop on how far the gate will WALK, distinct from what it may SPEND.

    Quota is capped by the caller's budget; this only bounds the pathological pool whose head is
    thousands of already-cached titles, where every step is a cheap local read and the loop would
    otherwise scan the lot.

    Derived, never flat, because the head is made of cached ratings and its length is a function of
    the spend rate. Two things pile up in front of the frontier, and the walk has to clear BOTH or the
    row is starved again by its own cache — the very bug this module was fixed for:

        deferred rejects   budget/day for _REJECT_RECHECK_TTL_S   (60 days)
        near misses        budget/day for RATING_CACHE_TTL_S      (7 days, the normal re-check)

    Plus one more budget, so a run always has room to reach that much NEW ground beyond the head.
    Counting only the rejects left a 60-day head against a 61-budget walk, which the weekly-cached
    near misses then overflowed — measured at 3,350 against 3,050 for a two-row split of 100.
    """
    days = (_REJECT_RECHECK_TTL_S + RATING_CACHE_TTL_S) // (24 * 3600)
    return budget * (days + 1)


# How long a title that missed the rating floor by MORE than `_NEAR_MISS` keeps its cached score,
# instead of the usual week. This is what stops the starvation fix from undoing itself.
#
# Ratings cache for 7 days, so in a steady state the number of titles expiring each night equals the
# number rated each night, which equals the budget — the whole allowance would go on re-rating the
# same high-demand rejects and the walk would stop advancing again, about a week after the fix. A
# title 0.5 below the bar does not climb over it in a week, so re-asking is pure cost. Near misses
# are deliberately NOT deferred: those are the ones that genuinely can cross, so they keep the normal
# weekly re-check.
#
# It defers RE-FETCHING, never the verdict: the score stays readable, so lowering `min_rating` still
# admits a deferred title on the very next run, for free.
_NEAR_MISS = 0.5
_REJECT_RECHECK_TTL_S = 60 * 24 * 3600


def _lookup_budget(max_per_run: int) -> int:
    """How many BILLABLE MDBList lookups this run may spend, from the owner's own request cap.

    A budget of calls, not of titles: cached ratings cost nothing and are walked past for free (see
    ``_gate_by_source``), so this is the number of titles the run may rate that it has not rated
    recently — which is what MDBList's daily allowance actually meters.
    """
    return max(_MIN_LOOKUP_POOL, max(0, max_per_run) * _LOOKUP_HEADROOM)


# Demand accumulator: (tmdb_id, media_type) -> the missing title and how many users wanted it. The
# pair, never the bare id — movie 1399 and TV 1399 are different titles (see filter_candidates).
DemandMap = dict[tuple[int, MediaType], MissingTitle]

# One demand map PER ROW, keyed by row slug, in run order. `min_demand` is a per-row floor, so a
# single flat map would have a row set to "3 people" counting wanters from other rows entirely.
RowDemand = dict[str, DemandMap]


def collect_missing(pool: list[Candidate], library_index: dict[MediaType, dict[int, int]]) -> list[Candidate]:
    """Candidates from the pool that no delivery library holds — the requestable titles.

    Mirrors ``filter_candidates``'s library test exactly (a title is 'present' iff its
    (tmdb_id, media_type) maps to a ratingKey), so the two can never disagree about what's missing.
    Watched/excluded/stale filtering is intentionally NOT applied: those shape one user's row, but a
    title being absent from the server is a fact about the server, and worth requesting regardless.
    """
    return [c for c in pool if library_index.get(c.media_type, {}).get(c.tmdb_id) is None]


def accumulate(
    demand: DemandMap,
    missing: list[Candidate],
    tags: set[str] | None = None,
    wanter: str | None = None,
    why: list[RequestWhy] | None = None,
) -> None:
    """Fold one user's missing candidates into the run-wide demand map, counting distinct wanters.

    ``tags`` are the request tags to attach to every title in this batch — the wanting user's own tag
    plus each row they're in the audience of. They accumulate across users, so a title three people
    want carries the union of all three users' (and their rows') tags when it's finally requested.

    ``wanter`` is the username whose taste surfaced these titles; it's collected into each title's
    ``wanters`` so the inbox can show WHO drove the demand, not just the count. ``why`` is the fuller
    provenance for the same batch (one entry per row that surfaced it, with the seed/source), merged
    and de-duplicated so the inbox can explain which row each request came from and why.
    """
    tags = {t for t in (tags or set()) if t}
    who = {wanter} if wanter else set()
    reasons = list(why or [])
    for c in missing:
        key = (c.tmdb_id, c.media_type)
        existing = demand.get(key)
        if existing is None:
            demand[key] = MissingTitle(
                tmdb_id=c.tmdb_id,
                title=c.title,
                media_type=c.media_type,
                year=c.year,
                rating=c.rating,
                vote_count=c.vote_count,
                poster_path=c.poster_path,
                overview=c.overview,
                language=c.language,
                demand=1,
                tags=set(tags),
                wanters=set(who),
                why=list(reasons),
            )
        else:
            existing.demand += 1
            # A title several people wanted may have arrived from a poster-less source for one of
            # them and a TMDB list for another — keep whichever copy actually has the artwork.
            existing.poster_path = existing.poster_path or c.poster_path
            existing.overview = existing.overview or c.overview
            # Same rule as the artwork: one person's copy may have come from Trakt (no language) and
            # another's from TMDB. Keep whichever copy actually knows, or the bar is picked blind.
            existing.language = existing.language or c.language
            existing.tags |= tags
            existing.wanters |= who
            for reason in reasons:
                if reason not in existing.why:
                    existing.why.append(reason)


# The leading words of every reason a QUALIFYING title can be held back from auto-send. Exported
# because `run_persistence._is_failure_detail` classifies a stored `detail` by them: anything not
# starting with one of these came from an exception while actually talking to Radarr/Sonarr, and a
# real failure must outlive a mere threshold note. Reword a reason below and this moves with it —
# restating the strings in the classifier is what would let them drift apart silently.
QUEUE_REASON_PREFIXES = (
    "auto-send is off",
    "this row's own limit",
    "on an Arr exclusion list",
    "on the blocklist",  # the same fact, on the *seerr route
    "demand below",
    "rating below",
    "max_per_run",
)
# "rating below" above already covers the other-language reason ("rating below the bar for other
# languages (8.5)") — it is a threshold note, not a failure, and must classify as one. Spelled out
# here because the shared prefix is a coincidence of wording: reword either string independently and
# the classifier silently starts calling a held title a failed request.


def _within_year_window(year: int | None, min_year: int, max_year: int) -> bool:
    """Whether a candidate's release year falls inside the requested window.

    Both bounds are inclusive; ``<= 0`` disables that end (so ``0, 0`` accepts everything). A show's
    ``year`` is its first-air year (set at the candidate source). When a bound is active but the
    title has no year, it is excluded — a year restriction can't be judged against an unknown date,
    so the conservative choice is not to auto-request it.
    """
    if min_year <= 0 and max_year <= 0:
        return True
    if year is None:
        return False
    return (min_year <= 0 or year >= min_year) and (max_year <= 0 or year <= max_year)


def is_preferred_language(cfg: RequestConfig, language: str) -> bool:
    """Whether this title counts as one of the owner's preferred languages.

    An UNKNOWN language ("") counts as preferred, and that is a deliberate choice with a real cost,
    not a free one. Only `merge()`-built candidates lack a language — in practice Trakt, an opt-in
    source — because every TMDB list response carries `original_language`. We COULD resolve those:
    TMDB's detail endpoint has the field. What makes it the wrong trade is WHERE the answer is
    needed. The base gate runs over the whole demand pool (thousands of titles on a real server),
    before anything has been narrowed, so resolving there is a detail call per unknown title on every
    run. Doing it later — in `_enrich`, which already fetches that same cached payload for the gated
    few — would fix "prefer" and leave "only" judging the same title differently, which is worse than
    one rule applied consistently.

    So the rule is: unknown is preferred, deliberately permissive, so that enabling a second
    candidate source never silently stops it contributing. The year window makes the opposite call
    because a year bound is a hard restriction, where this is a preference.

    Consistency across rows is handled separately — see `_agree_on_language`.
    """
    return not language or language in cfg.preferred_languages


def _agree_on_language(rows: list[RowRequest]) -> None:
    """Give every row's copy of a title the language whichever row actually knows it.

    Per-row demand splits one title into one object per row, and each row's copy is built from its
    OWN users' candidates — so a title one row saw only via Trakt carries "" while another row's copy,
    found via TMDB, carries "ja". The two rows would then judge the same title against different
    bars, and in "only" mode the row holding the blank copy can request a title the owner said never
    to ask for.

    Runs BEFORE `_gate_rows`, unlike `_merge_across_rows` (tags/wanters/why), and that is the whole
    point: "only" mode drops at the base gate, so a fold that happens after it is already too late.

    Costs one pass over the pool and no API calls — it only moves knowledge the run already has.
    """
    known: dict[tuple[int, MediaType], str] = {}
    for row in rows:
        for m in row.demand.values():
            key = (m.tmdb_id, m.media_type)
            if m.language and not known.get(key):
                known[key] = m.language
    if not known:
        return
    for row in rows:
        for m in row.demand.values():
            if not m.language:
                m.language = known.get((m.tmdb_id, m.media_type), "")


def other_language_bar(cfg: RequestConfig) -> float:
    """The auto-send rating bar a non-preferred title must clear, resolved for THIS row's config.

    Follows the row's own ``min_rating`` plus :data:`OTHER_LANGUAGE_BAR_GAP` until the owner sets a
    number, so a row that raises its base floor carries this bar up with it and the shipped default
    encodes the owner's taste rather than ours.

    CLAMPED to 10 and rounded to one decimal, and both matter:

    * ``min_rating`` may legally be up to 10, so the raw sum reaches 11.5 — a bar no rating can
      clear, which turns "prefer" into "hold back every other language" without ever saying so.
    * The settings API accepts two decimals (``7.25``), so an unrounded ``8.75`` would disagree with
      the ``8.8`` the settings screen shows for the same server.

    ``floor(x * 10 + 0.5) / 10`` rather than the obvious ``round(x, 1)``, because this rule has a
    second implementation — ``otherLanguageBar`` in ``web/src/lib/request-language.ts`` — and the two
    have to agree on every input. They did not: Python's ``round`` is banker's rounding applied to the
    true binary value, while JavaScript's ``Math.round`` scales first and rounds half UP, so the pair
    disagreed by 0.1 on 42 of the 1001 two-decimal floors between 0 and 10 (``7.15`` → 8.6 against
    8.7). This expression is JS's ``Math.round`` spelled out, and matches it on all 1001. The engine
    is the source of truth; the TS mirrors it, and neither may be "tidied" alone.
    """
    if cfg.min_rating_other is None:
        return math.floor(min(10.0, cfg.min_rating + OTHER_LANGUAGE_BAR_GAP) * 10 + 0.5) / 10
    return cfg.min_rating_other


def _language_allowed(cfg: RequestConfig, m: MissingTitle) -> bool:
    """Whether the base gate may keep this title at all, on language grounds.

    Only ``"only"`` mode ever answers False, and that is the one language decision that DISCARDS: a
    title dropped here never reaches the inbox, exactly like one below ``min_rating``. ``"prefer"``
    never drops — it holds back at the auto tier instead, where the reason survives onto the title
    and the owner can still say yes.

    An EMPTY preferred list in ``"only"`` mode rejects everything, unknown included. That is the one
    state where :func:`is_preferred_language`'s permissiveness has nothing to be permissive relative
    TO: "only these languages" with none listed reads as "nothing", and it is what the settings
    screen, the row editor and the reference table all promise. Without this, a server in that state
    still auto-sent every Trakt-sourced title — the copy and the code disagreeing on a path that adds
    titles to Radarr.

    That also decides how a POISONED setting behaves, and the direction is deliberate.
    `normalise_languages` degrades an unusable stored value to `()` rather than raising, so a corrupt
    list lands in exactly this branch: in ``"only"`` mode the run now requests NOTHING, where before
    it would have requested everything. Failing closed is the right way round here — this path adds
    titles to someone's library, and a run that asks for nothing is a visible non-event the owner can
    investigate, while a run that asks for everything is a mess to undo.
    """
    if cfg.language_mode != "only":
        return True
    if not cfg.preferred_languages:
        return False
    return is_preferred_language(cfg, m.language)


def _gate_rows(
    rows: list[RowRequest],
    handled: set[tuple[int, str]],
    report: RequestReport,
    *,
    mdblist: MdbListClient | None,
    budget: int,
) -> list[tuple[str, RequestConfig, list[MissingTitle]]]:
    """Apply each row's base floors and then its rating gate, sharing one lookup budget between them.

    Each row gets an even share of what is LEFT rather than a fixed slice, so a row that spends
    little hands the rest on automatically — the same redistribution the slot allocator does.

    A spent MDBList quota (``ratings_rate_limited``) short-circuits every LATER row straight to TMDB:
    the allowance is gone for the day, so asking again can only produce another 429. Without it each
    row fired its own doomed request and logged its own "daily limit reached", turning one event into
    N wasted calls against an API already refusing us.
    """
    gated: list[tuple[str, RequestConfig, list[MissingTitle]]] = []
    # From the RUN's budget, not a row's share of it — see `_gate_by_source`. Every row walks past the
    # same run-wide rating cache, so every row gets the same allowance for getting past it.
    walk_limit = _walk_limit(budget)
    # TITLES dropped for language, deduplicated across rows — never a per-row sum. `report.wanted` is
    # a deduplicated set of keys, and the notification compares the two ("did the language setting
    # rule out EVERYTHING the run wanted?"). A sum counts a title two rows both wanted twice, so it
    # can exceed `wanted` and claim the language setting is the sole cause when it is not — the very
    # mis-attribution this counter exists to prevent, reintroduced one level up.
    dropped_language_keys: set[tuple[int, MediaType]] = set()
    for index, row in enumerate(rows):
        in_play = [
            m
            for m in row.demand.values()
            if (m.tmdb_id, str(m.media_type)) not in handled
            and m.demand >= row.cfg.min_demand
            and _within_year_window(m.year, row.cfg.min_year, row.cfg.max_year)
        ]
        pool = [m for m in in_play if _language_allowed(row.cfg, m)]
        # Counted separately from the demand/year drops, because the "nothing qualified" alert tells
        # the owner WHICH limit to loosen. A pool emptied by "only" mode would otherwise be reported
        # as their demand and year settings being too tight — advice they could follow forever
        # without effect. This codebase has fixed that exact mis-attribution once already (step 4
        # below, "Name the limit that ACTUALLY bound").
        kept = {(m.tmdb_id, m.media_type) for m in pool}
        dropped_language_keys |= {(m.tmdb_id, m.media_type) for m in in_play} - kept
        report.pool_size += len(pool)
        report.pool_by_row[row.slug] = len(pool)
        share = -(-(budget - report.lookups_spent) // (len(rows) - index))  # ceiling division
        before_examined = report.examined
        if row.cfg.rating_source != "tmdb" and mdblist is not None and not report.ratings_rate_limited:
            qualifying = _gate_by_source(row.cfg, mdblist, pool, report, budget=share, walk_limit=walk_limit)
        else:
            qualifying = _gate_by_tmdb(row.cfg, pool)
            report.examined += len(pool)  # the TMDB gate reads the whole pool and bills nothing
        report.examined_by_row[row.slug] = report.examined - before_examined
        gated.append((row.slug, row.cfg, qualifying))
    report.dropped_by_language = len(dropped_language_keys)
    return gated


def _auto_eligible(
    cfg: RequestConfig, survivors: list[MissingTitle], blocked: Counter[str]
) -> tuple[list[MissingTitle], list[MissingTitle]]:
    """Split one row's qualifying titles into ``(eligible, held_back)`` on its auto-send bar.

    Both lists are returned rather than deriving one from the other by membership: ``MissingTitle`` is
    a plain dataclass, so ``in`` compares by VALUE, and two distinct titles that happen to match
    field-for-field would collapse into one.

    The run cap is deliberately NOT applied here: allocation decides who gets the slots, so no row can
    fill the cap before another has been considered. A held-back title keeps its reason ON itself —
    that is what the DB row and the run trace carry, and the only answer to "why didn't THIS one go?".
    """
    eligible: list[MissingTitle] = []
    held_back: list[MissingTitle] = []
    other_bar = other_language_bar(cfg)
    for m in survivors:  # already ranked best-first by the gate
        if not cfg.auto_send:
            reason = "auto-send is off"
        elif m.excluded:
            # Named for the route: the Arrs call it an import-exclusion list and a *seerr calls it a
            # blocklist, and a reason line that names the wrong one sends the owner to the wrong app
            # to undo it. Both spellings are in QUEUE_REASON_PREFIXES.
            #
            # Written as a plain if/else with a literal in each arm, NOT a conditional expression:
            # `test_every_queue_reason_the_gate_can_emit_classifies_as_a_threshold` walks this
            # function's AST and refuses any reason it cannot read as a literal, precisely so a
            # reason built by a lookup or a helper cannot slip past the classifier unchecked. Ruff's
            # SIM108 asks for the ternary that test rejects, so it is silenced rather than obeyed —
            # the linter is arguing about shape, the test is protecting a contract.
            if cfg.target == "overseerr":  # noqa: SIM108 — see above; a ternary fails that test
                reason = "on the blocklist"
            else:
                reason = "on an Arr exclusion list"
        elif m.demand < cfg.auto_min_demand:
            reason = f"demand below auto_min_demand ({cfg.auto_min_demand})"
        elif m.rating < cfg.auto_min_rating:
            reason = f"rating below auto_min_rating ({cfg.auto_min_rating})"
        elif cfg.language_mode == "prefer" and not is_preferred_language(cfg, m.language) and m.rating < other_bar:
            reason = f"rating below the bar for other languages ({other_bar})"
        else:
            eligible.append(m)
            continue
        blocked[reason] += 1
        m.detail = reason
        held_back.append(m)
    return eligible, held_back


class RowRequest(NamedTuple):
    """One row's slice of the request pass: its slug, its effective config, and its own demand map."""

    slug: str
    cfg: RequestConfig
    demand: DemandMap


def request_missing(
    base_cfg: RequestConfig,
    tmdb: TmdbClient,
    rows: list[RowRequest],
    *,
    dry_run: bool,
    min_write_interval: float = 1.0,
    already_handled: set[tuple[int, str]] | None = None,
    mdblist: MdbListClient | None = None,
) -> RequestReport:
    """Auto-request the strongest missing titles per row; queue the rest for the owner to approve.

    Each row is gated on ITS OWN floors and its own share of the rating-lookup budget, so one row can
    neither spend the whole allowance nor take every slot. ``rows`` is in RUN ORDER, which decides the
    even-split remainder and which row claims a title several rows want.

    The two ceilings stay global and come from ``base_cfg``: ``max_per_run`` (the run's request cap)
    and the lookup budget derived from it. A row may only make itself more restrictive.

    One title's failure never stops the rest: each is caught and recorded as its own outcome.
    """
    report = RequestReport(warnings=list(base_cfg.incomplete_targets))
    # Titles the owner has already actioned — asked for, or said no to — are out of the running
    # entirely. Two bugs lived here: a title still DOWNLOADING was still "missing", so it re-won a
    # slot every night and `max_per_run` starved forever on the same five titles; and a REJECTED
    # title could still be auto-sent later, so a "no" wasn't a no.
    handled = already_handled or set()
    budget = _lookup_budget(base_cfg.max_per_run)
    # Counted BEFORE any floor, and deduplicated across rows: it is what separates "nothing was
    # missing" (fine) from "plenty was missing and your floors rejected all of it" (actionable).
    # Counted NET of titles the owner already actioned. A server whose whole inbox was rejected sits
    # permanently at "wanted > 0, pool == 0" — the titles were rejected, so they are never requested,
    # so they stay missing, so they stay in demand — and the alert then tells the owner to loosen
    # floors that were never the reason. Only titles still in play count as wanted.
    report.wanted = len({key for row in rows for key in row.demand if (key[0], str(key[1])) not in handled})

    # Before any gate: make every row's copy of a title agree on what language it is. "only" mode
    # drops at the base gate, so a row holding a blank copy would otherwise request a title another
    # row already knew was excluded.
    _agree_on_language(rows)

    gated = _gate_rows(rows, handled, report, mdblist=mdblist, budget=budget)

    # 1. One reconcile for the whole run against whichever app is the route: what it already holds
    #    is a fact about the server, not about a row, and the per-row targets differ only in profile
    #    and root folder (and not at all on the *seerr route, which has neither). Built from
    #    `base_cfg` for the same reason. Fails OPEN — see `_apply_arr_state` / `_apply_seerr_state`.
    seerr = SeerrClient(base_cfg.overseerr, min_write_interval=min_write_interval) if base_cfg.overseerr else None
    radarr = RadarrClient(base_cfg.radarr, min_write_interval=min_write_interval) if base_cfg.radarr else None
    sonarr = SonarrClient(base_cfg.sonarr, min_write_interval=min_write_interval) if base_cfg.sonarr else None
    flat = [m for _, _, qualifying in gated for m in qualifying]
    if seerr is not None:
        kept, in_arr, report.arr_present = _apply_seerr_state(flat, seerr)
    else:
        kept, in_arr, report.arr_present = _apply_arr_state(tmdb, flat, radarr, sonarr)
    kept_keys = {(m.tmdb_id, m.media_type) for m in kept}
    # Fold every row's evidence about a title into every copy of it, BEFORE one of them is claimed
    # and sent — the claimed copy has to carry all of it (see _merge_across_rows).
    _merge_across_rows(gated)
    if in_arr:
        # Names the app actually asked. It read "already in Sonarr/Radarr" on the Overseerr route,
        # which is the sort of log line that sends someone to check the wrong server.
        logger.info(
            "requests: {} qualifying already in {} — dropped", in_arr, "Overseerr" if seerr else "Sonarr/Radarr"
        )

    # 2. Per row: keep what survived the Arr drop, enrich it, and split auto-eligible from queued on
    #    THIS row's auto-send bar. The run cap is deliberately NOT applied here — allocation below
    #    decides who gets the slots, so no row can fill the cap before another is considered.
    blocked: Counter[str] = Counter()
    auto_by_row: list[tuple[str, list[MissingTitle]]] = []
    cfg_by_row: dict[str, RequestConfig] = {}
    for slug, cfg, qualifying in gated:
        survivors = [m for m in qualifying if (m.tmdb_id, m.media_type) in kept_keys]
        report.considered += len(survivors)
        report.considered_by_row[slug] = len(survivors)
        cfg_by_row[slug] = cfg
        _enrich(tmdb, survivors)
        eligible, held_back = _auto_eligible(cfg, survivors, blocked)
        report.queued.extend(held_back)
        auto_by_row.append((slug, eligible))

    # 3. Divide the run's slots between the rows (even split, surplus redistributed, one slot per
    #    title however many rows wanted it).
    claims = allocate(
        auto_by_row,
        cap=base_cfg.max_per_run,
        row_caps={slug: cfg.max_per_row for slug, cfg in cfg_by_row.items()},
    )
    # Which row's target this went out under. `why` lists every row that WANTED it, so it cannot
    # answer that once provenance is merged across rows — and a later approval has to reuse the row
    # the run actually used, or it files into a different folder.
    #
    # Stamped on EVERY copy of the key, not only the claimed object. A title an earlier row held back
    # is already in `report.queued` as that row's copy, and `_dedupe_queued` keeps the earliest — so
    # stamping only the claim left the surviving inbox row with no slug, falling back to the earliest
    # `why` entry: exactly the mis-file this is meant to prevent.
    claimer_of = {(m.tmdb_id, m.media_type): slug for slug, m in claims}
    for copy in [m for _, eligible in auto_by_row for m in eligible] + report.queued:
        owner = claimer_of.get((copy.tmdb_id, copy.media_type))
        if owner is not None:
            copy.row_slug = owner
    claimed = {(slug, m.tmdb_id, m.media_type) for slug, m in claims}
    for slug, _m in claims:
        report.claimed_by_row[slug] = report.claimed_by_row.get(slug, 0) + 1

    # 4. Anything auto-worthy that missed out is queued rather than lost — including a title a LATER
    #    row offered that an earlier row already claimed, which is not the owner's problem to see
    #    twice, so it is only queued when nobody claimed it at all.
    won = {(m.tmdb_id, m.media_type) for _, m in claims}
    taken_by_row: dict[str, list[MissingTitle]] = {}
    for slug, m in claims:
        taken_by_row.setdefault(slug, []).append(m)
    for slug, eligible in auto_by_row:
        for m in eligible:
            if (slug, m.tmdb_id, m.media_type) in claimed or (m.tmdb_id, m.media_type) in won:
                continue
            # Name the limit that ACTUALLY bound. A row the owner capped (0 = "never asks on its
            # own") was told "max_per_run already filled", pointing at a global setting they could
            # raise forever without effect.
            # The row's own limit only counts as the cause when the owner set one BELOW the run
            # cap and this row reached it. A row that merely INHERITS the run cap is bound by the
            # run, not by itself — and saying otherwise would point at a per-row setting they never
            # touched, the mirror of the bug this fixes.
            row_cap = cfg_by_row[slug].max_per_row
            row_bound = row_cap < base_cfg.max_per_run and len(taken_by_row.get(slug, [])) >= row_cap
            m.detail = (
                f"this row's own limit ({row_cap}) — held for your approval"
                if row_bound
                else f"max_per_run ({base_cfg.max_per_run}) already filled"
            )
            blocked[m.detail] += 1
            report.queued.append(m)

    if blocked:
        logger.info(
            "requests: {} queued rather than auto-sent — {}",
            sum(blocked.values()),
            "; ".join(f"{n} {reason}" for reason, n in blocked.most_common()),
        )

    if not claims:
        report.queued = _dedupe_queued(report.queued, set())
        # Say how far it LOOKED, not just what it found. A bare "0 qualifying" reads identically
        # whether the floors emptied the pool, the gate rejected everything it rated, or it ran out of
        # budget before reaching anything good — and the third is the one the owner can act on.
        logger.info(
            "requests: {} qualifying, 0 auto-sent, {} queued for approval "
            "({} past the floors, {} rated, {} live lookups)",
            report.considered,
            len(report.queued),
            report.pool_size,
            report.examined,
            report.lookups_spent,
        )
        if report.pool_size and not report.considered and report.examined < report.pool_size:
            logger.warning(
                "requests: the rating gate stopped {} titles into a pool of {} — nothing it rated "
                "cleared the rating floor. Raise requests.max_per_run to rate more per run, or lower it.",
                report.examined,
                report.pool_size,
            )
        return report

    # 5. Send, each title under the target of the row that claimed it.
    report.outcomes = _send_claims(
        claims, cfg_by_row, tmdb, dry_run=dry_run, min_write_interval=min_write_interval, seerr=seerr
    )
    # Only the ones the Arr actually accepted. A send that failed, or was skipped for want of a TVDB
    # id, must stay requestable — suppressing it would lose the title silently.
    landed = {(o.tmdb_id, o.media_type) for o in report.outcomes if o.status in ("requested", "would_request")}
    report.sent = [m for _, m in claims if (m.tmdb_id, m.media_type) in landed]
    for slug, m in claims:
        if (m.tmdb_id, m.media_type) in landed:
            report.sent_by_row[slug] = report.sent_by_row.get(slug, 0) + 1
    slug_by_key = {(o.tmdb_id, o.media_type): o.arr_slug for o in report.outcomes}
    for m in report.sent:
        m.arr_slug = slug_by_key.get((m.tmdb_id, m.media_type))
    # A failed auto-send (status "error") used to vanish: it was in neither `sent` nor `queued`, so it
    # never reached the inbox and retried blindly every night. Queue it WITH the reason. Only "error"
    # — the skips are settled facts (already in the Arr, or no TVDB id) and surfacing them is noise.
    fail_detail = {(o.tmdb_id, o.media_type): o.detail for o in report.outcomes if o.status == "error"}
    # EVERY queued copy of a key, not one of them. A dict comprehension here kept the LAST copy while
    # `_dedupe_queued` keeps the FIRST, so with three or more rows holding a title back the Arr's
    # failure was written onto a copy dedupe then discarded — and `detail` is the one field dedupe
    # does not merge. Two rows was not enough to show it; the first version's test used two.
    # Stamping all of them is order-independent and survives dedupe changing its keeper rule.
    queued_by_key: dict[tuple[int, MediaType], list[MissingTitle]] = {}
    for queued in report.queued:
        queued_by_key.setdefault((queued.tmdb_id, queued.media_type), []).append(queued)
    for _, m in claims:
        key = (m.tmdb_id, m.media_type)
        if key not in fail_detail:
            continue
        copies = queued_by_key.get(key)
        if copies:
            # An earlier row already queued this for a threshold reason. The REAL failure has to land
            # on whichever copy survives, else the inbox says "auto-send is off" for a title Radarr
            # rejected — and `_is_failure_detail` reads that as a mere threshold note, so it is not
            # even protected from being overwritten next run.
            for copy in copies:
                copy.detail = fail_detail[key]
            continue
        m.detail = fail_detail[key]
        report.queued.append(m)
    report.queued = _dedupe_queued(report.queued, {(m.tmdb_id, m.media_type) for m in report.sent})
    logger.info(
        "requests: {} of {} auto-{}, {} queued for approval ({} considered across {} row(s))",
        report.requested,
        len(claims),
        "would-send" if dry_run else "sent",
        len(report.queued),
        report.considered,
        len(rows),
    )
    return report


def _merge_across_rows(gated: list[tuple[str, RequestConfig, list[MissingTitle]]]) -> None:
    """Give every row's copy of a title the tags, wanters and provenance of ALL of them.

    Per-row demand splits one title into one object per row, and only the claiming row's object is
    ever sent — so without this a title two rows wanted went to Radarr carrying one row's tag. That
    contradicts the documented contract ("the tags of every per-person row they're in") and breaks
    exactly the thing the tag is for: hanging Radarr/Sonarr rules on which rows asked.

    Demand stays each row's own — it is the floor `min_demand` is checked against, and merging it
    would let one row's popularity satisfy another row's threshold.
    """
    merged: dict[tuple[int, MediaType], tuple[set[str], set[str], list[RequestWhy]]] = {}
    for _slug, _cfg, titles in gated:
        for m in titles:
            tags, wanters, why = merged.setdefault((m.tmdb_id, m.media_type), (set(), set(), []))
            tags |= m.tags
            wanters |= m.wanters
            for reason in m.why:
                if reason not in why:
                    why.append(reason)
    for _slug, _cfg, titles in gated:
        for m in titles:
            tags, wanters, why = merged[(m.tmdb_id, m.media_type)]
            m.tags = set(tags)
            m.wanters = set(wanters)
            m.why = list(why)


def _dedupe_queued(queued: list[MissingTitle], sent: set[tuple[int, MediaType]]) -> list[MissingTitle]:
    """One inbox row per title, however many rows wanted it — and none for a title already sent.

    Per-row demand means a title several rows want exists as several MissingTitle objects, one per
    row. Both used to reach `report.queued`, and `request_candidates` has a UNIQUE constraint on
    (tmdb_id, media_type), so persisting that run died with an IntegrityError — the whole inbox write
    lost, not just the duplicate.

    The earliest row's copy wins, matching the collision rule in `allocate`, but the others' evidence
    is folded in: the inbox exists to say WHO wanted a title and from WHICH row, and dropping a copy
    silently would drop half that answer. `demand` takes the max rather than the sum — a person in two
    rows is one person, and summing would inflate them into two.
    """
    first: dict[tuple[int, MediaType], MissingTitle] = {}
    for m in queued:
        key = (m.tmdb_id, m.media_type)
        if key in sent:
            continue  # it went out under another row; an inbox row would name a title Radarr has
        keeper = first.get(key)
        if keeper is None:
            first[key] = m
            continue
        keeper.tags |= m.tags
        keeper.wanters |= m.wanters
        keeper.demand = max(keeper.demand, m.demand)
        for reason in m.why:
            if reason not in keeper.why:
                keeper.why.append(reason)
    return list(first.values())


def _enrich(tmdb: TmdbClient, titles: list[MissingTitle]) -> None:
    """Fill the inbox's IMDb deep-link, poster and synopsis for titles that survived the Arr drop.

    Deliberately after `_apply_arr_state`, not before — enriching first paid a TMDB detail call per
    title the very next line then discarded. All three are best-effort: a miss leaves the IMDb search
    fallback / a placeholder tile / no synopsis paragraph, never a failed run.
    """
    for m in titles:
        if not m.imdb_id:
            try:
                m.imdb_id = tmdb.imdb_id(m.tmdb_id, m.media_type) or ""
            except Exception as e:  # never fail the run for a link nicety
                logger.debug("imdb id lookup for {!r} failed: {}", m.title, e)
        if not m.poster_path:
            try:
                # `or ""` at the client: a title TMDB has no artwork for must not put None into the
                # NOT NULL column — it would fail the whole request-queue persist, not just this row.
                m.poster_path = tmdb.poster_path(m.tmdb_id, m.media_type)
            except Exception as e:  # never fail the run for a picture
                logger.debug("poster lookup for {!r} failed: {}", m.title, e)
        if not m.overview:
            try:
                # Hits the same detail endpoint the poster lookup just did, and `TMDBClient._get`
                # caches on path — so a title missing both pays one HTTP round-trip, not two.
                m.overview = tmdb.overview(m.tmdb_id, m.media_type)
            except Exception as e:  # never fail the run for a paragraph of text
                logger.debug("synopsis lookup for {!r} failed: {}", m.title, e)


def _send_claims(
    claims: list[tuple[str, MissingTitle]],
    cfg_by_row: dict[str, RequestConfig],
    tmdb: TmdbClient,
    *,
    dry_run: bool,
    min_write_interval: float,
    seerr: SeerrClient | None = None,
) -> list[RequestOutcome]:
    """Send each claimed title under the target of the row that claimed it.

    Clients are cached by their whole target, so rows sharing a profile and folder share one client
    and its rate limiter — the plex-safety throttle is per client, and one per row would multiply the
    write rate by the number of rows.
    """
    clients: dict[ArrTarget | SeerrTarget, RadarrClient | SonarrClient | SeerrClient] = {}
    clocks: dict[str, list[float]] = {}
    # Seed the run's own reconcile client, so the send reuses its memoised media state instead of
    # walking /media a second time. Seeded into the SAME cache rather than special-cased in the loop
    # below: keyed by its target, it is reused only for rows that actually point at that instance,
    # and the loop keeps one code path. The inbox-approval path passes none and builds its own.
    if seerr is not None:
        clients[seerr.target] = seerr
    outcomes: list[RequestOutcome] = []
    for slug, title in claims:
        cfg = cfg_by_row[slug]
        if cfg.target == "overseerr":
            # On the ROUTE, not on the target: a chosen-but-unconnected Overseerr must not fall
            # through to the Arr branch below and be explained in that branch's words.
            target = _cached_client(clients, clocks, cfg.overseerr, SeerrClient, min_write_interval)
            outcomes.append(_request_one_seerr(title, target, dry_run=dry_run))
            continue
        radarr = _cached_client(clients, clocks, cfg.radarr, RadarrClient, min_write_interval)
        sonarr = _cached_client(clients, clocks, cfg.sonarr, SonarrClient, min_write_interval)
        outcomes.append(_request_one(title, radarr, sonarr, tmdb, dry_run=dry_run, sonarr_monitor=cfg.sonarr_monitor))
    return outcomes


def _cached_client(
    clients: dict,
    clocks: dict[str, list[float]],
    target: ArrTarget | SeerrTarget | None,
    factory,
    min_write_interval: float,
):
    """One client per distinct target, but one write clock per SERVER.

    Rows differ only in root folder and quality profile, so several targets are usually the same
    Radarr. A client each is fine — they are cheap and hold per-target state — but a rate limiter each
    would multiply the write rate to that one server by the number of rows (plex-safety rule 6).
    """
    if target is None:
        return None
    if target not in clients:
        clock = clocks.setdefault(target.url.rstrip("/"), [0.0])
        clients[target] = factory(target, min_write_interval=min_write_interval, write_clock=clock)
    return clients[target]


def request_titles_by_row(
    cfg_by_row: dict[str, RequestConfig],
    tmdb: TmdbClient,
    claims: list[tuple[str, MissingTitle]],
    *,
    dry_run: bool,
    min_write_interval: float = 1.0,
) -> RequestReport:
    """Send titles the owner approved from the inbox, each under its own row's target.

    An approval is a delayed send, so it must land exactly where a same-night send would have — which
    means one config per row, not one for the batch. Routed through ``_send_claims`` (the same path a
    run uses) rather than a loop of per-group sends: a loop builds a fresh client per group,
    and several rows are usually the SAME Radarr with different folders, so each group would get its
    own rate limiter and multiply the write rate to one server (plex-safety rule 6).
    """
    report = RequestReport(considered=len(claims))
    report.outcomes = _send_claims(claims, cfg_by_row, tmdb, dry_run=dry_run, min_write_interval=min_write_interval)
    return report


def _apply_arr_state(
    tmdb: TmdbClient,
    pool: list[MissingTitle],
    radarr: RadarrClient | None,
    sonarr: SonarrClient | None,
) -> tuple[list[MissingTitle], int, set[tuple[int, str]]]:
    """Reconcile the gated pool against what the Arrs already know.

    Drops titles an Arr already tracks (they aren't really "missing" — a downloading title just isn't
    in Plex yet) and flags titles on an Arr import-exclusion list (``m.excluded``) so the inbox can
    show why approving one wouldn't add it. Returns ``(kept, dropped_count, arr_present)`` — the last
    is every (tmdb_id, MediaType.value) the Arrs track, for the server's stale-pending-row prune
    (see ``RequestReport.arr_present``); shows land in it via Sonarr v4's own ``tmdbId`` (empty on v3).

    Fails OPEN: any fetch error skips that Arr's checks entirely — a redundant request is a far
    smaller sin than silently dropping a title the owner actually wanted. Shows are matched on TVDB
    (Sonarr's key), resolved once per title and cached on ``m.tvdb_id`` for the later send.
    """
    if not pool:
        return pool, 0, set()
    # Only fetch the sets for media types actually present — a movie-only pool shouldn't pay for
    # Sonarr's two calls, and vice versa.
    want_movies = any(m.media_type is MediaType.MOVIE for m in pool)
    want_shows = any(m.media_type is not MediaType.MOVIE for m in pool)
    movie_present = _safe_ids(radarr.library_tmdb_ids) if radarr and want_movies else set()
    movie_excluded = _safe_ids(radarr.excluded_tmdb_ids) if radarr and want_movies else set()
    show_present, show_present_tmdb = _safe_id_pair(sonarr.library_ids) if sonarr and want_shows else (set(), set())
    show_excluded = _safe_ids(sonarr.excluded_tvdb_ids) if sonarr and want_shows else set()
    arr_present = {(tid, MediaType.MOVIE.value) for tid in movie_present} | {
        (tid, MediaType.SHOW.value) for tid in show_present_tmdb
    }

    kept: list[MissingTitle] = []
    dropped = 0
    for m in pool:
        # Sonarr's own tmdbId settles a show without a TVDB crossing — and MUST settle it, because
        # `arr_present` is keyed that way for the server's stale-row prune. Matching presence on TVDB
        # here and on TMDB there gave two answers to one question: a show whose TVDB id TMDB maps
        # differently (or not at all) was simultaneously "the arr already has it" and "queue it", so
        # the run re-sent it to Sonarr AND the persist both pruned and re-filed one inbox key — which
        # dies on its UNIQUE constraint, losing the whole run's report (issue #104). For movies the
        # two keyings are the same set, so this changes nothing there.
        if (m.tmdb_id, m.media_type.value) in arr_present:
            dropped += 1
            continue
        if m.media_type is MediaType.MOVIE:
            present, excluded = movie_present, movie_excluded
            key: int | None = m.tmdb_id
        else:
            present, excluded = show_present, show_excluded
            # Only pay the TVDB lookup when there's actually a Sonarr set to match against.
            key = _resolve_tvdb(tmdb, m) if (show_present or show_excluded) else None
        if key is not None and key in present:
            dropped += 1
            continue
        if key is not None and key in excluded:
            m.excluded = True  # surfaced as its own inbox flag; the app is inferred from media_type
        kept.append(m)
    return kept, dropped, arr_present


def _apply_seerr_state(
    pool: list[MissingTitle],
    seerr: SeerrClient,
) -> tuple[list[MissingTitle], int, set[tuple[int, str]]]:
    """``_apply_arr_state`` for an Overseerr/Jellyseerr target — one fetch instead of four.

    Overseerr's media table already IS the union of "in the Plex library" and "requested and on its
    way", both keyed by TMDB id for movies AND shows. So there is no TVDB crossing to do here, and
    ``present`` needs no separate exclusion set: a title the instance knows about in any actionable
    state is one Shortlist must not ask for again.

    ``m.excluded`` is set from the instance's BLOCKLIST, which is the *seerr's own spelling of an Arr
    import-exclusion list. Flagged and KEPT rather than dropped — the same choice the Arr twin makes
    — so the inbox can say why approving it would not help, and `_auto_eligible` holds it back from
    auto-send. An instance too old to serve `/blocklist` simply contributes no exclusions.

    Fails OPEN, exactly like its Arr twin: a fetch error skips the check entirely, because a
    redundant request is a far smaller sin than silently dropping a title the owner wanted.

    ``SeerrError`` only, where ``_safe_ids`` casts a deliberately wide net. The client funnels every
    transport and body problem into that one type — including the non-JSON reply that made the Arr
    version broad — so anything else reaching here is a bug in our own code, and this file already
    records what swallowing one of those costs: the ``MediaType.TV`` AttributeError in
    ``api/requests.py`` that made a fallback silently no-op for every show, for ever.
    """
    if not pool:
        return pool, 0, set()
    try:
        state = seerr.media_state()
        # Inside the same guard, not after it. `blocklisted()` swallows its own transport errors, but
        # a shape it cannot parse must not be the one thing in this function that fails the request
        # pass — the whole point here is that a reconcile problem costs a redundant request, never a
        # run.
        blocked = seerr.blocklisted()
    except SeerrError as e:
        logger.warning("Overseerr state fetch failed, skipping that check this run: {}", e)
        return pool, 0, set()
    # Note the flip: `media_state` keys (media_type, tmdb_id) — the shape the inbox reads it in —
    # while `arr_present` is (tmdb_id, media_type). Getting it round the wrong way costs nothing
    # loudly: every lookup simply misses, so the run silently re-requests the whole library.
    known = {(tmdb_id, kind) for (kind, tmdb_id) in state}
    kept: list[MissingTitle] = []
    for m in pool:
        if (m.tmdb_id, m.media_type.value) in known:
            continue
        if (m.media_type.value, m.tmdb_id) in blocked:
            m.excluded = True
        kept.append(m)
    return kept, len(pool) - len(kept), known


def _safe_ids(fetch) -> set[int]:
    """Call an Arr id-set fetch, returning an empty set on ANY error so the check truly fails open.

    Deliberately broad: besides ``ArrError`` (connect/auth/non-200), a 200 with a non-JSON body — an
    SSO interstitial or SPA HTML from a misconfigured reverse proxy — makes ``r.json()`` raise
    ``ValueError``. Either way, skip the check and request as if the Arr held nothing, never abort the
    whole pass (which would drop every wanted title on a proxy hiccup).
    """
    try:
        return fetch()
    except Exception as e:
        logger.warning("Arr state fetch failed, skipping that check this run: {}", e)
        return set()


def _safe_id_pair(fetch) -> tuple[set[int], set[int]]:
    """``_safe_ids`` for a fetch returning a pair of id sets (``SonarrClient.library_ids``)."""
    try:
        return fetch()
    except Exception as e:
        logger.warning("Arr state fetch failed, skipping that check this run: {}", e)
        return set(), set()


def _resolve_tvdb(tmdb: TmdbClient, m: MissingTitle) -> int | None:
    """A show's TVDB id, cached on the title so presence-check and send don't each look it up."""
    if m.tvdb_id is None:
        try:
            m.tvdb_id = tmdb.tvdb_id(m.tmdb_id, m.media_type)
        except Exception as e:  # a lookup miss just means we can't dedup this one — never fatal
            logger.debug("tvdb lookup for {!r} failed: {}", m.title, e)
    return m.tvdb_id


def _gate_by_tmdb(cfg: RequestConfig, pool: list[MissingTitle]) -> list[MissingTitle]:
    """Keep titles clearing the TMDB rating/vote floors, ranked by demand then score."""
    qualifying = [m for m in pool if m.rating >= cfg.min_rating and m.vote_count >= cfg.min_votes]
    qualifying.sort(key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)
    return qualifying


def _live_lookups(mdblist: object) -> int | None:
    """Billable calls this rating client has made so far, or None if it cannot report its own spend.

    None falls the caller back to billing every inspection — the behaviour before 2026-08-18, which
    is merely over-cautious rather than wrong. Only a client that doesn't track its spend takes it.
    """
    value = getattr(mdblist, "live_lookups", None)
    return value if isinstance(value, int) else None


def _gate_by_source(
    cfg: RequestConfig,
    mdblist: MdbListClient,
    pool: list[MissingTitle],
    report: RequestReport,
    *,
    budget: int,
    walk_limit: int,
) -> list[MissingTitle]:
    """Keep titles clearing the chosen MDBList source's rating/vote floors, ranked by demand then score.

    Walks the demand-ranked pool and rates titles until ``_lookup_budget`` LIVE lookups have been
    spent, so a big missing pool stays well under MDBList's daily cap. A lookup that fails or has no
    score for this source just drops that title. If the daily quota runs out mid-gate, we stop, flag
    the run, and fall back to TMDB for the WHOLE pool so the run still completes (the owner is alerted
    from the flag).

    The budget counts API CALLS, not titles inspected, and the difference is the whole point. Ratings
    cache for a week (``RATING_CACHE_TTL_S``) and the demand ranking barely moves between nights, so
    the head of this list is mostly titles already rated on an earlier run. Charging those cache hits
    against the budget — which is what taking a fixed ``[:budget]`` slice did — spent the allowance
    re-reading the same rejects every night and never reached the titles below them. In production
    that stuck at ``0 qualifying`` for five days across 10,488 wanted titles, and the ranking makes it
    permanent rather than unlucky: the list is ordered by how many people want a title while the gate
    asks how well-rated it is, and on a big server the most-wanted MISSING titles are exactly the ones
    nobody thought worth adding. Reading past a stale head costs nothing, so it is no longer paid for.

    ``budget`` is this row's share of the run's lookups; ``walk_limit`` is derived from the whole
    run's, and the two are deliberately not the same number. The cache the walk has to get past is
    global to the run, so a row scaled to a fraction of it stops INSIDE that head, spends nothing and
    hands its share to the next row — starving whichever rows the owner happens to have put first,
    while the run reports every slot filled. Splitting the quota is right; splitting the free walk is
    not (release review 2026-08-18).
    """
    source = cfg.rating_source
    enforce_votes = source in VOTE_SOURCES  # RT/Metacritic are critic scores — no audience-vote floor
    ranked = sorted(pool, key=lambda m: (m.demand, m.rating, m.vote_count), reverse=True)
    start = _live_lookups(mdblist)
    scored: list[tuple[MissingTitle, float, int]] = []
    examined = 0

    def spent() -> int:
        now = _live_lookups(mdblist)
        return examined if start is None or now is None else now - start

    for title in ranked:
        if spent() >= budget or examined >= walk_limit:
            break
        examined += 1
        try:
            score = mdblist.rating(title.tmdb_id, title.media_type, source)
        except MdbListRateLimitError:
            logger.warning("MDBList daily limit reached — falling back to TMDB ratings for this run")
            report.ratings_rate_limited = True
            report.examined += examined
            report.lookups_spent += spent()
            return _gate_by_tmdb(cfg, pool)
        except Exception as e:  # a single lookup hiccup drops that title, never the whole gate
            logger.warning("{} rating lookup for {!r} failed: {}", source, title.title, e)
            continue
        if score is None:
            continue
        rating, votes = score
        if rating >= cfg.min_rating and (not enforce_votes or votes >= cfg.min_votes):
            # Carry the chosen-source score forward so the auto-send bar and the queued rows the owner
            # reviews both reflect it, not the TMDB value the title arrived with.
            title.rating = rating
            title.vote_count = votes
            scored.append((title, rating, votes))
        elif rating < cfg.min_rating - _NEAR_MISS:
            # Nowhere near the bar. Hold the score so next week's run reads it for free instead of
            # spending one of its lookups re-learning the same answer (see _REJECT_RECHECK_TTL_S).
            mdblist.defer_recheck(title.tmdb_id, title.media_type, _REJECT_RECHECK_TTL_S)
    report.examined += examined
    report.lookups_spent += spent()
    scored.sort(key=lambda row: (row[0].demand, row[1], row[2]), reverse=True)
    return [title for title, _, _ in scored]


def _request_one_seerr(title: MissingTitle, seerr: SeerrClient | None, *, dry_run: bool) -> RequestOutcome:
    """``_request_one`` for an Overseerr/Jellyseerr target.

    Much shorter than its Arr twin because the *seerr takes movies and shows through one endpoint,
    both keyed by TMDB id — so there is no app to choose, no TVDB id to resolve, and therefore no
    ``skipped_no_tvdb`` outcome to explain.

    ``seerr`` is None when the route is chosen but the instance is not connected — the same shape as
    ``_request_one``'s missing Arr, and it earns the same kind of skip rather than being explained in
    the other route's words.
    """
    if seerr is None:
        return RequestOutcome(
            tmdb_id=title.tmdb_id,
            title=title.title,
            media_type=title.media_type,
            status="skipped_no_target",
            detail="Overseerr is the chosen request target but has no address or API key",
            arr_slug=None,
        )
    try:
        status, detail, slug = seerr.request_title(title.tmdb_id, title.media_type, dry_run=dry_run)
    except SeerrError as e:
        # A request failing is a footnote, never a run failure — the *seerr is optional plumbing.
        logger.warning("request for {!r} failed: {}", title.title, e)
        status, detail, slug = "error", str(e), None
    return RequestOutcome(
        tmdb_id=title.tmdb_id,
        title=title.title,
        media_type=title.media_type,
        status=status,
        detail=detail,
        arr_slug=slug,
    )


def _request_one(
    title: MissingTitle,
    radarr: RadarrClient | None,
    sonarr: SonarrClient | None,
    tmdb: TmdbClient,
    *,
    dry_run: bool,
    sonarr_monitor: str = "all",
) -> RequestOutcome:
    """Route one missing title to the right app; translate any failure into an outcome, never a raise."""

    def outcome(status: str, detail: str, slug: str | None = None) -> RequestOutcome:
        return RequestOutcome(
            tmdb_id=title.tmdb_id,
            title=title.title,
            media_type=title.media_type,
            status=status,
            detail=detail,
            arr_slug=slug,
        )

    try:
        if title.media_type is MediaType.MOVIE:
            if radarr is None:
                return outcome(
                    "skipped_no_target", "Radarr not fully configured (check quality profile and root folder)"
                )
            status, detail, slug = radarr.add_movie(title.tmdb_id, dry_run=dry_run, extra_tags=title.tags)
            return outcome(status, detail, slug)
        # Shows: Sonarr keys on TVDB, so cross the namespace first. The TVDB lookup is a TMDB call,
        # not an Arr one, so it raises RuntimeError/httpx errors rather than ArrError — catch it here
        # so one show's lookup hiccup becomes that title's outcome, never an escape that discards the
        # whole pass's recorded outcomes (the run-level handler would otherwise lose the audit trail).
        if sonarr is None:
            return outcome("skipped_no_target", "Sonarr not fully configured (check quality profile and root folder)")
        # Reuse the TVDB id if the arr-state check already resolved it this run; else look it up now.
        tvdb_id = title.tvdb_id
        if tvdb_id is None:
            try:
                tvdb_id = tmdb.tvdb_id(title.tmdb_id, title.media_type)
            except Exception as e:
                logger.warning("TVDB lookup for {!r} failed: {}", title.title, e)
                # Distinct from the skip below on purpose: THIS one is a lookup that failed (TMDB
                # down, a timeout), so retrying may well work. The skip is a settled fact about the
                # data and never will.
                return outcome("error", "couldn't reach TMDB to look up this show's TheTVDB id — it may work next run")
        if tvdb_id is None:
            # Says what to DO, because nothing here can. Sonarr identifies shows by TheTVDB id and
            # TMDB is where we look it up; when TMDB has not recorded one there is no way to name the
            # show to Sonarr, and guessing is worse than skipping — the nearest title match for
            # "The Haunting of Bly Manor" is "The Haunting", a different and much larger series.
            #
            # The old text was "no TheTVDB id for this show": true, and useless. It reads as a fault
            # report to anyone who does not already know what a TVDB id is, so the reader cannot tell
            # whether Shortlist is broken, their Sonarr is misconfigured, or this is simply how it is.
            # None of those, and there is exactly one remedy.
            return outcome(
                "skipped_no_tvdb",
                "TMDB has no TheTVDB id for this show, and Sonarr needs one — add it in Sonarr yourself",
            )
        status, detail, slug = sonarr.add_series(
            tvdb_id, dry_run=dry_run, extra_tags=title.tags, monitor=sonarr_monitor
        )
        return outcome(status, detail, slug)
    except ArrError as e:
        # A request failing is a footnote, never a run failure — Sonarr/Radarr are optional plumbing.
        logger.warning("request for {!r} failed: {}", title.title, e)
        return outcome("error", str(e))
