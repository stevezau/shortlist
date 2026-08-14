# Plan: record what each ROW cost, instead of repeating the person's total under every row

**Status:** DONE — shipped 2026-08-13.

## The problem, from the live server

On run #7, the Rows tab shows Alex Mastroianni under both `✨ Picked for You` and
`🎯 Because you watched` with **byte-identical** figures in each panel — `7m 22s · 15,917 AI tokens
(web search 15,917)` — and both row cards read `Per-person · building — 4 of 46 done`. All four
finished people repeat the same way (1m 5s, 9m 19s, 8m 20s).

That reads as two rows racing each other, each independently costing 7m 22s. Neither is true.

**There is no per-row cost recorded anywhere.** `duration_s`, `llm_tokens`, `llm_tokens_by_step` and
`exa_searches` all live on `UserRunReport` (`engine/models.py:886-895`) — one set per PERSON per RUN.
The engine stamps that timer around the whole person: `started = time.monotonic()`
(`engine/pipeline.py:349`) wraps `rows._run_user`, which builds _every_ row that person is in, and
`user_report.duration_s` lands at `pipeline.py:369`.

The Rows tab then spreads the entire user result into each row group, narrowing only two fields
(`web/src/lib/run-rows.ts:152-156`):

```js
result: {
  ...user,          // duration_ms, llm_tokens, exa_searches, status, diff — ALL whole-run
  breakdown: mine,  // row-scoped
  picks: ...,       // row-scoped
}
```

Everything the panel header prints comes from the un-narrowed spread. Hence the identical numbers.

**The picks themselves are correct** and must not be touched — they render from `breakdown`, which is
genuinely per-(row, library). This is a header-metrics and progress-line problem only.

## Why a naive "split the cost per row" would be a lie

All AI token cost is incurred **before** the per-row loop, and it is **pool-scoped, not row-scoped**.

`_warm_start` builds every row's candidate pool up front (`rows.py:1606-1607`), and `pools_for`
memoises on `pool_key`, so rows sharing media/sources/seeds share one gather. `_record_gather`'s own
docstring is explicit (`rows.py:851-853`):

> This is the ONLY AI cost now — the AI is used only to FIND titles (web search). Ranking the pool
> and writing each row's reason are done in code (`picker.build_picks`), so there is no per-row LLM
> spend to attribute anymore.

and (`rows.py:856-857`):

> Most users have a single pool shared by every row, so this is usually one entry.

So Alex's 15,917 tokens are **one web-search gather**. If both his rows drew from one pool — the
common case — then any per-row token figure is an allocation invented by the UI, not a measurement.
Where rows genuinely use different pools (differing media, sources or seed budgets), the split is
real, and `_record_gather` already carries a `pool_label` to name it.

Duration does split, but lopsidedly. Everything expensive — the history fetch and the whole gather —
precedes the loop. The loop body is `pools_for` (a cache hit), a sort, in-code ranking, and the Plex
delivery writes. Expect something like:

```
Picked for You         ~12s
Because you watched     ~9s
Shared setup (history + candidate gather)   ~7m   · 15,917 tokens · 1 pool, used by both rows
```

That is more useful than today's page and it is true. It does mean the headline becomes "most of
this person's cost belongs to neither row."

## The second symptom is not a bug

Both cards saying `building — 4 of 46 done` is `group.pending`, computed from `expected_users` minus
whoever has reported (`run-rows.ts:222-253`). A person's result lands only once they have finished
**all** their rows, so every per-person row group necessarily shows the same count.

The rows are not running concurrently. Within `_run_user` a person's rows are built in sequence — but
they all complete at the same instant, so lockstep progress is a faithful reflection of the engine.
There is no per-row progress to be had without splitting persistence per row, which is not worth it.
Fix the wording, not the mechanism.

## What already exists (do not rebuild)

- `RunUser.breakdown` is already per-(row, library) with `row_slug` — the row grouping works.
- `_record_gather` already receives and records `pool_label` (`rows.py:846`), and files gather traces
  under `report.trace["gathers"]`.
- `run_shared_rows.duration_ms` is **already genuinely per-row** — a shared row is one row, one
  result. No change needed there.
- `rows_considered` (`engine/models.py:915`) is the established precedent for per-row, per-person data
  keyed by row slug, including its "`{}` on a legacy run means _not recorded_, never _none_" rule.

## Design

### 1. Storage — one nullable JSON column

`run_users.cost`, nullable. `NULL` means legacy, rendered "not recorded", never zero.

```json
{
  "setup_ms": 421000,
  "rows": {
    "picked-for-you": { "duration_ms": 12040, "blocked_ms": 310 },
    "because-you-watched": { "duration_ms": 9120, "blocked_ms": 880 }
  },
  "pools": [
    {
      "label": "movie · tmdb, llm_web",
      "tokens": 15917,
      "exa_searches": 3,
      "duration_ms": 398000,
      "rows": ["picked-for-you", "because-you-watched"]
    }
  ]
}
```

One JSON blob rather than three scalar columns or a new table: `rows_considered`, `breakdown`, `diff`
and `llm_tokens_by_step` are all JSON on this table already (`server/db/models.py:311-332`), so this
follows the established shape and keeps the migration to a single nullable add.

