# Recommendation-evaluation harness design (Wave 1)

From the September 2026 audit — see `.claude/docs/audit-2026-09-programme.md`. This is the item that
gates the engine-scoring work: without it, every ranking change is a guess.

## TL;DR

- **Offline against real data**, not fixtures, not live delivery. History comes from Shortlist's own
  `watched_titles` DB cache (already synced nightly); candidates hit real TMDB (read-only, same as
  every run); the library index is one read-only Plex call. **The harness never calls a Plex/plex.tv
  write** — so none of plex-safety's write-ordering rules are in scope for replay.
- **The metric is a paired per-case table, not a single score.** Rank/MRR before vs after, per
  (user, held-out title), read by a human. Aggregate MRR/hit-rate is a secondary summary, explicitly
  caveated as low-resolution at N=10–40.
- **New code lives in `shortlist/engine/eval/`** (pure, no server imports) plus a maintenance script
  `scripts/replay_eval.py` that wires it to the real server — mirroring `scripts/recheck_pms_assumptions.py`.
  Not shipped in the Docker image, not in CI.
- **A variant is just a second `EngineConfig`.** No strategy hook needed.
- **Interleaving is phase 2** — one Alembic migration (two nullable columns on `picks`) and a small
  `rows.py` hook, but attribution reuses `report_service.resolve_outcomes` wholesale.
- **Day-1 deliverable is leave-one-out replay only**, ~150 lines, because ~80% of the wiring
  (`ContextBuilder.build`, `WatchCache.watched_set`, `TmdbClient`'s `DbCache`) is pure reuse.

## 1. Offline or live?

**Offline against real recorded data, zero Plex writes.**

| Need                           | Source                                                                             | Why                                                                                                        |
| ------------------------------ | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Watch history                  | `WatchCache.watched_set(session, user_id)` — `watched_titles` (`db/models.py:653`) | Already synced, real, per-user, carries `tmdb_id`/`watch_count`/`viewed_leaf_count`. Zero extra Plex load. |
| Candidates                     | `ctx.tmdb` (real `TmdbClient`, real `DbCache`)                                     | The same read-only public API every normal run calls.                                                      |
| What's actually in the library | `ctx.plex.sections()` + `ctx.plex.build_library_index(section)`                    | Exactly what `pipeline._library_index` calls — a **read**.                                                 |
| Curator / trakt / search       | `ContextBuilder.build(dry_run=True)`                                               | Reuses the real credential wiring instead of re-implementing it.                                           |

**Why not `tests/fakes/fake_plex.py`?** That fake is built for privacy-flow fidelity (wizard e2e,
leak-safe ordering), not taste fidelity — it has no genre-coherent watch history. At N=10–40 real
users, a fixture-based number would look like a metric and measure nothing.

**Why not live delivery?** Leave-one-out replay only needs "would the pipeline have surfaced this
title", answerable from candidate generation + ranking alone. It never writes a collection or touches
a share filter. Live delivery is what **interleaving** needs later (§5) — reserve it for that.

Two tiers, both write-free:

1. **CI regression tests** — `tests/unit/test_eval_replay.py`, existing `mock_tmdb`/`engine_config`
   fixtures + hand-built tiny histories. Proves the _harness_ has no bugs (no leakage, correct rank
   math). Cannot tell you whether a ranking change is good — only that the ruler isn't bent.
2. **Real-data run** — `scripts/replay_eval.py`, run by hand against the real DB + real TMDB. This is
   the tool that answers "did this help". Not in CI (real credentials, TMDB budget), not in the image
   — same treatment as `scripts/recheck_pms_assumptions.py`.

## 2. What exactly is the metric?

**Not Cremonesi's literal construction** (true item + 1000 random negatives ranked by a global
scorer) — that targets algorithms that score the whole catalogue. Shortlist only ever ranks what
`gather_candidates` proposed. The faithful replay runs the **real pipeline** (gather → filter →
pre-rank → diversify) on the reduced history and sees where the held-out title lands. More honest
than synthesising negatives, and exactly what "would this variant have surfaced it" means.

