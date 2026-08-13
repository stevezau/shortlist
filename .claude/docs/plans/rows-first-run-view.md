# Plan: make the run view rows-first, and give a shared row a run record

**Status:** DONE — all four phases shipped 2026-08-12. Kept as the record of why the run view is
shaped this way. Design chosen by the owner ("rebuild around rows"); the open question below was
resolved in favour of option (b), so `rows_considered` exists on `run_users`.

**One deliberate deviation from the chosen mockup.** It showed the tabs as `[ Rows ] [ Log ]`, i.e.
People deleted. People was KEPT as a secondary tab: `Rows` is the default and the primary axis, which
is what the choice was about, but the People tab carries the failures-first nav, one person's picks,
their error bucket and token breakdown — none of which the row tree reproduces. Deleting working UI
to match a sketch is the irreversible half of the decision, so it was left in. Say the word and it
goes.

## The problem, from the live server

Run #37 (2026-08-12, SFLIX) recorded **46 users, every one `skipped`** — "None of this person's rows
were due to rebuild in this run" — while the shared row `popular_library_name_on_sflix` built **40
picks successfully**. The run page showed 46 rows of noise and nothing at all about the only work the
run actually did.

Three separate causes, and they need fixing in this order:

1. **A shared row gets no run record.** `_run_shared` produces a full `UserRunReport` (status, diff,
   breakdown, trace, tokens), but `persist_report` files reports by user slug — a shared row's slug is
   `shared_<slug>`, which is nobody — so it hits `continue` (`run_persistence.py:249`). All that
   survives is `_emit_shared_row_event`: an Event carrying status/picks-count/diff-titles and **no
   trace, no breakdown, no tokens**. Its picks are never written to `picks` either, so a shared row's
   40 titles exist only in that blob and on Plex.
2. **The run page has no axis for a row.** `run-detail.tsx` tabs are People and Log. A shared row
   belongs to no person, so there is nowhere for it to appear.
3. **The skip wall.** Per-person skips are correct but repeated 46 times, burying everything else.

## What already exists (do not rebuild)

- `RunUser.breakdown` is **already per-(row, library)** — `row_slug`, `row_title`, `library_key`,
  `library_title`, added/removed/kept/deleted, created, picks. The per-person half of a rows-first
  tree can be assembled by grouping `users[].breakdown[]` by `row_slug`. **No migration for that half.**
- `RunUser.trace` + `GET /runs/{id}/users/{user_id}/trace` + `run-user-trace.tsx` (1622 lines) are the
  existing trace view. The shared variant should reuse it, not fork it.
- `_with_provenance(breakdown, picks)` already joins picks onto breakdown entries.

## Phase 1 — persist a shared row's result (migration)

New table `run_shared_rows`, mirroring `RunUser` minus the user:

| column                                                            | notes                                                                                                                                                                               |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run_id`                                                          | FK `runs.id`, PK part. Cascade with the run — unlike `run_users`, there is no user record to protect, so the `RESTRICT` policy in `models.User` does not apply.                     |
| `collection_slug`                                                 | PK part. The row's own slug, NOT the `shared_` prefixed report slug.                                                                                                                |
| `row_title`                                                       | as rendered at run time — the row can be renamed later and a past run must not change.                                                                                              |
| `status` / `error` / `reason`                                     | same three-way meaning as `RunUser`; `reason` is a non-failing skip.                                                                                                                |
| `duration_ms`, `llm_tokens`, `llm_tokens_by_step`, `exa_searches` | same shapes.                                                                                                                                                                        |
| `diff`, `breakdown`, `trace`                                      | same shapes.                                                                                                                                                                        |
| `picks` (JSON)                                                    | **not** the `picks` table: `PickRow.user_id` is non-nullable and RESTRICT-keyed, and nullable-user pick rows would pollute every per-user hit-rate query. Self-contained JSON here. |

Write it from `persist_report`'s existing `startswith(SHARED_SLUG_PREFIX)` branch — keep
`_emit_shared_row_event` as well, since plex-safety rule 10 wants the audit event regardless.

**Backfill:** none. Past runs keep only their event; the UI must render an empty shared-rows list
without implying the row never built.

## Phase 2 — API

- `GET /runs/{id}` gains `shared_rows: [...]` (same field set as a user entry, plus `row_title`,
  minus the user fields; `has_trace` computed the same way).
- `GET /runs/{id}/rows/{collection_slug}/trace` mirrors the per-user trace endpoint, including the
  `_request_outcomes` overlay.
- Regenerate `web/openapi.snapshot.json` + `web/src/lib/api-schema.d.ts` (recipe in
  `tests/unit/test_openapi_snapshot.py`; run `openapi-typescript` from `web/node_modules/.bin`).

## Phase 3 — the rows-first run view

Tabs become **Rows** / **Log**. The Rows tab lists every row the run touched:

```
▾ ✨ Picked for You            per-person
    46 people · 0 built, 46 not due
    └ sarah   skipped  not due   ›
