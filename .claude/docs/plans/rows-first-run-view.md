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
