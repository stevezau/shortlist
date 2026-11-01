"""Measure what a negative `recency` (the "older" lean, #174) does to the rows, against real watches.

It hides each person's most recent DISCOVERIES one at a time (a movie watched once, a show whose first play was
within a day of the watch), rebuilds the pool from only what they had watched before, and ranks it under each
`recency` in `RECENCY_ARMS` ("server" = the server's own setting). The report gives, per arm, how often the
held-out title is in the row, the mean reciprocal rank, and how old the delivered rows run (median release year,
share before 2000 and 1990, share undated), then the same for the candidate pool and for the held-out titles
themselves, so "the row got older" can be told from "the pool, or what people actually watch, is old".

    SHORTLIST_CONFIG=/path/to/COPY/of/config OLDER_T=25 .venv/bin/python scripts/recency_eval.py

`OLDER_T` is `ranking.OLDER_BOOST_YEARS` for the invocation (one value per run; run it again for another).

FREE: TMDB only. `llm_web` is removed from the sources and there is no curator, web search or search cache, so
nothing here can spend AI tokens or search credits. Still run it only against a COPY of the config directory,
never the live one. It touches no collection, label or share filter. Maintenance tooling, like
`replay_eval.py` - not shipped in the Docker image, not run by CI.

The in-library unwatched year distribution is not reported: `PlexClient` only builds a tmdb-id -> rating-key
index of a section, with no cheap way to list items with their years.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import chain
from pathlib import Path

import replay_eval

from shortlist.engine import ranking
from shortlist.engine.eval.replay import (
    Comparison,
    HoldoutCase,
    ReplayOutcome,
    first_watch_keep,
    holdout_cases,
    replay_case,
    summarise,
    year_stats,
)
from shortlist.engine.models import EngineConfig, MediaType, UserProfile
from shortlist.server.db.models import User
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import WatchCache

# ── the experiment ────────────────────────────────────────────────────────────────────────────────
CASES_PER_USER = 5
MAX_CASES: int | None = None  # stop after this many cases in total; None = every case
RECENCY_ARMS: list[float | None] = [None, 0.0, -0.25, -0.5, -1.0]  # None = the server's own recency
OLDER_T = float(os.environ.get("OLDER_T", "25"))  # ranking.OLDER_BOOST_YEARS for this invocation
WORKERS = 4  # people replayed in parallel
# ──────────────────────────────────────────────────────────────────────────────────────────────────

SERVER = "server"


def arm_label(value: float | None) -> str:
    return SERVER if value is None else f"{value:g}"


def make_arms(config: EngineConfig) -> dict[str, EngineConfig]:
    """One config per recency, with AI web search taken out so the replay cannot spend anything."""
    free = replace(config, candidate_sources=[s for s in config.candidate_sources if s != "llm_web"])
    assert "llm_web" not in free.candidate_sources
    return {arm_label(v): free if v is None else replace(free, recency=v) for v in RECENCY_ARMS}


def replay_user(
    cases: list[HoldoutCase], arms: dict[str, EngineConfig], shared: dict
) -> list[dict[str, ReplayOutcome]]:
    """Every case under every arm. No curator, search or cache: the replay is TMDB-only."""
    return [
        {
            label: replay_case(
                case,
                config,
                config_label=label,
                curator=None,
                search=None,
                web_search_cache=None,
                run_year=case.held_out.watched_at.year,
                **shared,
            )
            for label, config in arms.items()
        }
        for case in cases
    ]


def fmt_stats(years) -> str:
    stats = year_stats(years)
    median = f"{stats.median:.0f}" if stats.median is not None else "-"
    return (
        f"n={stats.count:<6} median {median:<6} <2000 {stats.pct_before_2000:>4.0%}  "
        f"<1990 {stats.pct_before_1990:>4.0%}  undated {stats.pct_undated:>4.0%}"
    )


def report(results: list[dict[str, ReplayOutcome]], arms: dict[str, EngineConfig], server_recency: float) -> None:
    labels = list(arms)
    print(f"\nServer recency = {server_recency:g}; OLDER_T = {OLDER_T:g}; {len(results)} cases\n")

    print("Per arm: held-out title in the row, mean reciprocal rank, and the delivered rows' release years")
    for label in labels:
        got = [o[label] for o in results]
        in_row = sum(o.in_final_row for o in got)
        mrr = sum(o.reciprocal_rank for o in got) / len(got)
        row_years = [t.year for o in got for t in o.final_row]
        print(f"  {label:<8}{f'{in_row}/{len(got)}':>8}  MRR {mrr:.3f}  rows: {fmt_stats(row_years)}")

    print("\nContext (the same for every arm)")
    pool_years = list(chain.from_iterable(o[labels[0]].pool_years for o in results))
    print(f"  pool (in library, before the cut)  {fmt_stats(pool_years)}")
    print(f"  held-out titles (what was watched) {fmt_stats(o[labels[0]].case.held_out.year for o in results)}")

    print(f"\nPaired against {SERVER} (better/worse/same on the held-out title's reciprocal rank)")
    for label in labels[1:]:
        comparison = Comparison(SERVER, label, [o[SERVER] for o in results], [o[label] for o in results])
        print(summarise(comparison))
        print()


def main() -> None:
    ranking.OLDER_BOOST_YEARS = OLDER_T  # before any thread starts
    config_dir = Path(os.environ["SHORTLIST_CONFIG"])
    engine = make_engine(config_dir)
    sessions = make_session_factory(engine)
    secrets = SecretBox(config_dir)
    builder = ContextBuilder(sessions, secrets, EventBus())
    ctx = builder.build(dry_run=True)
    cache = WatchCache(sessions)
    arms = make_arms(ctx.config)
    print(f"Candidate sources: {', '.join(next(iter(arms.values())).candidate_sources)} (no AI, no web search)")

    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in ctx.plex.sections():
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        library_index[kind].update(ctx.plex.build_library_index(section))
    shared = {
        "tmdb": ctx.tmdb,
        "library_index": library_index,
        "resolve_tmdb_id": lambda item: item.tmdb_id,
    }

    work: list[tuple[UserProfile, list[HoldoutCase]]] = []
    remaining = MAX_CASES
    with sessions() as session:
        user_ids = {u.plex_account_id: u.id for u in session.query(User).all()}
        for profile in builder.enabled_profiles(session):
            user_id = user_ids.get(profile.plex_account_id)
            if user_id is None or remaining == 0:
                continue
            history = [w for w in cache.watched_set(session, user_id) if not w.only_played_earlier]
            keep: Callable = first_watch_keep(replay_eval.first_plays(session, profile.plex_account_id))
            limit = CASES_PER_USER if remaining is None else min(CASES_PER_USER, remaining)
            cases = holdout_cases(profile, history, max_holdouts=limit, keep=keep)
            if cases:
                work.append((profile, cases))
                remaining = None if remaining is None else remaining - len(cases)

    print(f"Replaying {sum(len(c) for _, c in work)} cases over {len(work)} people, arms {', '.join(arms)}")
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(replay_user, cases, arms, shared) for _, cases in work]
        results = [outcomes for future in futures for outcomes in future.result()]
    if not results:
        print("No cases.")
        return
    report(results, arms, ctx.config.recency)


if __name__ == "__main__":
    main()
