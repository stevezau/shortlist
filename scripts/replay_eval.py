"""Measure what the AI web search is told about a person's history, against the real server's watches.

Issue #152. It hides each person's most recent DISCOVERIES one at a time (a movie watched once, a show
whose first play was within a day of the watch), rebuilds what the AI would have been shown from only what
they had watched before, and runs the real web search and the real AI pick on it. Arms are history mixes,
(favourites, older) searched on top of the recent ones, set in `ARMS`. The first arm runs twice: the rerun is
the noise floor (the AI is not deterministic).

Each case is gathered ONCE per arm and ranked under every credit mode in `MODES` (how the AI-search titles are
credited to the watches that found them: not at all, in full, decayed by position, to one seed, or both). The
report gives, per arm and mode, how often the held-out title is in the row and how much of the final rows the AI
search supplied, and how much of that only a favourite or older watch's search found.

`SEARCH` picks the web search (the server's own, Exa, SearXNG, or the AI's native one) and `CURATOR` the AI
(the server's own, a local Ollama, or none: with Exa and no AI, `found_by_group` says how many cases the
first F favourite and first O older searches would have found the held-out title in, for every F and O).

    SHORTLIST_CONFIG=/path/to/COPY/of/config .venv/bin/python scripts/replay_eval.py

THIS SPENDS REAL MONEY unless `CURATOR` is local: AI tokens and web searches (cached ones are free). Run it
only against a COPY of the config directory, never the live one: it writes web-search cache rows. Set
`CASES_PER_USER` and `MAX_CASES` for a cheap smoke run first. It touches no collection, label or share
filter. Maintenance tooling, like `recheck_pms_assumptions.py` - not shipped in the Docker image, not run by CI.

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
from itertools import pairwise
from pathlib import Path

from sqlalchemy import func, select

from shortlist.engine.curator import make_curator
from shortlist.engine.eval.replay import (
    HoldoutCase,
    ReplayOutcome,
    first_watch_keep,
    furthest_stage,
    holdout_cases,
    mode_web_share,
    replay_case,
    summarise_picked,
)
from shortlist.engine.models import EngineConfig, MediaType, UserProfile
from shortlist.server.db.models import User, WatchEvent
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.context_builder import ContextBuilder, curator_kwargs, make_search_client
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_cache import WatchCache
from shortlist.server.settings_store import SettingsStore

# ── the experiment ────────────────────────────────────────────────────────────────────────────────
CASES_PER_USER = 5
MAX_CASES: int | None = None  # stop after this many cases in total; None = every case
ARMS: list[tuple[int, int]] = [(0, 0), (3, 3), (6, 6), (10, 10)]  # (favourites, older) searched on top of recent
MODES = ["off", "full", "decayed", "single", "decayed_single"]  # AI-search credit variants ranked per gather
RERUN = True  # run the first arm twice (the noise floor); False skips the duplicate
WORKERS = 4  # people replayed in parallel
SEARCH = "server"  # "server" (as configured) | "exa" | "searxng" | "native"
SEARXNG_URL = ""  # for SEARCH = "searxng" when the server has none set
CURATOR = "server"  # "server" (as configured) | "ollama" | "none" (the web search's own extraction, no AI pick)
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = ""
# ──────────────────────────────────────────────────────────────────────────────────────────────────


def arm_key(mix: tuple[int, int]) -> str:
    return f"{mix[0]}/{mix[1]}"


@dataclass(frozen=True)
class Arm:
    key: str
    config: EngineConfig


def make_setup(sessions, secrets: SecretBox, base: EngineConfig) -> tuple[object, object, EngineConfig]:
    """The web search client, the curator and the config the experiment's `SEARCH` and `CURATOR` ask for."""
    overrides: dict[str, object] = {}
    if SEARCH != "server":
        overrides["llm_web.search_provider"] = SEARCH
    if SEARXNG_URL:
        overrides["searxng.url"] = SEARXNG_URL
    if CURATOR == "ollama":
        overrides.update(
            {
                "curator.provider": "ollama",
                "curator.openai_base_url": OLLAMA_URL,
                "curator.model": OLLAMA_MODEL,
                "curator.api_key": "",
            }
        )
    with sessions() as session:
        store = SettingsStore(session, secrets)

        def get(key: str) -> object:
            return overrides[key] if key in overrides else store.get(key)

        search = make_search_client(get)
        curator = make_curator("" if CURATOR == "none" else get("curator.provider"), **curator_kwargs(get))
        mode = get("llm_web.search_provider") or "native"
    sources = list(base.candidate_sources)
    config = replace(
        base, candidate_sources=sources if "llm_web" in sources else [*sources, "llm_web"], web_search_provider=mode
    )
    return search, curator, config


def make_arms(config: EngineConfig) -> dict[str, Arm]:
    """The arms over the server's own settings and sources, with AI web search always among them."""
    return {
        arm_key(mix): Arm(arm_key(mix), replace(config, favourite_count=mix[0], older_count=mix[1])) for mix in ARMS
    }


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
    cases: list[HoldoutCase], arms: dict[str, Arm], shared: dict, search, curator, cache
) -> list[tuple[HoldoutCase, dict[str, ReplayOutcome]]]:
    """Every case under every arm. Keys are "0/0", "0/0'" (the first arm's rerun) and so on."""
    results = []
    first = next(iter(arms))
    for case in cases:
        outcomes: dict[str, ReplayOutcome] = {}
        for key, arm in arms.items():
            runs = (key, key + "'") if key == first and RERUN else (key,)
            for run in runs:
                outcomes[run] = replay_case(
                    case,
                    arm.config,
                    config_label=run,
                    modes=tuple(MODES),
                    curator=curator,
                    search=search,
                    web_search_cache=cache,
                    run_year=case.held_out.watched_at.year,
                    **shared,
                )
        results.append((case, outcomes))
    return results


