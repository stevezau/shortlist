# Full-repo review backlog

Every finding from the July 2026 pre-release review that is **not yet fixed**. Nine reviewers ran:
two on the `dev`→`master` diff, seven over the whole repo (engine core, engine clients, API layer,
services, persistence/auth/wiring, frontend, tests/docs).

Everything the reviews found that could **lose data, leak access, or state a falsehood to the owner**
has already shipped (`6f1a5da`, `029164b`, `4bc1af1`, `d2ca1b9`). What remains is below.

**How to work this file:** take one section at a time, smallest blast radius first. Run the full
suite after each (`pytest -q`, `pnpm -C web test --run`, `pnpm -C web build`, `ruff check`). Tick items
off by deleting them. Anything marked ⚠️ changes behaviour and wants its own commit + a browser check.

---

## 1. Structural refactors (no behaviour change, big readability win)

These are the ones deliberately held back from the release. Pure refactors — the test suite is the net.

### 1.1 `_run_user` is 565 lines with 10 nested closures ⚠️

`shortlist/engine/rows.py:678-1242`

Nested defs: `effective_watched_pct`, `excludes_finished`, `pool_exclusions`, `effective_freshness`,
`effective_recent_count`, `effective_sources`, `pool_key`, `pools_for`, `seeds_for`, `_deliver_locked`.
They close over eight mutable locals, two mutated in place _specifically_ so a closure sees the change
(`rows.py:940` — `watched_titles |= ...`, with a comment saying exactly that).

No reader can hold this, and it is why findings 1.2 and 3.1 stayed invisible.

**Fix:** extract a `RowPolicy` dataclass holding `ctx`, `user`, `cfg`, `specs`, the watched breakdown
and the two caches. The seven `effective_*`/`pool_*` closures become methods with no closure state.
`_run_user` becomes: resolve specs → build policy → cold-or-warm → per-row delivery loop.

### 1.2 `seeds_for` is called by closures defined before it, and bound only conditionally

`shortlist/engine/rows.py:834`, `:874-877` call it; it is `def`'d at `:919` inside the `else:` arm of
`if cold:`. On the cold path the name never binds. Safe today only because `pools_for`/`pool_key` are
unreachable when `cold` (guarded at `:1044`). Any future call outside that guard is a `NameError` on
cold-start users — the least-tested path. **Fix:** define it unconditionally, or fold into 1.1.

### 1.3 `_deliver_one`: 220 lines, 12 parameters, four write strategies

`shortlist/engine/delivery.py:663-883`

**Fix:** extract `_find_this_rows_collection(plex, section, owned, title, marker, delivered_key,
sole_row) -> Collection | None` (lines 715-749) — self-contained, the subtlest part, and independently
testable. That alone halves the function.

### 1.4 `rows.py` and `pipeline.py` import each other

`rows.py:18` ↔ `pipeline.py:21`. Works only via module-object imports + deferred attribute access.
All seven uses are `_pipeline._emit(...)` — a **private** function reached across a cycle.
**Fix:** move `_emit` and `EngineContext` into `shortlist/engine/context.py`; both import that.

### 1.5 `services/jobs.py` imports from `api/users.py` — layering runs backwards

`shortlist/server/services/jobs.py:676` → `from shortlist.server.api.users import sync_users_from_state`.
The only import of `api/` from outside `api/`. `sync_users_from_state` (`users.py:612-797`) is ~190
lines of roster reconciliation with no HTTP in it, plus `_sync_owner`, `_display_names_drifted`,
`_rename_after_nickname`, `_hide_existing_rows_from_new_accounts`.
**Fix:** move all five to `services/user_sync.py`. `api/users.py:607` becomes a 3-line handler.

### 1.6 `RunService` is three services in one 1130-line file

`shortlist/server/services/run_service.py` — its docstring (`:6`) claims "only orchestration".

1. **Run-log buffering** `:126-224` + `_LOG_FLUSH_EVERY` and four parallel dicts keyed by `run_id`.
2. **Watch-cache orchestration** `:256-490` + `:652-703` (~230 lines) — belongs beside `watch_cache.py`.
3. **Persistence + audit** `:720-1130` (~410 lines).

**Fix, in order:** extract `RunLog` first (self-contained), then `watch_sync.py`, then
`run_persistence.py`. ~250 lines of actual orchestration remain, matching the docstring.

### 1.7 `report.effectiveness` — a ~370-line handler running ~30 queries on the event loop ⚠️

`shortlist/server/api/report.py:177-550`. Eleven nested closures. `async def` with synchronous
SQLAlchemy, so a `GROUP BY user_id, tmdb_id, media_type` over the whole picks table stalls SSE,
`/api/system/health` and every other request for its duration.
**Fix:** (a) move the computation to `services/report_service.py`; (b) make the handler `def` so
Starlette threadpools it — `api/requests.py` already does this and is the only router that does.
Same treatment for `api/users.py:135` `list_users`, which issues four queries per user in a loop.

### 1.8 `update_collection` — a ~200-line handler carrying the row-change → Plex-work decision table

