"""Row construction: turn one user's (or the audience's) history into ranked, delivered picks.

Everything here is the "what goes in the row" half of the engine. The ordering that keeps a row
private — deliver unpromoted, merge filters, promote last — lives in ``pipeline.py``; this module
only builds and delivers collections, always UNPROMOTED.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

import shortlist.engine.pipeline as _pipeline
from shortlist.engine import candidates as candidates_mod
from shortlist.engine import ranking
from shortlist.engine import requests as requests_mod
from shortlist.engine.curator import CuratorError, NullCurator
from shortlist.engine.delivery import (
    _section_kind,
    _target_sections,
    deliver_rows,
    remove_row,
    render_row_name,
    resolve_row_template,
    row_marker,
    sections_for_keys,
)
from shortlist.engine.history import derive_seeds
from shortlist.engine.models import (
    SHARED_SLUG_PREFIX,
    Candidate,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    PromptConfig,
    RowSpec,
    UserProfile,
    UserRunReport,
    UserType,
    WatchedItem,
    overlay_prompt,
)

if TYPE_CHECKING:
    from shortlist.engine.pipeline import EngineContext


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


def _watched_titles(
    watched_movies: set[int],
    show_plays: dict[int, int],
    episode_counts: dict[int, int],
    show_pct: float,
) -> set[tuple[int, MediaType]]:
    """The (tmdb_id, media_type) titles this person has FINISHED — the ones a watched-cap counts.

    Every watched movie, plus every show seen to >= ``show_pct`` of its episodes. A partly-watched
    show, or one with a new season (its episode count grew, so the fraction dropped), is NOT counted
    — it can still be recommended. A show whose episode count is unknown is counted as finished
    rather than risk surfacing one the person has already worked through.
    """
    finished: set[tuple[int, MediaType]] = {(tid, MediaType.MOVIE) for tid in watched_movies}
    for tid, plays in show_plays.items():
        total = episode_counts.get(tid)
        if not total or plays >= total * show_pct:
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


def _rotate_for_freshness(ranked: list, k: int, freshness: float, day: int) -> list:
    """Rotate a fraction of a row's ranked candidates by a per-day phase, keeping the strongest put.

    Freshness ``f`` rotates ``round(f * k)`` of the k slots: the top ``k - swap`` picks stay (quality
    preserved), and the remaining slots rotate daily through the candidates ranked just below them —
    so the row shifts day to day without dropping its best titles or reaching for weak ones until
    ``f`` is high. ``day`` is the phase (``date.toordinal()``): the same day yields the same order, a
    later day a shifted one. ``f == 0``, a zero ``day``, or a pool no deeper than ``k`` is a no-op.
    """
    if freshness <= 0 or day <= 0 or len(ranked) <= k:
        return ranked
    swap = round(freshness * k)
    if swap <= 0:
        return ranked
    stable = k - swap
    tail = ranked[stable:]  # everything eligible to rotate through the swap slots
    offset = (day * swap) % len(tail)
    return ranked[:stable] + tail[offset:] + tail[:offset]


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


def _row_catalog(ctx: EngineContext, spec: RowSpec) -> dict[MediaType, list[dict]]:
    """The AI-from-library catalog THIS row may propose from — its own libraries only."""
    if not spec.library_keys or not ctx.section_catalog:
        return ctx.library_catalog
    catalog: dict[MediaType, list[dict]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
    seen: dict[MediaType, set[int]] = {MediaType.MOVIE: set(), MediaType.SHOW: set()}
    for section in sections_for_keys(ctx.delivery_sections, spec.library_keys):
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        for item in ctx.section_catalog.get(section.key, []):
            # A row pinned to both "Movies" and "4K Movies" must not show the LLM the same film
            # twice — that spends its slice of the catalog on duplicates.
            if item["tmdb_id"] not in seen[kind]:
                seen[kind].add(item["tmdb_id"])
                catalog[kind].append(item)
    return catalog


def _candidate_pool(
    ctx: EngineContext,
    seeds: list,
    library_index: dict[MediaType, dict[int, int]],
    *,
    excluded_genres: set[str],
    recent: set[tuple[int, MediaType]],
    profile=None,
    sources: list[str] | None = None,
    media: str = "both",
    catalog: dict[MediaType, list[dict]] | None = None,
    watched_exclusions: set[tuple[int, MediaType]] | None = None,
) -> tuple[list[Candidate], list[Candidate], list[Candidate], list[Candidate]]:
    """Gather TMDB candidates for ``seeds``, intersect with the library, split by staleness.

    Returns ``(pool, in_library, ranked, held_back)``:

    * ``pool`` — every pooled candidate (used for request-demand bookkeeping before narrowing).
    * ``in_library`` — the ones the delivery libraries actually hold and this user may still see.
    * ``ranked`` — the pre-ranked fresh candidates the curator chooses from.
    * ``held_back`` — pre-ranked titles the staleness guard held back (recommended in the last N
      runs). Still valid recommendations, so they backfill a row fresh candidates can't fill —
      without them a thin pool SHRINKS the row rather than repeating a title.

    ``media`` narrows the pool BEFORE the pre-rank truncation. Filtering after it meant a
    movie-heavy watcher's shows-only row could lose every show to the 40-candidate cut and deliver
    nothing — a dead row on a green run.

    One ``filter_candidates`` pass, not two: the valid set is partitioned by ``recent``. Identity
    is (tmdb_id, media_type), never the bare id — movie 1399 and TV 1399 are different titles.
    """
    # The titles this person has already watched (per the row's policy), not just the ~30 seeds — a
    # recommendation you've finished is the exact thing the row shouldn't surface. Falls back to the
    # seed set for callers that don't compute the full breakdown (e.g. shared rows).
    watched_ids = watched_exclusions if watched_exclusions is not None else {(s.tmdb_id, s.media_type) for s in seeds}
    pool = candidates_mod.gather_candidates(
        ctx.tmdb,
        seeds,
        sources=sources if sources is not None else ctx.config.candidate_sources,
        curator=ctx.curator,
        catalog=ctx.library_catalog if catalog is None else catalog,
        profile=profile,
        trakt=ctx.trakt,
        search=ctx.search,
        web_search_mode=ctx.config.web_search_provider,
    )
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=excluded_genres,
        recent_pick_ids=set(),
    )
    valid = _media_filter(valid, media)
    in_library = [c for c in valid if (c.tmdb_id, c.media_type) not in recent]
    held = [c for c in valid if (c.tmdb_id, c.media_type) in recent]
    # Pre-rank EACH media type to its own cap, not the mixed pool to one cap — otherwise a 'both'
    # row whose pool skews one way (a mostly-TV watcher) truncates the other type away before the
    # per-media curate ever sees it, and that library's collection comes up empty.
    kinds = [MediaType.MOVIE, MediaType.SHOW] if media == "both" else [MediaType(media)]
    cap = ctx.config.candidates_pre_rank
    ranked = [c for kind in kinds for c in ranking.pre_rank([x for x in in_library if x.media_type is kind], cap)]
    held_back = [c for kind in kinds for c in ranking.pre_rank([x for x in held if x.media_type is kind], cap)]
    return pool, in_library, ranked, held_back


def _in_audience(user: UserProfile, spec: RowSpec) -> bool:
    return spec.audience is None or user.plex_account_id in spec.audience


def _is_muted(user: UserProfile, spec: RowSpec) -> bool:
    override = user.row_overrides.get(spec.slug)
    return bool(override and override.muted)


def _remove_muted_and_retired(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, diff: CollectionDiff) -> None:
    """Remove this user's rows that were muted or disabled since the last run.

    A row muted or switched off in the UI is gone from ``cfg.rows``, but its collection still sits on
    this person's Home (excluded from everyone else, so private — just not gone). Removing it makes
    "muted"/"disabled" mean *gone*. This runs before the "no active rows -> return" check so a user
    whose every row was switched off is still cleaned up, and only ever makes the server MORE private,
    so it happens regardless of whether the user has any row this time.
    """
    muted = [s for s in cfg.per_person_rows() if _in_audience(user, s) and _is_muted(user, s)]
    retired = [s for s in cfg.retired_rows if not s.shared and _in_audience(user, s)]
    for spec in (*muted, *retired):
        # write_lock: a Plex mutation (and the collections-cache read/invalidate inside it) must be
        # serialized when users run concurrently — only reads + LLM overlap (Stage 3).
        with ctx.write_lock:
            remove_row(ctx.plex, user, cfg, spec, dry_run=cfg.dry_run, diff=diff, sections=ctx.delivery_sections)


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
    demand: requests_mod.DemandMap | None = None,
) -> bool:
    """Deliver every per-person row this user is in the audience of. Candidates are computed once
    and reused across rows; each row curates and delivers with its own size/media/recipe. Returns
    True when at least one row was delivered (a candidate for promotion).

    When ``demand`` is provided (requests are on), the candidates this user wanted but no delivery
    library holds are folded into it, so the run-wide request pass can ask Sonarr/Radarr for them.
    """
    cfg = ctx.config

    user_report.diff = CollectionDiff()
    _remove_muted_and_retired(ctx, user, cfg, user_report.diff)

    specs = [spec for spec in cfg.per_person_rows() if _in_audience(user, spec) and not _is_muted(user, spec)]
    if not specs:
        return False  # this user is in no per-person row (none in audience, or all muted)
    # The adapter puts the Phase-A global+per-user recipe on the profile; a row with its own recipe
    # overrides it for that row only.
    base_prompt = user.prompt

    _pipeline._emit(ctx, user.slug, "history", {})
    user.history = ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    cold = len(user.history) < cfg.min_history
    base_cold: list[Pick] = []
    # A candidate pool per DISTINCT effective source-set among this user's rows. Rows that share
    # sources (the common case — every row inheriting the global set) reuse one pool; a row that
    # picks its own sources gets its own. Keyed by the sorted source tuple, memoised across the user.
    Pool = tuple[list[Candidate], list[Candidate], list[Candidate], list[Candidate]]
    pool_cache: dict[tuple, Pool] = {}
    pool_failures: dict[tuple, str] = {}  # pool key -> why every source for it failed
    seeds: list = []
    recent: set[tuple[int, MediaType]] = set()
    # This person's watched breakdown, filled in the non-cold branch and read by pools_for: watched
    # movie tmdb_ids, and show tmdb_id -> episode-play count (for the finished-show fraction). The
    # derived set of FINISHED (tmdb_id, media_type) titles is computed once the breakdown is in.
    watched_movies: set[int] = set()
    show_plays: dict[int, int] = {}
    watched_titles: set[tuple[int, MediaType]] = set()

    def effective_watched_pct(spec: RowSpec) -> float:
        return spec.watched_pct if spec.watched_pct is not None else cfg.watched_pct

    def effective_freshness(spec: RowSpec) -> float:
        return spec.freshness if spec.freshness is not None else cfg.freshness

    def effective_sources(spec: RowSpec) -> tuple[str, ...]:
        # Sorted so two rows with the same sources in a different order share ONE pool (gather is
        # set-based) — otherwise they'd each rebuild it, re-hitting rate-limited/LLM sources and, for
        # the non-deterministic llm_* sources, possibly diverging despite identical configuration.
        return tuple(sorted(spec.candidate_sources or cfg.candidate_sources))

    def pool_key(spec: RowSpec) -> tuple:
        # Sources alone is not enough. A row's media and its libraries both change which candidates
        # survive — and both now narrow the pool BEFORE the pre-rank truncation, so two rows that
        # differ in either must not share a pool. Rows that differ in none of the three (the common
        # case: everything inheriting the defaults) still share exactly one.
        return (
            effective_sources(spec),
            spec.media,
            tuple(sorted(str(k) for k in spec.library_keys)),
            # Only whether the pool hard-excludes finished titles changes the CANDIDATES: a 0% row
            # drops them from the pool; any >0 row keeps them and caps at delivery. Two >0 rows (20%
            # and 50%) share one pool and differ only in their cap, so they must not key apart.
            effective_watched_pct(spec) == 0,
        )

    def pools_for(spec: RowSpec) -> Pool | None:
        """This row's pool, or None when every source it uses is down.

        Per ROW, not per user: a row pinned to a single source (a Trakt-only row while Trakt 502s)
        must not take the person's other rows down with it — those rows have working sources and a
        row they can still fill.
        """
        key = pool_key(spec)
        if key in pool_failures:
            return None
        if key not in pool_cache:
            try:
                pool_cache[key] = _candidate_pool(
                    ctx,
                    seeds,
                    row_library_index(ctx, spec, library_index),
                    excluded_genres=user.excluded_genres,
                    recent=recent,
                    profile=user,
                    sources=list(key[0]),
                    media=spec.media,
                    catalog=_row_catalog(ctx, spec),
                    # A 0% row drops finished titles from the pool entirely; a >0 row keeps them (the
                    # per-library cap trims the surplus at delivery). None -> exclude only the seeds.
                    watched_exclusions=watched_titles if effective_watched_pct(spec) == 0 else None,
                )
            except Exception as e:
                pool_failures[key] = f"{type(e).__name__}: {e}"
                logger.warning("{}: row '{}' has no working candidate source ({})", user.username, spec.slug, e)
                return None
        return pool_cache[key]

    if cold:
        # Enough picks for the LARGEST row this user is in; each row then takes its own k.
        base_cold = _cold_start_picks(ctx, user, cfg, k=max(spec.size for spec in specs))
        user_report.status = "cold_start"
    else:
        resolve = _rating_key_resolver(seed_index)
        seeds = derive_seeds(user.history, resolve, max_seeds=cfg.max_seeds)
        user_report.counts.seeds = len(seeds)
        # Full watched breakdown (not just the seeds): every watched movie, and each show's
        # episode-play count. History is already completion-filtered, so this is meaningful watches.
        for item in user.history:
            tid = item.tmdb_id if item.tmdb_id is not None else resolve(item)
            if tid is None:
                continue
            if item.media_type is MediaType.MOVIE:
                watched_movies.add(tid)
            else:
                show_plays[tid] = show_plays.get(tid, 0) + 1
        # The finished-title set, derived once: read by pools_for (0% hard-exclude) and the per-row
        # watched cap (>0). Mutated in place so the pools_for closure sees it.
        watched_titles |= _watched_titles(watched_movies, show_plays, ctx.episode_counts, cfg.watched_show_pct)
        _pipeline._emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": len(seeds)})
        recent = ctx.recent_picks.get(user.slug, set())
        for spec in specs:  # build every row's pool up front so counts and demand see them all
            pools_for(spec)
        # Only if EVERY row's sources are down do we know nothing about this person: that's a failed
        # user, not a quiet "ok" that leaves yesterday's rows in place. One dead row among several
        # is just that one row.
        if pool_failures and not pool_cache:
            raise RuntimeError("; ".join(sorted(pool_failures.values())))
        # Counts are the distinct union across pools (a title in two rows' pools is one candidate).
        user_report.counts.candidates = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[0]})
        user_report.counts.in_library = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[1]})
        user_report.counts.pre_ranked = len({(c.tmdb_id, c.media_type) for p in pool_cache.values() for c in p[2]})
        # Record what this user wanted that the server doesn't have, for the run-wide request pass.
        # A missing title is attributed to exactly the rows whose pool surfaced it: it gets the user's
        # own request tag plus the tag of each such row. Deduped per user so demand counts them once.
        if demand is not None:
            user_tag = {user.request_tag} if user.request_tag else set()
            first_seen: dict[tuple[int, MediaType], Candidate] = {}
            title_tags: dict[tuple[int, MediaType], set[str]] = {}
            for spec in specs:
                pools = pools_for(spec)
                if pools is None:
                    continue
                for c in requests_mod.collect_missing(pools[0], library_index):
                    key = (c.tmdb_id, c.media_type)
                    first_seen.setdefault(key, c)
                    tags = title_tags.setdefault(key, set())
                    tags |= user_tag  # the user wanted it, whatever the row's media
                    # ...but a row's tag only applies to titles that row could actually show, so a
                    # shows-only row never tags a missing movie (its pool holds both until delivery).
                    if spec.request_tag and spec.media in ("both", c.media_type.value):
                        tags.add(spec.request_tag)
            # `demand` is the run-wide shared map; the per-user tally above is local, so only this
            # merge needs the lock (Stage 3 parallel runs).
            with ctx.write_lock:
                for key, cand in first_seen.items():
                    requests_mod.accumulate(demand, [cand], tags=title_tags[key])
        user_report.status = "ok"

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
        # A row runs PER LIBRARY, not per media type: each library it targets gets its own full
        # collection of k, curated from that library's own contents. So a server with two movie
        # libraries (Movies + 4K) gets a full row in EACH, and a mostly-TV watcher still gets a full
        # movie row and a full show row (the "one movie in Picked for You" bug, SFLIX 2026-07-15).
        targets = _target_sections(ctx.delivery_sections, spec)
        if not cold:
            # The row's recipe (already the global one with the row's fields laid over it), then this
            # person's override laid over THAT. Setting only a tone for one person used to wipe the
            # row's guidance and custom prompt.
            row_prompt = spec.prompt if spec.prompt is not None else base_prompt
            effective_prompt = overlay_prompt(row_prompt, override.prompt if override else None)
            # A per-row copy carries the effective recipe to the curator; the real profile is never
            # mutated, so one row's recipe can't leak into the next row (or into delivery below).
            row_profile = _with_prompt(user, effective_prompt)
            # This row's own pool: its sources, its media and its libraries — already narrowed to
            # all three BEFORE the pre-rank truncation, so nothing this row could show was cut by
            # candidates it could never show.
            pools = pools_for(spec)
            if pools is None:
                continue  # every source this row uses is down; its siblings still deliver
            _pool, _in_library, pool_for_row, held_back = pools
            _pipeline._emit(ctx, user.slug, "curating", {"candidates": len(pool_for_row)})
        section_picks: dict[str, list[Pick]] = {}
        for section in targets:
            kind = _section_kind(section)
            # tmdb_id -> ratingKey for THIS library only; a candidate not in this library isn't a
            # valid pick for it, however well it ranks for the row overall.
            if cold:
                # Cold picks already come FROM a library (plex.top_rated), so they're in-library by
                # construction; delivery remaps each to the target library and drops any it lacks.
                cands = _rotate_for_freshness(
                    [p for p in base_cold if p.media_type is kind], k, effective_freshness(spec), ctx.run_day
                )[:k]
                section_picks[section.key] = [replace(p, rank=i + 1) for i, p in enumerate(cands)]
                continue
            sec_idx = ctx.section_index.get(section.key, {})
            sub = [c for c in pool_for_row if c.media_type is kind and c.tmdb_id in sec_idx]
            if not sub:
                continue
            # Freshness rotates which strong candidates lead today, so the row shifts day to day
            # without dropping its best titles (at 0 this is a no-op — the ranking is untouched).
            sub = _rotate_for_freshness(sub, k, effective_freshness(spec), ctx.run_day)
            try:
                sec_picks = ctx.curator.curate(row_profile, sub, k)
                user_report.llm_tokens += ctx.curator.last_tokens
            except CuratorError as e:
                logger.warning("{}: curator failed ({}); degrading to heuristic mode", user.username, e)
                sec_picks = NullCurator().curate(row_profile, sub, k)
            held = [c for c in held_back if c.media_type is kind and c.tmdb_id in sec_idx]
            if len(sec_picks) < k:
                sec_picks = _pad_picks(sec_picks, sub + held, k)
            pct = effective_watched_pct(spec)
            if pct > 0:
                # Let at most `pct` of this library's row be already-finished titles; backfill the
                # rest from its fresh candidates. (At pct == 0 the pool already dropped finished ones.)
                sec_picks = _apply_watched_cap(sec_picks, sub + held, watched_titles, k, pct)
            section_picks[section.key] = sec_picks
        # Stamp each pick with the row it belongs to, so the user page can group picks per row.
        section_picks = {key: [replace(p, collection_slug=spec.slug) for p in sp] for key, sp in section_picks.items()}
        # Record the exact title delivery will write for EACH library, so the promote phase can apply
        # this row's placement/pin. Per library, because a {top_seed} title differs library to library
        # (each was curated from its own contents). Matches delivery's `render_row_name(...) + marker`.
        title_template = resolve_row_template(spec, user, cfg)
        marker = row_marker(user.plex_account_id)
        for sp in section_picks.values():
            if sp:
                user_report.placement_titles[render_row_name(title_template, user, sp) + marker] = spec.slug
        picks = [pick for sp in section_picks.values() for pick in sp]
        all_picks.extend(picks)
        _pipeline._emit(ctx, user.slug, "delivering", {"picks": len(picks)})
        # write_lock: the Plex collection writes AND the shared stored_labels mutation inside
        # deliver_rows must be serial across users — the leak-safe half of Stage 3 parallelism.
        with ctx.write_lock:
            deliver_rows(
                ctx.plex,
                user,
                picks,
                cfg,
                spec,
                sole_row=len(specs) == 1,
                dry_run=cfg.dry_run,
                stored_labels=stored_labels,
                diff=user_report.diff,
                sections=ctx.delivery_sections,
                section_index=ctx.section_index,
                section_picks=section_picks,
                breakdown=user_report.breakdown,
            )
        delivered_any = delivered_any or bool(picks)

    user_report.picks = all_picks
    user_report.counts.picks = len(all_picks)
    if not all_picks:
        logger.warning("{}: no picks produced — existing rows are left as they are", user.username)
    return delivered_any  # nothing delivered -> nothing to promote


def _with_prompt(user: UserProfile, prompt: PromptConfig | None) -> UserProfile:
    """A shallow copy of the profile carrying ``prompt`` — used to curate one row without mutating
    the shared profile (its history/genres/overrides are read-only during curation)."""
    return replace(user, prompt=prompt)


def _run_shared(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[int, int],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report,
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
        agg = _shared_row(ctx, spec, users, seed_index, library_index, stored_labels, user_report, slug)
    except Exception as e:  # one shared row's failure never stops the next (rule 6 resume-safety)
        user_report.status = "error"
        user_report.error = f"{type(e).__name__}: {e}"
        logger.exception("shared row '{}': failed", spec.slug)
        agg = None
    finally:
        user_report.duration_s = round(time.monotonic() - started, 2)
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
        prompt=spec.prompt,
    )
    if not agg_history:
        user_report.status = "skipped"
        logger.info("shared row '{}': no title watched by >= {} people yet", spec.slug, threshold)
        return None

    seeds = derive_seeds(agg_history, resolve, max_seeds=cfg.max_seeds)
    row_sources = spec.candidate_sources if spec.candidate_sources else None  # None -> global default
    # Same three narrowings a per-person row gets: its sources, its media, its libraries.
    _pool, _in_library, ranked, held_back = _candidate_pool(
        ctx,
        seeds,
        row_library_index(ctx, spec, library_index),
        excluded_genres=set(),
        recent=ctx.recent_picks.get(slug, set()),
        profile=agg,
        sources=row_sources,
        media=spec.media,
        catalog=_row_catalog(ctx, spec),
    )
    k = spec.size
    # Curate PER LIBRARY, exactly like a per-person row: each targeted library gets its own full k
    # from its own contents. One mixed curate over a now media-segregated pool would let a 'both'
    # shared row come back all-movies-no-shows.
    targets = _target_sections(ctx.delivery_sections, spec)
    section_picks: dict[str, list[Pick]] = {}
    freshness = spec.freshness if spec.freshness is not None else cfg.freshness
    for section in targets:
        kind = _section_kind(section)
        sec_idx = ctx.section_index.get(section.key, {})
        sub = [c for c in ranked if c.media_type is kind and c.tmdb_id in sec_idx]
        if not sub:
            continue
        sub = _rotate_for_freshness(sub, k, freshness, ctx.run_day)  # rotate a public row day to day too
        try:
            sec_picks = ctx.curator.curate(agg, sub, k)
            # Shared-row LLM spend used to vanish — only the per-person path accounted tokens.
            user_report.llm_tokens += ctx.curator.last_tokens
        except CuratorError:
            sec_picks = NullCurator().curate(agg, sub, k)
        if len(sec_picks) < k:
            # Backfill from held-back titles too — a shared row used to SHRINK on a thin pool while a
            # per-person row backfilled.
            held = [c for c in held_back if c.media_type is kind and c.tmdb_id in sec_idx]
            sec_picks = _pad_picks(sec_picks, sub + held, k)
        section_picks[section.key] = sec_picks
    # Force aggregate framing regardless of curator: a shared row is nobody's "because you watched",
    # and the seed is dropped so a {top_seed} name template can never surface one person's title.
    section_picks = {
        key: [
            replace(p, reason="Popular on this server", seed_title=None, seed_tmdb_id=None, collection_slug=spec.slug)
            for p in sp
        ]
        for key, sp in section_picks.items()
    }
    picks = [pick for sp in section_picks.values() for pick in sp]

    user_report.picks = picks
    user_report.counts.picks = len(picks)
    user_report.status = "ok"
    user_report.diff = CollectionDiff()
    _pipeline._emit(ctx, slug, "delivering", {"picks": len(picks)})
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
    )
    return agg if picks else None


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up short curator output from the heuristic order (never invents titles)."""
    have = {(p.tmdb_id, p.media_type) for p in picks}  # movie 1399 and TV 1399 are different titles
    fillers = NullCurator().curate(
        UserProfile(username="", plex_account_id=0, user_type=UserType.SHARED),
        [c for c in ranked if (c.tmdb_id, c.media_type) not in have],
        k - len(picks),
    )
    out = list(picks)
    for f in fillers:
        out.append(Pick(**{**f.__dict__, "rank": len(out) + 1}))
    return out


def _cold_start_picks(ctx: EngineContext, user: UserProfile, cfg: EngineConfig, k: int = 0) -> list[Pick]:
    """ "Popular on <server>" fallback for a user with thin history: top-rated titles.

    Every library gets a share, not just movies — a movies-only cold start would hand delivery a
    pick list with no shows in it, and a thin-history night (a Tautulli outage is enough) would
    then leave a TV watcher with a row of films they never asked for.
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
                )
            )
    return picks