▾ 👥 Popular Movies on SFLIX   shared
    ok · 40 picks · +12 −3 · 48s   [Trace ›]
```

- Per-person rows: group `users[].breakdown[]` by `row_slug`; each person links to their existing
  trace page.
- Shared rows: straight from `shared_rows`, linking to the new trace endpoint.

**RESOLVED — option (b) is built.** `run_users.rows_considered` is
`{row_slug: "due" | "not_due" | "muted" | "not_in_audience"}`, written for EVERY user (not just
skipped ones) from the three conditions `_run_user` already applies. `"due"` is intent, not outcome —
the person's own `status` says what became of it, and calling it `"built"` would claim a success a
later pipeline error can still take away. `{}` on a legacy run and on a cold-start skip (which never
reaches the decision); the UI must render that as "not recorded", never as "no rows considered".

So the tree is: for each per-person row, the people whose `rows_considered` names it, grouped by that
value and by their own status. Shared rows come straight from `shared_rows`.

## Phase 4 — tests

- Python: shared-row persistence across ok / skipped / error (the matrix rule); the trace endpoint's
  404 and empty-trace cases; a run whose ONLY work was a shared row reports it.
- `test_openapi_snapshot.py` will fail until the snapshot is regenerated — that is the guard working.
- vitest: the row-grouping helper (pure function — put it in `lib/`, not the component).
- e2e: the run detail flow changed, so `-m e2e` is required, not optional.

## Gates

- Adds an Alembic migration and changes a Plex-adjacent audit path → **Architecture Review is
  mandatory** before the commit lands (`.claude/CLAUDE.md`, Conventions).
- Full `pytest`, `pnpm test`, `pnpm build`, and `-m e2e` before committing.

## Open defects (found on live runs 43–45, 2026-08-13)

All three are the same shape as the ones this plan already fixed: the UI stating something the
engine no longer does. Fix in this order.

1. **A shared row records no trace at all.** `_shared_row`'s trace was populated by
   `_record_gather` — the search stage — and the popularity rework deleted the search. Live: run 42
   trace 98,998 bytes, runs 43/44 trace `2` (`{}`). `has_trace` is therefore false and the Trace
   button correctly hides, so the shared-row trace built earlier the same day is gone. A popularity
   row SHOULD trace, and every input already exists in the function: the watcher counts, the floor
   (`threshold`), which titles cleared it, which were dropped for not being in the target library,
   and which `blocked_shared_seeds` removed. Write that into `user_report.trace` and give the trace
   page a branch that renders a ranking rather than a search.

2. **A RUNNING run renders "This run built no rows".** `groupRunByRow` scopes to rows with a `due`
   decision or a delivery, and mid-run nothing is persisted — so a live run hits the empty state,
   which then explains itself with "Runs from before this view existed", a confidently wrong reason
   for a run that started seconds ago. The empty state needs to split three ways: still running
   (say what it is doing — the phase line already exists), genuinely built nothing, and legacy.
   Consider defaulting a live run to the People tab, which populates as each person finishes.

3. **Confirm the shared row's library tabs.** `run_shared_rows.breakdown` has 2 entries on runs 43–45
   so the tab strip should render, but a live page showed the pre-tabs copy — most likely a stale JS
   bundle. Hard-refresh first; only investigate if it still stacks.

**Why these were missed:** each was found by the owner looking at a screen, not by the suite. The
tests assert what the components do with fixtures; none of them exercise a run that is still
running, and none asserted that a trace still EXISTS after the engine changed. A test that a shared
row's trace is non-empty would have caught #1 the moment it was introduced.

## Next: rows-only, with live progress (owner design, 2026-08-13)

Replaces the "keep the People tab" decision above. The owner is right and that call was wrong: inside
a run the row is the axis, and the People tab is actively misleading on a shared run — run 46 was a
shared-row run still offering "People (46)" for a row that belongs to nobody. "Everything one person
got" is a real question, but its home is that person's own page, not the run.

Target: **Rows** and **Log**, nothing else.

* Every row in scope appears IMMEDIATELY with a state — `waiting` → `building 12 of 46` → `done` /
  `skipped` — so a run can be tracked while it happens.
* A per-person row expands to the existing people-left / picks-right panel (`UserTabs` +
  `UserPanel`), which already renders `pending` and already has the search box.
* A SHARED row expands to progress and its trace. No person list — there is nobody to choose.

**The blocker, and why "rows appear as they finish" is currently a fudge.** Mid-run the page cannot
know which rows are in scope: scope is only derivable from `rows_considered`, which is written per
user AS EACH ONE FINISHES, so before the first person completes there is nothing to draw at all.

Fix that first, server-side: **record the run's row scope at start**, exactly as `stats.expected_users`
already records the people (`api/runs.py` synthesises `pending` user rows from it). Something like
`stats.expected_rows: [{slug, title, build, audience_size}]`, written where the run computes which
specs `should_build`. Everything else is frontend and follows from it — without it, no amount of UI
work produces progress tracking.

Order: persist `expected_rows` → render rows from it with live per-row counts → delete the People tab.