`shortlist/server/api/collections.py:383-579`. Eleven mutable flags, a merged-field re-validation block
duplicating `_validate` (`:447-466`), eight conditional dispatches.
**Fix:** `RowChange` dataclass + `plan_row_changes(before, after) -> list[Job]` in
`services/collection_reconcile.py`. Handler becomes validate → apply → plan → enqueue → drain, and the
eight branches become unit-testable without a Plex context.

### 1.9 Frontend files past the size a reviewer can hold

| File                                             | Lines | Components |
| ------------------------------------------------ | ----- | ---------- |
| `web/src/pages/run-user-trace.tsx`               | 1516  | 40         |
| `web/src/pages/run-detail.tsx`                   | 1108  | 14         |
| `web/src/pages/jobs.tsx`                         | 913   | 10         |
| `web/src/components/rows/row-editor.tsx`         | 882   | 5          |
| `web/src/components/dashboard/impact-report.tsx` | 703   | 10         |

Extractions, highest payoff first:

- `run-user-trace.tsx:224-342` — `buildLibraries` is 120 lines of pure trace→view-model transform →
  `lib/trace.ts` + unit tests. Take the pure label helpers with it (`:343-381`, `:1308-1508`).
  `useScrollSpy` (`:665`) is a generic hook in a page → beside `lib/use-hash-scroll.ts`.
- `row-editor.tsx:44-372` — the whole placement subsystem (330 self-contained lines) →
  `components/rows/placement-toggles.tsx`, pure string logic → `lib/placement.ts` (where
  `placementLabel` already lives).
- `run-detail.tsx` — `UserRow`+`UserTabs`+`GroupLabel` (`:520-725`) → `components/runs/user-tabs.tsx`;
  `PickLine`+`LibraryPicks`+`RowSection`+`ResultsLegend`+`UserPanel` (`:112-516`) →
  `components/runs/user-panel.tsx`; `RunStatTiles` (`:221-309`) → its own file; formatters
  (`:87-183`, `:735-745`) → `lib/run-format.ts`.
- `jobs.tsx:172-346` — `BackupPanel` is a 175-line feature → `components/jobs/backup-panel.tsx`.
  `CronPicker` (`:44-75`) → beside `components/cron-input.tsx`.

### 1.10 A 127-line IIFE inside JSX

`web/src/pages/run-detail.tsx:960-1087` — `{(() => { ... })()}` containing a Map, a sort and a
two-column layout. **Fix:** extract `<RunUsersTab run selectedSlug onSelect idBySlug />`.

---

## 2. Correctness and robustness

### 2.1 Two live `version_check` modules that disagree about this build ⚠️

`shortlist/server/version_check.py` (68 lines, tested) vs `shortlist/server/services/version_check.py`
(108 lines, untested). Both hit the GitHub releases API, both cache independently, both implement their
own comparison. `notifications.py:21` imports the first; `api/system.py:33` the second.

Verified to disagree on a pre-release, and `shortlist/__init__.py:3` is `0.1.0b8`:

```
running=0.1.0b5    released=0.1.0   bell=False  system=True   <-- DISAGREE
running=0.2.0.dev0 released=0.2.0   bell=False  system=True   <-- DISAGREE
```

So the notification bell and the About panel can state contradictory facts.
**Fix:** delete `services/version_check.py`; move `version_info()`'s extra surface (`_install_type`,
`current_version`) into the tested one; repoint `api/system.py:33`.

### 2.2 `all_public()` raises `KeyError` on a row `get()` deliberately tolerates

`shortlist/server/settings_store.py:229,231`. `get()` (`:204-206`) guards this exact case with a
comment explaining why; its sibling does `row.value["v"]` unguarded → 500 on the whole Settings page,
recoverable only by hand-editing SQLite. **Fix:** one private `_unwrap(row)` used by both.

### 2.3 `SettingsStore(session)` with no `SecretBox` stores plaintext

`shortlist/server/settings_store.py:192-220` — both `get` and `set` short-circuit crypto on
`and self._secrets`. No live bug (no-box callers only touch non-secret keys), but
`SettingsStore(session).set("plex.token", …)` would write the owner's token in the clear.
**Fix:** make `secret_box` required, or raise when `key in SECRET_KEYS and self._secrets is None`.

### 2.4 ORM says NOT NULL; migrations 0049/0050 created eleven columns nullable

`alembic/versions/0049_run_log_lines.py:33-38`, `0050_watch_cache.py:38-44,62`.
`compare_metadata` reports exactly eleven `modify_nullable` diffs and nothing else:
`run_log_lines.{ts,user_slug,stage,counts,reason,level}`,
`watched_titles.{title,watch_count,viewed_at,updated_at}`, `watch_sync_state.item_count`.
Tests use `Base.metadata.create_all`, so the **test DB is stricter than production** — a NULL
production accepts is unreachable in tests.
**Fix:** migration `0053` batch-altering the eleven to `nullable=False`, plus a `compare_metadata`
test asserting zero diffs so the next hand-written migration can't drift.

### 2.5 Every boot takes a "pre-migration" backup, so a crash loop rotates the real ones away

`shortlist/server/db/session.py:99-101`; rotation keeps 10 (`backup.py:58-63`). Ten restarts evict
all ten scheduled backups and replace them with copies of the already-broken state.
**Fix:** compare the stamped revision to head first (the `alembic_version` read already exists in
`_heal_squashed_revision`); optionally exclude `*_pre-migration.db` from the rotation budget.

