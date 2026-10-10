"""Measure what the AI web search is told about a person's history, against the real server's watches.

Issue #152. It hides each person's most recent DISCOVERIES one at a time (a movie watched once, a show
whose first play was within a day of the watch), rebuilds what the AI would have been shown from only what
they had watched before, and runs the real web search and the real AI pick on it. Four arms:

    A  recent taste text, no favourite searches (what a run sends today)
    B  wide taste text (recent, long-time favourites, their ratings)
    C  wide taste text, plus the web searches of FAVOURITES long-time favourites
    D  as B, but the AI is switched off: code ranks everything the search found

A, B and C run twice; the A rerun is the noise floor (the AI is not deterministic).

    SHORTLIST_CONFIG=/path/to/COPY/of/config .venv/bin/python scripts/replay_eval.py

THIS SPENDS REAL MONEY: AI tokens and web searches (cached ones are free). Run it only against a COPY of
the config directory, never the live one: it writes web-search cache rows. Set `MAX_CASES` and `ARMS` below
for a cheap smoke run first. It touches no collection, label or share filter. Maintenance tooling, like
`recheck_pms_assumptions.py` - not shipped in the Docker image, not run by CI.

Every source the server has switched on runs, as in a real run, so "in the row" means the title would have
reached the person whatever found it; AI web search is added if it is off. The search cache serves today's
extractions, not the ones from the day of each watch.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from shortlist.engine.eval.replay import (
    HoldoutCase,
    ReplayOutcome,
    first_watch_keep,
    furthest_stage,
    holdout_cases,
    replay_case,
    summarise_picked,
)
from shortlist.engine.models import EngineConfig, MediaType, UserProfile
from shortlist.server.db.models import User, WatchEvent
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import WatchCache

# ── the experiment ────────────────────────────────────────────────────────────────────────────────
MAX_HOLDOUTS_PER_USER = 5
MAX_CASES: int | None = None  # stop after this many cases in total; None = every case
ARMS = ("A", "B", "C", "D")  # which arms to run; ("A", "B") is a cheap smoke test
FAVOURITES = 8  # arm C: long-time favourites searched in addition to the recent ones
WORKERS = 4  # people replayed in parallel
# A, B and C run twice; the AI is not deterministic, so the A rerun measures how far a number moves by itself.
REPEATED = ("A", "B", "C")
# ──────────────────────────────────────────────────────────────────────────────────────────────────

ARM_NOTES = {
    "A": "recent taste text, no favourite searches",
    "B": "wide taste text",
    "C": f"wide taste text + {FAVOURITES} favourite searches",
    "D": "no AI pick: the first 40 titles the search extracted, ranked in code (what a run does without an AI)",
}


class _NoAi:
    """Wraps a curator so the web search extracts titles and code ranks them, with no AI call (arm D)."""

    can_complete = False
    supports_native_web_search = False
    name = "none"
    last_tokens = 0
    last_output_tokens = 0

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        raise AssertionError("arm D must never ask the AI")


@dataclass(frozen=True)
class Arm:
    key: str
    config: EngineConfig
    curator: object


def make_arms(base: EngineConfig, curator: object) -> dict[str, Arm]:
    """The arms over the server's own settings and sources, with AI web search always among them."""
    sources = list(base.candidate_sources)
    web = replace(base, candidate_sources=sources if "llm_web" in sources else [*sources, "llm_web"])
    configs = {
        "A": replace(web, taste_mode="recent", favourite_count=0),
        "B": replace(web, taste_mode="wide", favourite_count=0),
        "C": replace(web, taste_mode="wide", favourite_count=FAVOURITES),
        "D": replace(web, taste_mode="wide", favourite_count=0),
    }
    return {key: Arm(key, configs[key], _NoAi() if key == "D" else curator) for key in ARMS}


def first_plays(session, account_id: int) -> dict[int, datetime]:
    """Show rating key -> when this person first played an episode of it, from Plex's play log."""
    rows = session.execute(
        select(WatchEvent.show_rating_key, func.min(WatchEvent.viewed_at))
        .where(WatchEvent.plex_account_id == account_id, WatchEvent.show_rating_key.is_not(None))
        .group_by(WatchEvent.show_rating_key)
    ).all()
    # SQLite hands datetimes back naive; they were stored as UTC.
    return {key: first if first.tzinfo else first.replace(tzinfo=UTC) for key, first in rows}