Per case, per config:

- `gathered: bool` — did `gather_candidates` propose it at all (the recall ceiling ranking can't exceed)
- `drop_reason: str` — if gathered but removed, `filter_candidates`'s own reason
  (`already_watched` / `not_in_your_libraries` / `excluded_genre`), reused verbatim
- `rank_in_ranked: int | None` and `pool_size: int` — position in `ranking.pre_rank` output, with the
  pool size for context ("9th of 60" means something; "9th" alone doesn't)
- `in_final_row: bool` — survived `ranking.diversify_by_seed` into the top `row_size`
- `reciprocal_rank: float` — `1/rank`, else 0.0

**Aggregate:** `candidate_recall`, `hit_rate@row_size`, `MRR`. **Paired:** `Δreciprocal_rank` per
case plus improved/worsened/unchanged counts.

**Against tiny N:** 10–40 users × ≤5 holdouts each = ≤200 cases. A single scalar MRR delta (+0.03) is
not trustworthy — binary hit/miss has resolution 1/N, and a sign test on 50–200 paired cases has
power for a large consistent effect and essentially none for a subtle one. So:

- **The primary deliverable is the per-case table**, not the aggregate — a human scans for "who got
  worse, by name and title". Regressions here tend to be all-or-nothing (a title that ranked #3
  vanishing), not a p-value.
- The aggregate sign test is a **gut-check, not a gate**: report wins/losses/ties; trust the direction
  only when ≥~70% of non-tied cases agree, otherwise say "noise — read the table".
- The tool's own output must say this. It is good at "did I break something" and "does this fix the
  case I built it for". It is **not** powered to prove a subtle quality improvement is real.

## 3. Where the code lives

```
shortlist/engine/eval/__init__.py
shortlist/engine/eval/replay.py           # leave-one-out — pure, no shortlist.server imports
shortlist/engine/eval/interleave.py       # phase 2: team-draft blending (pure)
tests/unit/test_eval_replay.py
tests/unit/test_eval_interleave.py        # phase 2
scripts/replay_eval.py                    # wires replay.py to the real server; NOT in the image
```

Fits the architecture contract for `shortlist/engine/`: pure library, takes config dataclasses +
clients, returns reports. No server code, no API endpoint, no UI in the MVP — maintainer-only tool.

## 4. How a variant is defined

**Two `EngineConfig` instances.** Every ranking knob today (`recency`, `dislike_threshold`,
`watched_pct`, `candidates_pre_rank`, `candidate_sources`…) is a config field consumed by pure
functions in `ranking.py`/`candidates.py`/`history.py` — never a callback or strategy object. The
four scoring changes (genre avoidance, negative-signal multiplier, per-signal attribution, cast
signal) fit the same shape.

**Franchise awareness is the one to watch** — if it adds a _candidate source_ rather than a scoring
weight, the two variants' `candidate_sources` differ, meaning two independent `gather_candidates`
calls (trap 8), not one shared gather re-ranked twice.

**No strategy hook for MVP.** Optional later escape hatch: an unused-by-default
`score_fn: Callable[[Candidate], float] | None = None` on `replay_case`, for A/B'ing an experimental
scorer before it becomes a real config knob.

## 5. Interleaving (phase 2 — not day 1)

**Team-draft interleaving** (Chapelle et al. 2012) over two already-ranked, already-filtered lists
(`pre_rank` + `diversify_by_seed` per config):

```python
# shortlist/engine/eval/interleave.py
import random
from shortlist.engine.models import Candidate, MediaType


def team_draft_interleave(
    ranked_a: list[Candidate], ranked_b: list[Candidate], k: int, rng: random.Random
) -> list[tuple[Candidate, str]]:
    """Blend two ranked lists into one of length k. Coin-flip which team picks first each impression
    (removes position bias); each slot is credited to whichever team's turn produced it. A title both
    lists would propose is claimed once, by whichever team's turn came first — the other team's list
    simply advances past the duplicate on its own next turn."""
    order = ["a", "b"] if rng.random() < 0.5 else ["b", "a"]
    lists = {"a": ranked_a, "b": ranked_b}
    idx = {"a": 0, "b": 0}
    chosen: list[tuple[Candidate, str]] = []
    seen: set[tuple[int, MediaType]] = set()
    i = 0
    while len(chosen) < k and (idx["a"] < len(ranked_a) or idx["b"] < len(ranked_b)):
        team = order[i % 2]
        lst = lists[team]
        while idx[team] < len(lst) and (lst[idx[team]].tmdb_id, lst[idx[team]].media_type) in seen:
            idx[team] += 1
        if idx[team] < len(lst):
            c = lst[idx[team]]
            chosen.append((c, team))
            seen.add((c.tmdb_id, c.media_type))
            idx[team] += 1
        i += 1
    return chosen
```

**Blending without breaking anything downstream:** the row-size contract, leak-safe write ordering and
delivery ledger stay untouched because they never see a "variant" — they see one ordinary
`list[Pick]` of length `row_size`, exactly as today. The seam is _upstream_ of `_deliver_row`, inside
`_build_section_picks`/`RowPolicy` (`shortlist/engine/rows.py:1976`): when a row carries an
experiment, build the pool under config A and under `dataclasses.replace(cfg, **overrides)` — sharing
the raw `gather_candidates` pool when both agree on `candidate_sources`/`recent_count` (the common
case) — then call `build_interleaved_picks` instead of `picker.build_picks`. The resulting
`list[Pick]` flows into the same `_deliver_row` every other row uses.

**Schema change: one small migration**, same style as `0086_idle_hold.py`/`0088_row_show_days.py` —
two nullable columns, no backfill, zero effect on any pick outside an experiment:

```python
# shortlist/server/db/alembic/versions/0090_pick_variant.py  (confirm 0090 is still free)
def upgrade():
    op.add_column("picks", sa.Column("variant", sa.String(length=32), nullable=True))
    op.add_column("picks", sa.Column("experiment", sa.String(length=128), nullable=True))
```

`PickRow`/`Pick` each gain `variant` / `experiment`, **appended last** on the `Pick` dataclass — per
this codebase's own documented hazard: several call sites build `Pick` positionally, so a mid-list
field silently shifts every argument after it (the bug `RowSpec.fallback_name`'s comment warns about).

**Attribution reuses `report_service.resolve_outcomes`** (`report_service.py:566`, `BOUNCE_PERCENT=5`,
`SETTLING_HOURS=24`) wholesale — it already classifies every `(user, tmdb_id, media_type)` as
finished/watching/bounced/dropped. No new tracking. New query: group `PickRow` by
`(experiment, variant)`, join to outcomes, score **per impression** (one delivered row-instance = one
night's blend for one user): a variant wins that impression if its exclusively-credited titles with
outcome in `{watching, finished}` strictly outnumber the other's. **Ties are dropped, not split** —
that is what gives team-draft its power at tiny N, and the real reason to use it here. Report
`wins_a / wins_b / ties` + a binomial sign test, same gut-check-not-gate framing as §2.

**Safe on a real user's row?** Yes, under conditions — unlike replay, this _does_ change what a real
person sees:

1. Opt-in per row, starting with the maintainer's own account (matches "ask before live writes" and
   "my taste is not the default"). Never silently applied.
2. Both variants pass the _same_ `filter_candidates` watched/genre-exclusion logic — an experiment can
   never surface something the person's settings would block.
3. Both resolve to the _same_ collection/label/`library_keys`; `variant`/`experiment` are audit-only
   columns Plex never sees. No new privacy surface.
4. `row_size` must be identical both sides (validate at construction) — otherwise more slots confounds
   "did it help".
5. **Per-person rows only** — a shared row already aggregates many people's demand (`min_watchers`), so
   per-user paired attribution doesn't apply.

## 6. Smallest useful first version

**Day 1 = leave-one-out replay only.** No schema change, no server change, no UI, no CI gate.

```python
# shortlist/engine/eval/replay.py
"""Leave-one-out replay: would this EngineConfig have surfaced a title the person went on to watch,
run as of the moment just before they watched it. Pure — no shortlist.server imports, no Plex/plex.tv
WRITES anywhere in this module."""

from __future__ import annotations
from dataclasses import dataclass
from shortlist.engine.models import Candidate, EngineConfig, MediaType, UserProfile, WatchedItem
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine import candidates as candidates_mod, history as history_mod, ranking


@dataclass(frozen=True)
class HoldoutCase:
    user: UserProfile
    held_out: WatchedItem
    history_before: list[WatchedItem]  # every OTHER watch strictly BEFORE held_out.watched_at


@dataclass(frozen=True)
class ReplayOutcome:
    case: HoldoutCase
    config_label: str
    gathered: bool
    drop_reason: str = ""
    rank_in_ranked: int | None = None
    pool_size: int = 0
    in_final_row: bool = False
    reciprocal_rank: float = 0.0

    @property
    def hit(self) -> bool:
        return self.in_final_row


def holdout_cases(user: UserProfile, history: list[WatchedItem], *, max_holdouts: int = 5) -> list[HoldoutCase]:
    """Up to `max_holdouts` most-recent watches with a tmdb_id, each paired with history strictly
    before it — so a later watch never leaks into an earlier case's seeds (trap 2). The DB cache has
    already applied min_completion at sync time, so no completion re-filter is needed here."""
    eligible = sorted((w for w in history if w.tmdb_id is not None), key=lambda w: w.watched_at, reverse=True)
    eligible = eligible[:max_holdouts]
    return [
        HoldoutCase(user=user, held_out=w, history_before=[h for h in history if h.watched_at < w.watched_at])
        for w in eligible
    ]


def replay_case(
    case: HoldoutCase,
    config: EngineConfig,
    *,
    config_label: str,
    tmdb: TmdbClient,
    library_index: dict[MediaType, dict[int, int]],
    resolve_tmdb_id,
    run_day: int = 0,
    curator=None,
    trakt=None,
    search=None,
) -> ReplayOutcome:
    """Derive seeds/exclusions from `case.history_before` ONLY (never the full history — trap 1),
    gather -> filter -> rank, and report where `case.held_out` landed.

    Mirrors rows._candidate_pool's composition using the PUBLIC functions directly, not the private
    rows.py helper — see trap 4 for why this must never reuse EngineContext.previous_picks."""
    disliked = history_mod.disliked_seed_keys(case.history_before, config.dislike_threshold)
    seeds = history_mod.derive_seeds(
        case.history_before,
        resolve_tmdb_id,
        max_seeds=config.max_seeds,
        disliked=disliked,
    )
    pool = candidates_mod.gather_candidates(
        tmdb,
        seeds,
        sources=config.candidate_sources,
        curator=curator,
        trakt=trakt,
        search=search,
    )
    key = (case.held_out.tmdb_id, case.held_out.media_type)
    gathered = any((c.tmdb_id, c.media_type) == key for c in pool)
    watched_ids = {(w.tmdb_id, w.media_type) for w in case.history_before if w.tmdb_id is not None}
    dropped: list[tuple[Candidate, str]] = []
    valid = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=set(),
        dropped=dropped,
    )
    drop_reason = next((reason for c, reason in dropped if (c.tmdb_id, c.media_type) == key), "")
    kinds = [MediaType.MOVIE, MediaType.SHOW]
    ranked = ranking.cut_for_recency(valid, kinds, config.candidates_pre_rank, config.recency, run_day)
    rank = next((i + 1 for i, c in enumerate(ranked) if (c.tmdb_id, c.media_type) == key), None)
    final = ranking.diversify_by_seed(ranked, config.row_size)
    in_final = any((c.tmdb_id, c.media_type) == key for c in final)
    return ReplayOutcome(
        case=case,
        config_label=config_label,
        gathered=gathered,
        drop_reason=drop_reason,
        rank_in_ranked=rank,
        pool_size=len(ranked),
        in_final_row=in_final,
        reciprocal_rank=(1.0 / rank) if rank else 0.0,
    )
```

`scripts/replay_eval.py` wires it to real data, reusing existing server plumbing:

```python
"""Leave-one-out replay against the real server's cached history. Read-only: never calls
pipeline.run, _deliver_row, promote_*, or privacy.* — no import of any of them, so it is
structurally incapable of writing to Plex. Not copied into the Docker image.

    SHORTLIST_CONFIG=/path/to/real/config python scripts/replay_eval.py
"""
import os
from pathlib import Path
from shortlist.server.db.session import make_engine, make_session_factory
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.watch_cache import WatchCache
from shortlist.engine.eval.replay import holdout_cases, replay_case
from shortlist.engine.models import EngineConfig, MediaType

CONFIG_A = EngineConfig()                      # baseline
CONFIG_B = EngineConfig(dislike_threshold=1.0) # the change under test

config_dir = Path(os.environ["SHORTLIST_CONFIG"])
engine = make_engine(config_dir / "shortlist.db")
sessions = make_session_factory(engine)
builder = ContextBuilder(sessions, secret_box=..., bus=...)  # confirm real args at build time
ctx = builder.build(dry_run=True)
cache = WatchCache(sessions)

library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
for section in ctx.plex.sections():
    kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
    library_index[kind].update(ctx.plex.build_library_index(section))

with sessions() as session:
    for user in builder.enabled_profiles(session):
        history = cache.watched_set(session, user.id)
        for case in holdout_cases(user, history):
            a = replay_case(case, CONFIG_A, config_label="baseline", tmdb=ctx.tmdb,
                            library_index=library_index, ...)
            b = replay_case(case, CONFIG_B, config_label="variant-b", tmdb=ctx.tmdb,
                            library_index=library_index, ...)
```

~150 new lines across both files. `ContextBuilder.build`'s TMDB client already carries a persistent
`DbCache` (`context_builder.py:384`), so repeated seed overlaps across one user's holdouts are cached
for free, exactly as in production.

**Explicitly out of day-1 scope:** interleaving, any UI, CI-gating on replay numbers, a general
`--config-json` flag (edit the two `EngineConfig(...)` literals by hand — matches
`recheck_pms_assumptions.py`'s style; there is no CLI framework in this repo today, don't build one
for this).

## 7. How it's run, and what the output looks like

```
SHORTLIST_CONFIG=/config python scripts/replay_eval.py
```

```
Replay: baseline vs dislike_threshold=1.0  (6 users, up to 5 holdouts each = 24 cases)

user       title                          watched      rank(base)  rank(variant-b)  Δrr     note
---------- ------------------------------ ------------ ----------- ---------------- ------- --------------------
moohouse   The Bear S3                    2026-08-30   3           2                +0.17
moohouse   Deadliest Catch: The Viking    2026-08-14   —           —                 0.00   not gathered
danvex     Shogun                         2026-08-29   1           1                 0.00
danvex     Slow Horses S4                 2026-08-02   12          41                -0.08   WORSE — check this
jarrah     (title removed from library)   2026-07-28   —           —                 0.00   not_in_your_libraries
...

Aggregate                     baseline   variant-b    Δ
candidate recall (gathered)     0.83       0.83       0.00   (unaffected — as expected for a ranking-only change)
hit@row_size (15)               0.50       0.58       +0.08
MRR (of gathered)               0.29       0.35       +0.06
wins / losses / ties            9 / 4 / 8              9 improved of 13 non-tied (69%) — below the 70% bar,
                                                        read the per-case table before trusting this direction
```

## 8. Traps

1. **Leakage via the watched-exclusion set.** `filter_candidates`'s `watched_tmdb_ids` must come from
   `case.history_before`, never the full history. Computed from the full history, the held-out title
   is dropped every time as `already_watched` — a permanent 0% hit rate that reads as "the algorithm
   never works" rather than "the harness has a bug". Seeds, disliked-seed-keys and watched-exclusions
   must all come from the same reduced list.
2. **Temporal leakage across multiple holdouts.** A later watch must never appear in an earlier
   holdout's seed set. Fixed by `history_before = [h for h in history if h.watched_at < held_out.watched_at]`
   — never "history minus this one item".
3. **Deterministic scoring makes replay self-confirming.** `derive_seeds`'s 45-day recency half-life
   (`history.py:27`) means the most recent watch dominates seeding, so removing it just promotes the
   second-most-recent and barely changes the pool. A high hit rate can mean "the pool barely moved",
   not "the algorithm is smart". Always report `pool_size`/`candidate_recall` beside hit-rate so an
   A/B difference is traceable to _ranking a shared pool differently_ vs _gathering a different pool_.
4. **The idle-hold / carried-forward row must not leak in.** Real rows often reuse ~2/3 of yesterday's
   picks (`EngineContext.previous_picks`, `rows._reusable_prior`/`_held_for_idle`) rather than
   re-ranking nightly. Replay must always compute a _fresh_ ranking — never build an `EngineContext`
   with real `previous_picks`, or an A/B comparison becomes a comparison of two rotation schedules.
5. **A hard filter looks identical to "not gathered" unless drop reasons are tracked.** If genre
   avoidance ships as a filter rather than a re-score, extend `filter_candidates`'s drop-reason
   vocabulary so eval can tell working-as-intended from a bug.
6. **Library drift.** A title watched months ago may since have left the library;
   `not_in_your_libraries` is correct but is the harness testing a moving library, not a ranking miss.
   Report it as its own bucket, count it against neither variant.
7. **Kometa-polluted ratings silently disable `dislike_threshold`.** `ratings_are_trustworthy`
   (`history.py:271`) judges a whole account at once; a user whose ratings are mostly tool-written
   shows _zero_ effect from a `dislike_threshold` change — correct, not a bug. Surface
   `ratings_trusted: bool` per user so a flat line isn't misread.
8. **Cost and non-determinism.** Up to 40 users × 5 holdouts × 2 configs = up to 400 gather calls,
   mitigated by the existing `DbCache`. If either config includes `llm_web`, results are
   non-deterministic and non-repeatable — say so explicitly rather than silently excluding the source
   (that would make the eval config diverge from the one being evaluated). If a variant changes
   `candidate_sources` itself, gather must run twice, not once-shared — flag at construction by
   comparing the two configs' `candidate_sources`.

## Build order

1. `shortlist/engine/eval/__init__.py` + `replay.py` (§6) — pure, no server imports.
2. `tests/unit/test_eval_replay.py` using `mock_tmdb`/`engine_config`/`make_watched` from
   `tests/conftest.py`. Cover: holdout ordering (trap 2), no leakage from full history (trap 1),
   `gathered=False` vs `drop_reason` vs `hit=True` distinguishable, empty history, single-watch
   history (no valid case).
3. `scripts/replay_eval.py` — wire to `ContextBuilder.build(dry_run=True)` + `WatchCache.watched_set`.
4. Run against the real server for the FIRST ranking change, read the per-case table by hand, iterate.
5. **Only if replay shows a promising, non-trivial change:** phase 2 — `interleave.py`, migration
   `0090_pick_variant.py`, `RowSpec.experiment`, the `rows.py` hook, the `resolve_outcomes` win/loss
   query. Maintainer's own row first, per §5.

## Unconfirmed — verify before building

- `ContextBuilder`'s exact constructor signature (`secret_box`, `bus` args) and whether `build()`
  needs an event loop for anything replay would trigger. The call site was read; not every
  `__init__` line was.
- That migration `0090` is still the next free number when phase 2 is actually built.