### 2.6 Retention pruning runs inside the run-persist transaction

`shortlist/server/services/run_service.py:763-770`. Bulk deletes across `runs`, `run_users`,
`run_log_lines`, `picks`, `events` share the transaction that persists the run — any failure discards
the whole persist for a run that already wrote to Plex. Also `int(store.get("runs.retention"))` at
`:764` lacks the `or 0` guard its sibling has at `:768` (`TypeError` inside that transaction).
**Fix:** make it a `maintenance.prune` job kind (`writes_plex=False`) on the nightly schedule.

### 2.7 `MediaType.TV` does not exist — a Sonarr v3 fallback that can never work

`shortlist/server/api/requests.py:254` — `MediaType` is a `StrEnum` of `MOVIE`/`SHOW` only. The
`AttributeError` is raised inside a broad `try` and swallowed to `logger.debug`, so on Sonarr v3 the
TMDB→TVDB fallback silently no-ops forever and every show shows a blank status.
**Fix:** `MediaType.SHOW`, and narrow the `except` to network/HTTP errors.

### 2.8 `audience_user_ids` is never validated → 500 instead of 422

`shortlist/server/api/collections.py:243-247`, reachable from POST (`:257`) and PATCH (`:491`).
`CollectionAudience.user_id` is a FK and `PRAGMA foreign_keys=ON`, so an unknown id raises
`IntegrityError` at commit — an unhandled 500 with a SQL string, where every other bad input here gets
a 422. On a _shared_ row this list decides who is excluded from the share filter.
**Fix:** resolve ids in `_set_audience` and 422 on any that don't exist.

### 2.9 The rename SSE generator blocks the event loop ~2/3 of the time

`shortlist/server/api/collections.py:683-692` — `queue.Queue.get(timeout=0.1)` is a **blocking** stdlib
call inside `async def`. While a rename walks 40 users over plex.tv, the loop is unavailable ~2/3 of
the time; every other request and the Docker healthcheck queue behind it.
**Fix:** `await loop.run_in_executor(None, q.get)`, or push into an `asyncio.Queue` via
`loop.call_soon_threadsafe`.

### 2.10 The run-log poll re-reads the entire log every tick

`shortlist/server/api/runs.py:275` → `run_service.py:207-223`. `after_seq` is applied in Python after
loading and materialising **all** rows. **Fix:** `query.filter(RunLogLine.seq > after_seq)` before
`.all()`; keep the Python filter only for the in-memory `live` branch.

### 2.11 `_request_outcomes` scans the whole request table per trace fetch

`shortlist/server/api/runs.py:139` — `session.query(RequestCandidate).all()` for a per-user page.
**Fix:** pass the trace's missing `(tmdb_id, media_type)` pairs and filter, or bound it like
`MAX_INBOX` does in `api/requests.py`.

### 2.12 Arr queue read caps at 1000 records with no short-page check

`shortlist/engine/clients/arr.py:154-167`. **Fix:** page properly, or compare against `totalRecords`
and log when short.

### 2.13 `TautulliClient.get_history` — dead code with an unbounded `while True`

`shortlist/engine/clients/tautulli.py:63`. No production caller; a Tautulli that ignores `start` loops
forever. Module docstring still calls it "the preferred watch-history source" while `history.py:1-7`
says the share-token source superseded it. **Fix:** delete the method + its test; restate the docstring
as "friendly names + connectivity probe".

### 2.14 The configurable PMS timeout doesn't reach the two heaviest raw reads

`shortlist/engine/clients/plex_pms.py:894` (`timeout=45`), `:758` (`timeout=30`) are hardcoded while
`__init__` documents at length that the run's client uses the operator's `plex.timeout_s`.
**Fix:** store the constructor's `timeout` and use it.

### 2.15 `pending_plex_tokens` has no TTL and is never cleared

`shortlist/server/main.py:123`, written at `auth.py:408`. A live Plex token stays in process memory
indefinitely, one entry per successful pre-link login. **Fix:** drop the entry once a server is linked.

### 2.16 FK cascade policy is inconsistent across children of `users`

`db/models.py:271,306,375,408` — `watched_titles`/`watch_sync_state` declare `ondelete="CASCADE"`;
`picks`/`run_users`/`restriction_snapshots` declare nothing. No live bug (users are only disabled), but
the first code that deletes a `User` gets an `IntegrityError` from three tables and a silent cascade
from two. Also `api/runs.py:96-97` relies on the DB cascade while `run_service.py:798-800` deletes
explicitly, commenting that bulk ORM delete "does not cascade" — both work; only one can be the reason.

---

## 3. Comments and docs that state something false

Every one of these has been verified against the code. In this repo a wrong explanation has already
cost more than a missing one.

### 3.1 "Blocked titles ride along with the watched exclusions"

`shortlist/engine/rows.py:446-448`. Verified: `user.blocked_seeds` is passed **only** to `derive_seeds`
(`:930`, `:1381`); `filter_candidates` never receives a blocked set. The cited "issue #5" mechanism is
not present. The product intent is seed-only (the UI says "Don't seed"), so **the code is right and the
comment is wrong** — and it is wrong in the direction that reads as a guarantee.
**Fix:** "blocked titles are dropped at seed derivation only — a blocked title may still surface if
another seed suggests it."