`pools[].rows` is the load-bearing field. It is what lets the UI say "one pool, used by both rows"
rather than dividing a number that was never divided.

Migration `0069` (latest is `0068_run_began_at`). Additive, nullable, no backfill.

### 2. Engine

`UserRunReport` gains `setup_s: float`, `row_timing: dict[str, dict[str, float]]`,
`pool_costs: list[dict]`. Engine stays pure — no server imports.

The engine measures in **seconds** (`setup_s`, matching the existing `duration_s`) and
`run_persistence` converts to integer milliseconds on write, exactly as it already does for
`duration_ms` (`services/run_persistence.py:431`). The `_s` / `_ms` split in this document is that
boundary, not two different numbers.

- **`setup_s`** — timed in `_run_user` (`rows.py:2055`) from the history fetch through the end of
  `_warm_start`/`_cold_start`; everything before `for spec in specs`.
- **Per-row duration** — one timer per loop iteration.
- **`blocked_ms`** — a `_timed_lock(ctx, report)` context manager replaces the bare
  `with ctx.write_lock:` at the delivery write inside `_deliver_row`'s `_deliver_locked`
  (`rows.py:1989`), adding the _acquire_ wait to the row the report currently points at.

  **Only that one site.** The other three write-lock sites — `_drop_cold_skipped_rows`
  (`rows.py:1146`), `_remove_muted_and_retired` (`rows.py:1185`) and `_record_demand`
  (`rows.py:1683`) — all run during SETUP, before the row loop, so their wait is already inside
  `setup_s` and there is no row to charge it to. They stay bare. Setup's own lock wait is not broken
  out separately: setup is one sequential span per person, so it is never compared against a sibling
  the way two rows are, which is the distortion `blocked_ms` exists to prevent.

  The accumulator lives on **`report`, not `ctx`**. `ctx` is shared across the whole user pool, so a
  bucket there would cross-attribute one person's wait to another's row. `report` is per-user and
  therefore per-thread.

- **`pool_costs`** — extend `_record_gather` to append an entry per pool computation. Which rows used
  a pool comes from recording `spec.slug` against `pool_key` on every `pools_for` call, **hit or
  miss** — a cache hit is exactly the case that proves two rows shared a pool.

**Edge case:** `pools_for` returning `None` (every source for that row is down) `continue`s the loop.
That row must still get a `rows` entry, or the UI cannot distinguish "finished fast" from "not
recorded".

**Cold start:** no pools are built, so `pools` is `[]` and setup covers `_cold_start`. Not an error
state — must not render as missing data.

### 3. API

`RunUserOut.cost: RunCostOut | None`, serialized straight from the column at `runs.py:238`'s
neighbourhood. Pending/synthesised users (`runs.py:272-292`) get `None`.

### 4. UI

- `RunRowPerson` gains `cost` — that row's `{duration_ms, blocked_ms}`. `groupRunByRow` stops letting
  the spread at `run-rows.ts:152-156` imply the person's total is the row's.
- `UserPanel` header with a row cost present: `12s · shared setup 7m 1s`, with tokens moved to their
  own shared-setup line naming the pool and the rows that shared it.
- `blocked_ms` shown only when it is **≥10% of that row's `duration_ms`**. At the default
  `run.concurrency`
  of 1 (`services/context_builder.py:223`) it is always ~0 and would be noise.
- `cost === null` → "timing not recorded for this run". Never `0s`.
- `rowSummary` (`run-rows.ts:318-321`) → `"building — 4 of 46 people done"`, so lockstep progress
  reads as expected rather than as two rows racing.
- **Delete the vestigial per-row token display.** `RowSection` renders
  `breakdown[].llm_tokens` as "AI tokens the curator spent choosing this row's picks"
  (`user-panel.tsx:194-209`), but no engine code has written that field since curation moved
  in-code — `rows.py` only ever writes `report.llm_tokens` / `report.llm_tokens_by_step`, so it is
  always 0 and the block never renders. It is a per-row token figure of exactly the kind this work
  exists to stop showing; leaving it would put two competing stories on one page.

### 5. Testing

- Engine unit: the setup/row/pool split; a cold-start user (no pools); a row whose sources are down;
  blocked time landing in the right bucket at concurrency > 1.
- Migration applies to a **populated** DB and is not a no-op — the `0032` lesson from CLAUDE.md.
- API: a legacy `run_users` row serializes `cost: null`.
- Vitest: per-row cost narrowing in `groupRunByRow`; null cost renders "not recorded", never `0s`.
- Architecture Review before commit — this adds a migration and touches the engine hot path.

## Scope

Purely additive measurement. No privacy, share-filter, delivery-ordering or promote code is touched,
and no existing recorded field changes meaning.

## Rejected alternatives

- **Allocate shared gather cost across rows** (evenly or pro-rata by picks) so each row shows a
  self-contained total. Sums cleanly, but the per-row token number would be an allocation presented
  as a measurement. Owner chose the honest split.
- **Relabel in the UI only** — move the metrics to the person-level header and change no engine code.
  Cheapest fix for the misleading part, but yields no per-row timing at all.
- **Make pools the primary UI unit.** Most faithful to the engine, but a much larger UI change than
  the problem warrants.