def report(results: list[tuple[HoldoutCase, dict[str, ReplayOutcome]]], arms: dict[str, Arm]) -> None:
    first = next(iter(arms))
    keys = [k for arm in arms for k in (arm, arm + "'") if any(k in o for _, o in results)]
    width = 14
    print(f"{'user':<12}{'title':<30}" + "".join(f"{k:<{width}}" for k in keys))
    print("-" * (42 + width * len(keys)))
    for case, outcomes in results:
        row = f"{case.user.username:<12}{case.held_out.title[:28]:<30}"
        print(row + "".join(f"{furthest_stage(outcomes[k]) if k in outcomes else '':<{width}}" for k in keys))

    # Counted one by one, not as a furthest stage: every source runs, so a title can reach the row through TMDB
    # without the AI ever seeing it, and "in row" alone would hide what the AI did.
    checks = {
        "found by search": lambda o: o.found_by_search,
        "AI picked": lambda o: o.ai_picked,
        "in pool": lambda o: o.gathered,
        "AI fell back": lambda o: o.ai_fallback,
    }
    print("\nCases per arm where the held-out title was...")
    print(f"{'arm':<8}" + "".join(f"{name:<20}" for name in checks) + f"{'tokens':>10}{'new searches':>14}")
    for key in keys:
        got = [o[key] for _, o in results if key in o]
        print(
            f"{key:<8}"
            + "".join(f"{f'{sum(1 for o in got if check(o))}/{len(got)}':<20}" for check in checks.values())
            + f"{sum(o.tokens for o in got):>10}{sum(o.new_searches for o in got):>14}"
        )

    print("\nPer arm and AI-search credit mode: the held-out title in the row, and the final rows' AI-search titles...")
    print(f"{'arm':<8}{'mode':<16}{'in row':>10}{'rows titles':>13}{'AI-search':>11}{'share':>8}{'only history':>14}")
    for key in keys:
        got = [o[key] for _, o in results if key in o]
        for mode in MODES:
            share = mode_web_share(got, mode)
            in_row = sum(1 for o in got if mode in o.by_mode and o.by_mode[mode].in_row)
            pct = f"{share.web / share.titles:.0%}" if share.titles else "-"
            only = f"{share.via_history / share.titles:.0%}" if share.titles else "-"
            print(f"{key:<8}{mode:<16}{f'{in_row}/{len(got)}':>10}{share.titles:>13}{share.web:>11}{pct:>8}{only:>14}")
    print("  ('only history' = found only by a favourite or older watch's search, and none of the recent ones.)")

    print("\nPaired on 'the AI picked the held-out title':")

    def pair(before: str, after: str) -> None:
        both = [(o[before], o[after]) for _, o in results if before in o and after in o]
        if both:
            print("  " + summarise_picked(before, after, [b for b, _ in both], [a for _, a in both]))

    names = list(arms)
    for before, after in pairwise(names):
        pair(before, after)
    pair(first, first + "'")
    print(
        f"  ({first} -> {first}' is the same arm run twice: the gap is the noise floor any other difference must beat.)"
    )
    if CURATOR == "none":
        report_found_by_group([o[names[-1]] for _, o in results if names[-1] in o])
    print()
    for key in arms:
        fav, older = key.split("/")
        print(f"  {key}: {fav} favourite + {older} older searches")


def report_found_by_group(outcomes: list[ReplayOutcome]) -> None:
    """Cases where the first F favourite and first O older searches held the title, for each F and O."""
    steps = (0, 1, 2, 3, 5, 6, 8, 10)

    def found(outcome: ReplayOutcome, fav: int, older: int) -> bool:
        groups = outcome.found_by_group
        return (
            bool(groups.get("recent"))
            or any(n <= fav for n in groups.get("favourite", []))
            or any(n <= older for n in groups.get("older", []))
        )

    print(f"\nCases (of {len(outcomes)}) where the searches held the title, by favourites (rows) and older (columns):")
    print(f"{'':<6}" + "".join(f"{older:>6}" for older in steps))
    for fav in steps:
        print(f"{fav:<6}" + "".join(f"{sum(found(o, fav, older) for o in outcomes):>6}" for older in steps))


def main() -> None:
    config_dir = Path(os.environ["SHORTLIST_CONFIG"])
    engine = make_engine(config_dir)
    sessions = make_session_factory(engine)
    secrets = SecretBox(config_dir)
    builder = ContextBuilder(sessions, secrets, EventBus())
    # `dry_run=True` so nothing downstream could write even if this script grew a bug; the clients are the
    # real ones either way. The web-search cache is the one a real run uses.
    ctx = builder.build(dry_run=True)
    cache = WatchCache(sessions)
    search, curator, config = make_setup(sessions, secrets, ctx.config)
    arms = make_arms(config)

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
            # As a run sees it: shows only played earlier (#173) are never seeds, so never discoveries.
            history = [w for w in cache.watched_set(session, user_id) if not w.only_played_earlier]
            keep: Callable = first_watch_keep(first_plays(session, profile.plex_account_id))
            limit = CASES_PER_USER if remaining is None else min(CASES_PER_USER, remaining)
            cases = holdout_cases(profile, history, max_holdouts=limit, keep=keep)
            if cases:
                work.append((profile, cases))
                remaining = None if remaining is None else remaining - len(cases)

    print(f"Replaying {sum(len(c) for _, c in work)} cases over {len(work)} people, arms {', '.join(arms)}\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [
            pool.submit(replay_user, cases, arms, shared, search, curator, ctx.web_search_cache) for _, cases in work
        ]
        results = [item for future in futures for item in future.result()]
    report(results, arms)


if __name__ == "__main__":
    main()