### 3.2 `RowSpec` docstring writes the shared label with one underscore

`shortlist/engine/models.py:241` says `shortlist_shared_<slug>`; the real value is
`SHARED_LABEL_PREFIX = "shortlist__shared_"` (`:215`), and `:210-213` explains that the **double**
underscore is what makes a shared label unreachable from any user slug — a load-bearing privacy
invariant, contradicted on the same screen.

### 3.3 `prunable_shared` also carries non-shared labels

`shortlist/engine/privacy.py:286-322`. The set also gets `excluded_from_self` — an account's own
**private** label. The removal is correct; the name says the opposite, in the one module whose rule is
"removals are the leak direction". **Fix:** rename to `prunable`, update the comment at `:328`.

### 3.4 `ranking.py:5` — "Seed provenance ADDS to a title's score, it does not multiply it"

`score()` at `:35` multiplies four factors. **Fix:** "seed provenance scales a title's score, it never
zeroes it."

### 3.5 `_cold_start_picks` says "Every library gets a share"

`shortlist/engine/rows.py:1534` — it iterates `plex.sections_by_type()`, which returns _one
representative library per media type_ (`plex_pms.py:260-268`). On a two-movie-library server the
second is never sampled. **Fix:** reword, or use `ctx.delivery_sections`.

### 3.6 `WatchSyncState` docstring points at a class that doesn't exist

`db/models.py:398-399` — "see `WatchCache` for why". No such class. The one non-obvious invariant in
the incremental design (why the cursor trails the newest `viewed_at`) is documented nowhere.

### 3.7 `sync-check` docstring says "off by default"; it is on ⚠️

`shortlist/server/scheduler.py:251-257`, and `settings_store.py:89-92` contradicts itself the same way.
On-by-default is intended (deletion is gated behind `confirmed`). **Fix:** rewrite both to "nightly at
05:45 by default; clearing the cron turns it off entirely".

### 3.8 `report.py:7` says "read-only"; `system.py:1` describes a third of its surface

`report.py` has `POST /report/sync` and `DELETE /report/deleted-rows` — the latter "the one destructive
action on the dashboard", in its own words. `system.py` is now also backups, jobs, logs, the API token
and the debug bundle.

### 3.9 Five places claim e2e runs against the built Docker image

`pyproject.toml:88`, `.claude/rules/testing.md:31`, `.claude/CLAUDE.md:29`,
`.claude/docs/shortlist-architecture.md:93,212`. It runs uvicorn **in-process** (`tests/e2e/conftest.py:63-66`),
and CI's `docker` job never runs the image — so nothing tests the container: not the PUID/PGID drop,
not the `HEALTHCHECK`, not that `web/dist` landed where the app looks.
**Fix:** correct all five strings. Optionally add ~6 lines to the `docker` job: `docker run -d` the
built image and poll `/api/system/health`.

### 3.10 Two more architecture-doc claims are false

`.claude/docs/shortlist-architecture.md:99` lists a root `Makefile` that does not exist. `:227` says
e2e waits on `test-web` for `web-dist`; `ci.yml:120-124` carries an explicit comment that this was
deliberately removed.

### 3.11 `ci.yml:169` says "both Python versions"

There is one, pinned to 3.12, and two comments in the same file explain why.

### 3.12 `models.py:296-297` claims picks is "the one table that grows without bound"

False — see the `caches` finding (now fixed) and `events`.

### 3.13 `plextv` blames throttling for a connection failure

`shortlist/engine/clients/plextv.py:256-289` — six connect failures raise
"plex.tv still throttling filter update…". Lands in `events` and the UI, sending the operator to the
wrong diagnosis on the most privacy-sensitive write path. **Fix:** track the last failure reason.

### 3.14 A stale-pick drop is reported as "already watched"

`shortlist/engine/candidates.py:543-546` — `recent_pick_ids` is the staleness guard, not the watched
set, and the trace is user-visible. **Fix:** split into `already_watched` / `recently_recommended`.

### 3.15 `is_running()` docstring vs behaviour

`run_service.py:610` says entries are added "when a run starts"; `start_run:515` adds when it is
**queued**. Conservative, but the comment doesn't describe the code.

---

## 4. Consistency

### 4.1 `system.py` is the only router that opts _into_ auth per endpoint ⚠️

`shortlist/server/api/system.py:27` — no `dependencies=` on the router; 17 individual decorators carry
it. Every sibling declares it at construction. All 18 routes are currently correct, so **not a live
vulnerability** — a structural trap on the worst router (`POST /system/uninstall`, `GET /system/debug`,
`GET /system/api-token`). One forgotten argument ships unauthenticated and nothing catches it.
**Fix:** move to the router; move `/health` to its own bare router (FastAPI has no per-route opt-out).

### 4.2 `Event.level` is written as both `"warn"` and `"warning"`

`"warn"`: `jobs.py:761,944`, `collection_reconcile.py:365`, `api/system.py:122,431,433,492`,
`api/users.py:756`. `"warning"`: `run_service.py:922,979,982`. `notifications.py:174` orders on
`{"error","warning","info"}` and the frontend types it the same, so any filter silently drops half.
**Fix:** one constant, `"warning"`, plus a read-side normalise or a migration.