def replay_user(
    cases: list[HoldoutCase], arms: dict[str, Arm], shared: dict, search, cache
) -> list[tuple[HoldoutCase, dict[str, ReplayOutcome]]]:
    """Every case under every arm. Keys are "A", "A'" (the rerun) and so on."""
    results = []
    for case in cases:
        outcomes: dict[str, ReplayOutcome] = {}
        for key, arm in arms.items():
            runs = (key, key + "'") if key in REPEATED else (key,)
            for run in runs:
                outcomes[run] = replay_case(
                    case,
                    arm.config,
                    config_label=run,
                    curator=arm.curator,
                    search=search,
                    web_search_cache=cache,
                    run_year=case.held_out.watched_at.year,
                    **shared,
                )
        results.append((case, outcomes))
    return results


def report(results: list[tuple[HoldoutCase, dict[str, ReplayOutcome]]], arms: dict[str, Arm]) -> None:
    keys = [k for k in ("A", "A'", "B", "B'", "C", "C'", "D") if any(k in o for _, o in results)]
    print(f"{'user':<12}{'title':<30}" + "".join(f"{k:<18}" for k in keys))
    print("-" * (42 + 18 * len(keys)))
    for case, outcomes in results:
        row = f"{case.user.username:<12}{case.held_out.title[:28]:<30}"
        print(row + "".join(f"{furthest_stage(outcomes[k]) if k in outcomes else '':<18}" for k in keys))

    # Counted one by one, not as a furthest stage: every source runs, so a title can reach the row through TMDB
    # without the AI ever seeing it, and "in row" alone would hide what the AI did.
    checks = {
        "found by search": lambda o: o.found_by_search,
        "AI picked": lambda o: o.ai_picked,
        "in pool": lambda o: o.gathered,
        "in row": lambda o: o.in_final_row,
        "AI fell back": lambda o: o.ai_fallback,
    }
    print("\nCases per arm where the held-out title was...")
    print(f"{'arm':<6}" + "".join(f"{name:<18}" for name in checks) + f"{'tokens':>10}{'new searches':>14}")
    for key in keys:
        got = [o[key] for _, o in results if key in o]
        print(
            f"{key:<6}"
            + "".join(f"{f'{sum(1 for o in got if check(o))}/{len(got)}':<18}" for check in checks.values())
            + f"{sum(o.tokens for o in got):>10}{sum(o.new_searches for o in got):>14}"
        )

    print("\nPaired on 'the AI picked the held-out title':")

    def pair(before: str, after: str) -> None:
        both = [(o[before], o[after]) for _, o in results if before in o and after in o]
        if both:
            print("  " + summarise_picked(before, after, [b for b, _ in both], [a for _, a in both]))

    pair("A", "B")
    pair("B", "C")
    pair("A", "A'")
    print("  (A -> A' is the same arm run twice: the gap is the noise floor any other difference must beat.)")
    if "D" in keys:
        got = [o["D"] for _, o in results if "D" in o]
        print(f"  D (no AI): in the row for {sum(o.in_final_row for o in got)} of {len(got)} cases")
    print()
    for key in arms:
        print(f"  {key}: {ARM_NOTES[key]}")


def main() -> None:
    config_dir = Path(os.environ["SHORTLIST_CONFIG"])
    engine = make_engine(config_dir)
    sessions = make_session_factory(engine)
    builder = ContextBuilder(sessions, SecretBox(config_dir), EventBus())
    # `dry_run=True` so nothing downstream could write even if this script grew a bug; the clients are the
    # real ones either way. The curator, search client and web-search cache are the ones a real run uses.
    ctx = builder.build(dry_run=True)
    cache = WatchCache(sessions)
    arms = make_arms(ctx.config, ctx.curator)

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
            history = cache.watched_set(session, user_id)
            keep: Callable = first_watch_keep(first_plays(session, profile.plex_account_id))
            limit = MAX_HOLDOUTS_PER_USER if remaining is None else min(MAX_HOLDOUTS_PER_USER, remaining)
            cases = holdout_cases(profile, history, max_holdouts=limit, keep=keep)
            if cases:
                work.append((profile, cases))
                remaining = None if remaining is None else remaining - len(cases)

    print(f"Replaying {sum(len(c) for _, c in work)} cases over {len(work)} people, arms {', '.join(arms)}\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(replay_user, cases, arms, shared, ctx.search, ctx.web_search_cache) for _, cases in work]
        results = [item for future in futures for item in future.result()]
    report(results, arms)


if __name__ == "__main__":
    main()
