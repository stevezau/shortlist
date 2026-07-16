#!/usr/bin/env python
"""A/B benchmark: native LLM web search vs. Exa-backed search for the ``llm_web`` source.

Answers "is Exa better than the model's own web search?" empirically, on the metric that actually
matters for a library-resolved 'Picked for You' row: how many proposed titles are REAL and,
optionally, IN YOUR LIBRARY. Both arms use the SAME model for the pick step, so the only variable
is the search mechanism:

  * NATIVE — ``AnthropicCurator.recommend_web``: Claude searches the web itself, then proposes titles.
  * EXA    — ``ExaClient.search`` → ``AnthropicCurator.complete``: WE search via Exa, Claude proposes
             titles from the returned article text.

This is a benchmark, not a unit test: it makes REAL API calls and is never run in CI. Nothing here
touches Plex writes.

Required env:
    ANTHROPIC_API_KEY   the model (pick step for both arms + native search)
    EXA_API_KEY         the Exa search backend
    TMDB_APIKEY         resolve proposed titles → real TMDB ids (the hallucination check)
Optional:
    PLEX_URL, PLEX_TOKEN   measure the in-library yield (the metric that most matters)
    EVAL_ROUNDS=3          repeats per persona per arm, to average over run-to-run variance

Usage:
    ANTHROPIC_API_KEY=... EXA_API_KEY=... TMDB_APIKEY=... python scripts/eval_web_search.py
"""

from __future__ import annotations

import os
import statistics
import time
from dataclasses import dataclass, field

from shortlist.engine.candidates import _web_via_search
from shortlist.engine.clients.search import ExaClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.curator.anthropic import AnthropicCurator
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType

K = 20  # titles requested per call — the row-building budget

# Fixed personas (watchlists) so the comparison is reproducible. Each is a taste a real user might have.
PERSONAS: dict[str, list[str]] = {
    "prestige-sci-fi": ["Arrival", "Blade Runner 2049", "Dune", "Ex Machina", "Annihilation", "Severance"],
    "prestige-tv": ["Succession", "The Bear", "Better Call Saul", "Mad Men", "Chernobyl"],
    "comfort-comedy": ["The Office", "Parks and Recreation", "Brooklyn Nine-Nine", "Superbad", "Game Night"],
    "action-thriller": ["John Wick", "Sicario", "Mad Max: Fury Road", "Heat", "The Raid"],
}


def _seeds(titles: list[str]) -> list[Seed]:
    # tmdb_id is unused by the search arms (they read only .title); a stable index keeps Seed valid.
    return [Seed(tmdb_id=i, title=t, media_type=MediaType.MOVIE, weight=1.0) for i, t in enumerate(titles, 1)]


@dataclass
class ArmResult:
    proposed: int = 0
    resolved: int = 0  # proposed titles that map to a real TMDB id (1 - hallucination rate)
    in_library: int = 0  # resolved titles the Plex library actually holds (None-safe: -1 if no Plex)
    years: list[int] = field(default_factory=list)
    unique_titles: set[str] = field(default_factory=set)
    latency_s: list[float] = field(default_factory=list)


def _run_arm(name, fn, tmdb, library_ids, seeds, profile, rounds) -> ArmResult:
    r = ArmResult()
    for _ in range(rounds):
        t0 = time.monotonic()
        recs = fn(seeds, profile)
        r.latency_s.append(time.monotonic() - t0)
        r.proposed += len(recs)
        for rec in recs:
            mt = MediaType.SHOW if rec.get("media") == "show" else MediaType.MOVIE
            found = tmdb.search(rec["title"], mt, year=rec.get("year"))
            if not found:
                continue
            r.resolved += 1
            r.unique_titles.add(f"{found['id']}-{mt.value}")
            date = found.get("release_date") or found.get("first_air_date") or ""
            if len(date) >= 4 and date[:4].isdigit():
                r.years.append(int(date[:4]))
            if library_ids is not None and found["id"] in library_ids:
                r.in_library += 1
    return r


def _pct(a: int, b: int) -> str:
    return f"{(100 * a / b):.0f}%" if b else "—"


def main() -> None:
    for key in ("ANTHROPIC_API_KEY", "EXA_API_KEY", "TMDB_APIKEY"):
        if not os.environ.get(key):
            raise SystemExit(f"missing required env {key} — this benchmark makes real API calls (see the docstring)")
    rounds = int(os.environ.get("EVAL_ROUNDS", "3"))
    curator = AnthropicCurator(api_key=os.environ["ANTHROPIC_API_KEY"])
    exa = ExaClient(os.environ["EXA_API_KEY"])
    tmdb = TmdbClient(os.environ["TMDB_APIKEY"])

    library_ids = None
    if os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN"):
        from shortlist.engine.clients.plex_pms import PlexClient

        plex = PlexClient(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"])
        library_ids = set()
        for section in plex.sections():
            index, _episodes = plex.build_library_index(section)  # tmdb_id -> ratingKey
            library_ids |= set(index)
        print(f"library: {len(library_ids)} titles with a TMDB id\n")

    arms = {
        "NATIVE (Claude web search)": lambda seeds, profile: curator.recommend_web(profile, seeds, K),
        "EXA (Exa search → Claude)": lambda seeds, profile: _web_via_search(curator, exa, profile, seeds, K),
    }
    totals = {name: ArmResult() for name in arms}

    for persona, titles in PERSONAS.items():
        print(f"── {persona} ──")
        seeds = _seeds(titles)
        profile = UserProfile(username=persona, plex_account_id=0, user_type=UserType.SHARED, history=[])
        for name, fn in arms.items():
            r = _run_arm(name, fn, tmdb, library_ids, seeds, profile, rounds)
            lib = f" · in-library {_pct(r.in_library, r.resolved)}" if library_ids is not None else ""
            med_year = f"{statistics.median(r.years):.0f}" if r.years else "—"
            print(
                f"  {name:30} proposed {r.proposed:3} · resolved {_pct(r.resolved, r.proposed)}{lib}"
                f" · unique {len(r.unique_titles):3} · median yr {med_year}"
                f" · {statistics.mean(r.latency_s):.1f}s"
            )
            t = totals[name]
            t.proposed += r.proposed
            t.resolved += r.resolved
            t.in_library += r.in_library
            t.years += r.years
            t.unique_titles |= r.unique_titles
            t.latency_s += r.latency_s
        print()

    print("== OVERALL ==")
    for name, t in totals.items():
        lib = f" · in-library {_pct(t.in_library, t.resolved)}" if library_ids is not None else ""
        med_year = f"{statistics.median(t.years):.0f}" if t.years else "—"
        print(
            f"  {name:30} resolve {_pct(t.resolved, t.proposed)}{lib}"
            f" · unique {len(t.unique_titles)} · median yr {med_year} · {statistics.mean(t.latency_s):.1f}s/call"
        )
    print(
        "\nVERDICT: the arm with the higher IN-LIBRARY count wins for a 'Picked for You' row "
        "(a title you don't own can't become a row). Without Plex, judge on resolve-rate + unique + recency."
    )


if __name__ == "__main__":
    main()