### 4.3 Audit-Event writing is duplicated five ways

`run_service._add_event` (`:817`), `_persist_user_report` (`:893-909`, hand-rolls `Event(...)` fifteen
lines below `_add_event`), `collection_reconcile._write_audit` (`:195`), four hand-rolled copies in
`jobs.py` (`:757-771`, `:802-815`, `:899-907`, `:940-954`), and `jobs._finish` (`:383-389`). The `"at"`
field duplicates `Event.ts` and only some writers emit it, so "when did this happen" is answered from a
different field depending on the writer. `jobs.py:765,949` also hardcode a dead `"error": None`.
**Fix:** promote `_write_audit` to `services/audit.py`; drop `"at"` in favour of `Event.ts`.

### 4.4 Seven job handlers, five different `dry_run` idioms

`jobs.py:599-955`. Only `_user_hide` follows the documented contract ("read the effective value back
off `ctx.config.dry_run`"). `_user_cleanup` calls `force_dry_run()` itself, bypassing the chokepoint.
**Fix:** one rule everywhere — `ctx = build_context(dry_run=payload.get("dry_run", False))`, then
`dry_run = ctx.config.dry_run` for every decision, log line, audit field and detail string.

### 4.5 Audit events lie about `dry_run` on three paths

`force_dry_run()` is OR'd in _below_ the caller, but the caller audits its pre-OR value:
`jobs.py:926-951` hardcodes `"dry_run": False`; `collection_reconcile.py:365` audits its parameter;
`:344` and `jobs.py:899-907` record no `dry_run` at all. With `SHORTLIST_DRY_RUN` set, the audit trail
records a preview as a real removal.

### 4.6 `sync.check`'s detail line reports deletions that didn't happen

`jobs.py:626-629` branches on the local `dry_run`, not `ctx.config.dry_run`. Under `SHORTLIST_DRY_RUN`
with `confirmed=True` the Jobs page reads "removed N orphaned collection(s)" for collections still
there. **Fix:** `dry_run = ctx.config.dry_run` right after `build_context`, use it for both.

### 4.7 Three sources of truth for the same cron default

`settings_store.DEFAULTS` carries a literal cron for two keys and `""` for three; `scheduler.py` has
module constants **and** `DEFAULT_CRONS`. `_resolve_cron` reads the raw row and bypasses `DEFAULTS`
entirely, so those copies are decorative for the scheduler but live for `all_public()`/the UI. That
split is what let 3.7 drift undetected. **Fix:** one literal per cron; `DEFAULT_CRONS` owns it.

### 4.8 Two redaction ladders, and the weaker one guards the persisted text

`http_retry.redact` (`:29`) only matches the query-param form. Probed:

```
'X-Plex-Token: abc123'           -> unchanged
"{'X-Api-Key': 'SECRET'}"        -> unchanged
'{"accessToken": "SECRETVALUE"}' -> unchanged
```

`log_reader.scrub` (`:32-54`) covers all three — but `redact` is what guards API 502 details
(`users.py:412`, `settings.py:384`) and `events` rows. Rule 9 treats those as equally exportable.
**Fix:** move `_EXTRA_SECRETS` into `redact`; make `scrub` an alias.

### 4.9 `build_context()` builds the whole client stack for handlers needing two pieces

`context_builder.py:132-153` — every call opens a PMS connection, plex.tv, TMDB, optionally
Trakt/Exa/MDBList, a curator **and** the poster studio. Callers needing only `ctx.plex` + `ctx.config`:
`jobs._user_cleanup`, `_user_hide`, `_row_reconcile`, `_reconcile_poster_reset`,
`reconcile_row_rename_iter`. `run_service.sync_watched` builds an LLM curator for a read-only history
sync. `:152` also scans the whole `Collection` table just to decide whether to build the studio.
**Fix:** `ContextBuilder.build_plex_only()` alongside the existing `build_requests_only()`.

### 4.10 `HTTPException` call style splits by file

`collections.py` uses positional 23×; `system.py` mixes; every other router is keyword-only.

### 4.11 `user_rows.py` imports a private helper across routers

`user_rows.py:12` — `from shortlist.server.api.users import _pick_dict`. **Fix:** move `_pick_dict` and
`users.py:81` `_serialize` to `api/serializers.py`.

### 4.12 Frontend: `queryKeys` is only half a registry

`web/src/lib/queries.ts:20-42`. Twelve keys are inline literals scattered through pages — `["report"]`,
`["schedule"]`, `["libraries"]`, `["notifications"]`, `["syncs"]`, `["version"]`, `["image-provider"]`,
`["arrStatus"]`, `["owned-collections"]`, `["library-collections", key]`, `["backups"]`,
`["jobs","catalog"]`, `["run-log", runId]`. Several are invalidation targets hand-typed in three places
each. A typo silently stops a refresh. **Fix:** move them all into `queryKeys`.

### 4.13 Frontend: semantic colour tokens exist but three components hand-roll the palette

`index.css:30-33` defines `--success`/`--warning`. Hand-built instead at
`components/settings/connections-section.tsx:132`, `components/rows/poster-field.tsx:175`,
`jobs.tsx:376`, `jobs/job-row.tsx:46`, `notification-bell.tsx:119`.
(`user-avatar.tsx:6-9` is a legitimate exception — identity hues, not status.)

### 4.14 Frontend: `lib/types.ts` is 1116 hand-written lines with a TODO to generate

`package.json:17` already has `gen:api` with a snapshot fallback; `rules/frontend.md` calls
hand-written API types a violation. **Fix:** commit `openapi.snapshot.json` and close it out.

---

## 5. Dead code

- `shortlist/engine/rows.py:471` — `recent_pick_ids=set()` is the only production call site; the
  parameter, docstring and branch in `filter_candidates` (`candidates.py:517,531,543`) are kept alive
  by tests alone. `_candidate_pool`'s own docstring says "No staleness partition anymore".
- `shortlist/server/services/collection_reconcile.py:370-436` (`_reconcile_row_rename`) and `:623-645`
  (`run_row_rename`) — ~80 lines superseded by the `_from_plex` variants. No production caller. The
  only reference is `tests/integration/test_api.py:2288`, which monkeypatches `run_row_rename` and
  asserts `calls == []` — **a test that cannot fail**, titled as if it guards real behaviour.
- `shortlist/engine/candidates.py:493` `_slice_for_llm`; `arr.py:274` `SonarrClient.library_tvdb_ids`;
  `mdblist.py:104` `MdbListClient.usage` — all kept alive by tests only.
- `shortlist/server/main.py:119` — `token_urlsafe(48)[:32] or str(uuid.uuid4())`; the `uuid4` branch is
  unreachable.
- `jobs.py:765,949` — `"error": None` hardcoded into two Event payloads.
- `tests/conftest.py:105-109` — `mock_tautulli` fixture; grep returns only its own definition.
- Root `ca_profile.xml` — a bare `.xml` at the repo root, which `tests/fixtures/README.md` explicitly
  warns against (Unraid CA scans every `*.xml` and flags non-templates). Real template lives in
  `unraid-templates/`. **Fix:** move it there.
- Three stray root screenshots (`phase0-baseline-home.jpeg`, `rowarr-live-login.jpeg`,
  `rowarr-live-setup0.jpeg` — note the dead `rowarr` project name), hidden by a repo-wide `*.jpeg`
  ignore. **Fix:** delete them; scope the ignore to `/*.jpeg`.

---

## 6. Tests

### 6.1 The curator-provider matrix has zero tests ⚠️

No test ever constructs `AnthropicCurator`, `OpenAICurator`, `GoogleCurator` or
`OpenAICompatibleCurator`, and no test calls `make_curator` — all five call sites monkeypatch it
(`test_api.py:1986,2008,2030,2043`, `test_curator_model_e2e.py:28`). That leaves ~406 lines of adapter
and the whole dispatch table unexecuted, **including** the back-compat alias
`if provider in ("openai_compatible", "openai-compatible", "local", "ollama")` that migration
`0034_merge_ollama_provider_for_real.py` and `api/settings.py:143` both depend on.
`.claude/rules/testing.md:66` names this matrix as needing every cell.
**Fix:** `tests/unit/test_curator.py` — (a) table-test `make_curator` for all six accepted names +
`ValueError`; (b) per adapter, inject a fake SDK client and assert the parsed output and `last_tokens`
from a recorded response in `tests/fixtures/` (rule 11). `make_curator` itself must not stay mocked.

### 6.2 A test plants a token in an exception and never asserts it stays out of the log

`tests/integration/test_api.py:2035-2046` raises
`RuntimeError("unauthorized at http://api?X-Plex-Token=SEKRET")` and asserts only the response body.
`SEKRET` appears in no assertion, while `api/settings.py:468` carries a four-line comment explaining it
logs `type(e).__name__` and never `e` "because an LLM SDK can embed the api_key in a shape redact()
doesn't cover — rule 9". Change that line to log `e` and every test still passes.
**Fix:** capture loguru output and assert `"SEKRET" not in captured` and `"RuntimeError" in captured`.

### 6.3 Weak assertions in two pipeline tests

- `tests/unit/test_pipeline.py:1228` — asserts `create_collection.assert_called()` with a comment
  naming the synthesized title; the title is never asserted, so it passes with the wrong name, media
  type or size.
- `:1915` — asserts `promote.assert_called_once()` but not that the promoted object is `stranded`,
  which is the point of the test.

### 6.4 `collection_reconcile.py` (645 lines) has no test module of its own

Second-largest service in the repo, decides what happens to collections on a real server, reached only
incidentally through five HTTP-layer call sites. **Fix:** unit module against `MagicMock(spec=PlexClient)`,
in the style of `tests/unit/test_delivery.py` (`TestAnUnlabelledRowIsNeverLeftBehind` is the model).
`shortlist/server/notifications.py` (178 lines) has no dedicated module either.

### 6.5 Two test files large enough to hide things

`tests/integration/test_api.py` (4830 lines, 31 classes, **three** separate fixtures at `:37`, `:394`,
`:3613`) and `tests/unit/test_pipeline.py` (2907 lines). **Fix:** split `test_api.py` by router; move
the shared `client` fixture to `tests/integration/conftest.py`. Mechanical — good `/model sonnet` work.

### 6.6 `testing.md` demands a matrix that cannot be written

`.claude/rules/testing.md:67` requires a `history source: tautulli / plex` matrix; there is one
`HistorySource` implementation (`history.py:34`). `:18` advertises `mock_tautulli`, which nothing uses.
**Fix:** replace that row with the matrix that _is_ live — token acquisition path (owner admin token /
shared roster token / managed canary switch), which `test_history.py:31,41,61,74` already covers.

### 6.7 `tests/e2e/` is the only test package without `__init__.py`

Works today via rootdir-relative collection; breaks the moment two modules share a basename.

### 6.8 `.hypothesis/` isn't gitignored

Created by `test_privacy.py`'s five `@given` properties; untracked-but-not-ignored, so it shows in every
`git status` and is one `git add -A` from being committed.

---

## 7. Frontend behaviour and copy

### 7.1 "N people failed with the same problem" groups unrelated errors

`web/src/pages/run-detail.tsx:99-101,974-1004` — `errorBucket` is a pure alias of `friendlyError`,
which returns one generic string for anything unrecognised, so five unrelated failures bucket together
and the page asserts they are the same problem. **Fix:** bucket on a normalised raw error, or only
claim commonality for the three recognised classes. Delete the alias.

### 7.2 The disabled-switch explanation is never announced

`web/src/components/rows/row-editor.tsx:252-258` — `aria-describedby={`${label}-why`}` where `label`
contains spaces, so it resolves to three non-existent ids. Disabled switches aren't focusable either.
**Fix:** slugify the id; use `aria-disabled` + a no-op handler so the control stays reachable.

### 7.3 "By person" silently hides people

`web/src/components/dashboard/impact-report.tsx:266` — `active.slice(0, 10)` with no count and no
"show more", while every _idle_ person is listed behind a disclosure.

### 7.4 "Sync now" sticks on "Syncing…" for ever and can't report failure

`impact-report.tsx:54-62` — `disabled={isPending || isSuccess}` with the label keyed on `isSuccess`, so
after a successful POST it is permanently disabled and permanently reads "Syncing…" until remount. No
`isError` branch at all.

### 7.5 Backup empty-state states a schedule the picker above it can change

`web/src/pages/jobs.tsx:339-343` — "One will be created automatically tonight at 3 AM" is only the
default; the `CronPicker` ~150 lines above offers 12h/6h/4h/Custom. **Fix:** derive from `backupCron`.

### 7.6 Every SSE stage event refetches the run, including events from a different run

`run-detail.tsx:803-806` — `onRunFinished` right below correctly guards on `event.run_id === runId`;
this one doesn't, so sitting on finished run #12 while run #40 streams refetches #12 per event.

### 7.7 The run log's own fetch has no error state

`run-detail.tsx:774-778,1094-1101` — a failed `GET /runs/:id/log` is indistinguishable from "no log".
`rules/frontend.md` requires all four states.

### 7.8 Copy-to-clipboard hand-rolled four times; the most important one has no error handling

`app-shell.tsx:63-71`, `settings/api-access-card.tsx:36-43`, `pages/logs.tsx:95-103`,
`run-detail.tsx:67-76`. `api-access-card.tsx:37` does **not** catch, so the API-token copy throws an
unhandled rejection and does nothing visible on plain HTTP. **Fix:** `lib/use-copy.ts`.

### 7.9 The same "global default" field is written out four times

`row-editor.tsx:621-803` — `watched_pct`, `freshness`, `recent_count`, `max_seeds`, ~180 lines of
near-copies. **Fix:** one `<InheritableField>`; collapses to ~40.

### 7.10 Smaller frontend items

- `jobs.tsx:119-127` `SyncCronPanel` is a pure pass-through to `CronPicker` — delete.
- `jobs.tsx:184-188` `formatSize` defined inside a render body → `lib/format.ts`.
- `impact-report.tsx:67-69` `pct()` duplicates `lib/format.ts:105-108` `formatHitRate`.
- `run-detail.tsx:160-183` `tokenStepSummary`/`tokenStepInline` differ only by parentheses.
- `run-detail.tsx:888,897` `currentPhase(liveLog)` walks the whole log twice per render.
- `impact-report.tsx:209-232` vs `:348-446` — `ZeroDisclosure` and `DeletedRows` are the same widget.
- `impact-report.tsx:691` `const [window, setWindow]` shadows the global `window`.
- `impact-report.tsx:124` the whole chart is `aria-hidden`, per-bar `title`s inside it → a screen
  reader gets nothing. Add a summary line.
- `impact-report.tsx:661` list key uses the array index; `watched_at` is available.
- `users.tsx:139` vs `:143` — "Enable all" has `loading=`, "Disable all" doesn't; both just open a
  dialog, so neither should.
- `queries.ts:505-511` `useSyncWatched` guesses 4s with a `setTimeout`; the sync already emits
  `sync.finished` on the SSE bus. Invalidate on the event.

---

## 8. Smaller engine/server items

- `rows.py:1527` — `Pick(**{**f.__dict__, "rank": …})` on a frozen dataclass; `replace` is imported and
  used four times in the same file.
- `pipeline.py:1000-1003` — `allowed_to_delete` and `known` are loop-invariant but rebuilt inside a
  double loop; `known` rebuilds a lowercased set of every slug per collection.
- `rows.py:1200` — `_claimed_this_run(user_report)` called inside a dict comprehension, re-walking the
  breakdown per entry.
- `picker.py:38-41` — iterating `_SEEDLESS_REASON.items()` makes the reason depend on dict literal
  order when a candidate carries two seedless sources. State the precedence or use an explicit tuple.
- `rows.py:1322-1323,1381` — the local `resolve` wrapper is needed for the watchers loop but redundant
  for `derive_seeds`, which already prefers `item.tmdb_id`. Reads as a deliberate asymmetry.
- `pipeline.py:768` — `next(...)` with no default raises `StopIteration` if the invariant breaks; the
  sibling at `:795` correctly passes `None`.
- `rows.py:1382,1392` — the shared-row path re-inlines fallbacks that `effective_row_sources` /
  `effective_max_seeds` exist to prevent (and whose docstring says so). `effective_row_sources` also
  sorts, so identical source sets share one pool; the shared path passes unsorted.
- `models.py:206-207` — `UserProfile.label` hardcodes `f"shortlist_{self.slug}"` while ~40 other sites
  thread `config.label_prefix`, used by `pipeline.py:895` to find collections to promote. Meanwhile
  `EngineConfig.label_prefix` is **never assigned** anywhere. Pick a direction: delete the config knob,
  or make `label` take the prefix.
- `plex_pms.py:906,74` — `int(gid.removeprefix("tmdb://"))` unguarded; one malformed guid raises out of
  a whole section scan, inconsistent with the tolerance shown everywhere else in the file.
- `arr.py:357` `make_arr_client` returns `SonarrClient` for any string that isn't `"radarr"` — a typo
  silently talks to the wrong app.
- `arr.py:375` `_first_error` puts raw `response.text[:200]` into an error whose docstring promises it
  "never carries the URL or api key". Pass it through `redact()`.
- `tmdb.py:53-54` doesn't cache misses while `trakt.py:62-63` deliberately does, with a comment saying
  why. A 404'd title is re-fetched every run for every user.
- `history.py:228` — `derive_seeds` uses `id(s)` for set membership. Correct today; breaks silently
  under a later refactor.
- Timeout policy is unexplained per client (mdblist 15, exa 20, tmdb/trakt/arr/tautulli/plextv 30,
  curators 60/300). Most are defensible; none say so. One `DEFAULT_TIMEOUT_S` + per-client overrides
  with a one-line reason each.
- `services/secrets.py:17-19` — writes then chmods; world-readable in between (same fix already applied
  to `main.py:_instance_secret`).
- `backup.py:38-39` — filenames are UTC while every log line is local time, so an operator picking a
  restore point by filename is picking in a different timezone from the one the logs narrate.
- `run_service.py:146-151` — `_new_run_log` mutates `_log_seq`/`_log_buffer` outside `_log_lock` while
  `sink` and `flush_run_log` guard every access. Serialized by `self._lock` today; latent.
- `run_service.py:1031-1059` — twelve attributes assigned through a parallel tuple unpack with comments
  interleaved between value expressions. Make it `_refresh_pending(row, m)` with plain assignments.
- `run_service.py:405` — `_has_a_row_in_scope` imports `_in_audience`/`_is_muted` (underscore-private)
  from `engine/rows.py`. Right instinct, wrong mechanism: export
  `engine.rows.builds_anything_for(profile, config)`.
- `collection_reconcile.py:264-293` vs `:316-330` — `_reconcile_row_removal` and
  `_reconcile_poster_reset` duplicate a six-line body differing only in which engine function they
  call. One shared `_walk_row_collections(ctx, users, …, action)`.
- `api/collections.py:748-752` — poster upload validates size after reading the whole body. Check
  `content-length` first.
- `api/requests.py:60-62` — `RequestAction.ids` is unbounded and feeds `.in_()` in five handlers; past
  SQLite's parameter ceiling that's a 500. `Field(max_length=1000)`.

---

## Already fixed — do not redo

- `privacy.py` restore `KeyError` aborting uninstall
- `plex_pms` watched paging collapsing to one page
- missing `lastViewedAt` ending the incremental walk; out-of-order pages trusted
- `_instance_secret` accepting an empty secret (forgeable sessions)
- `build_context()` and `GET /api/report` leaking a DB connection each call
- backup label path traversal + leaked SQLite handles
- `POST /settings/curator/models` SSRF bypass
- `LIMIT -1` on four endpoints
- stale sweep requeuing claimed-but-waiting jobs (duplicate execution)
- rename retitling a different row when the old title was empty
- shared-row rename silently no-op'ing and reporting success
- the double rename (PATCH + SSE) reporting "renamed 0"
- `privacy.sync` and the scheduled drift check deleting collections
- `pool_key` / `pool_exclusions` disagreeing for movies + `unstarted_only`
- `history_depth` reset to 0 for users a scoped run skipped
- `caches` table never swept
- `Placement` "off" badged as "Home & Library"
- `_resolve_cron` crash-looping on a malformed row; invalid cron turning a Plex writer ON
- writer batch churning against `WRITER_LOCK_WAIT_S`
- the false "deleting is the only thing that removes the exclude" justification
