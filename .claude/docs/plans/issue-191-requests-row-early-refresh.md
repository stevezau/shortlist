# Issue #191 — early "Your requests" update: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Overseerr completes a request, start the existing run for that requests row, scoped to
the people with new arrivals. Batch arrivals into one run and rate-limit it.

**Architecture:** A new 5-minute scheduler job reads Overseerr's requests completed in the last 24h.
It matches each requester to a person with the requests row's own code. Whether a (request, row,
person) still needs an update is **derived from the runs table**, with no new state table: a finished
run that covered that row and person and started after the completion settles it, unless that run's
trace verdict for the title was "not on Plex yet" and fewer than 3 early runs tried. A batching gate
then calls the same `run_service.start_run` the row cron calls (`scheduler.py:201`), with trigger
`request_landed`. The run is not modified.

**Tech Stack:** FastAPI, SQLAlchemy 2 + Alembic (SQLite), APScheduler 3, httpx, React + TS.

**Spec:** `.claude/docs/issue-191-requests-row-early-refresh-design.md`

## Changes from the spec (made while planning, for the owner's notice)

- **No Plex-index check in the poll.** The run decides readiness. If it says `not_on_plex` or
  `season_not_landed`, the entry waits and rides the next batch, at most 3 tries, within 24h. This
  follows the owner's "no ad-hoc code outside how the app works" and keeps readiness in one place.
- **No "already in the row" skip.** The once-only rule and "covered by another run" make it
  unnecessary.
- **No watermark and no state table (owner decision 2026-11-22).** The runs table is the record.
  Turning the switch on may give one batched run for the last 24h's completions.
- **Switch on by default, existing requests rows included (owner decision 2026-11-22).** The
  CHANGELOG says so.
- **No "last early update" line in the editor.** The Runs list (trigger label) and the `events` rows
  carry it. That avoids a new API field.

## Global Constraints

- Default poll cron `*/5 * * * *` (key `requests.landed_poll_cron`). Default min gap 60 min (key
  `requests.landed_min_gap_min`, 15..1440).
- Settle window 10 min; max wait 30 min; lookback/give-up 24h; at most 3 runs per entry.
- New per-row column `collections.requests_early_refresh` (bool, default **true**, server_default "1"
  so existing rows get it on). It is meaningful only when `requests_row` is true.
- Run trigger string `request_landed`, which fits `runs.trigger` `String(16)`. UI label "A request landed".
- The poll never writes to Plex or Overseerr. It only reads Overseerr and calls `start_run`.
- Identity: only `UserProfile.seerr_user_id` (owner's choice) or Overseerr `plexId` ==
  `plex_account_id`. Never a name or email. Exclude `RequestSources.exclude_seerr_user_id`.
- Engine stays pure: no `shortlist/engine` import of `shortlist/server`.
- Tests: write first, run only that file. One pytest at a time. No full local suite; branch CI at
  the end. `from loguru import logger`. 120-char lines. Type hints and Google docstrings on public
  functions.
- Never `git add -A`; stage explicitly. Ask the owner before committing.

## Review Focus

1. **Burst:** 40 completions in one tick → exactly one `start_run` call, with all 40 people. Test in
   Task 5 (`test_burst_is_one_run`).
2. **Loop:** the run leaves a title out as `watched`/`hidden`/`over_size` → settled, never retried.
   Test in Task 5 (`test_left_out_title_settles`).
3. **Overseerr down or a malformed page:** the tick logs, records nothing and starts nothing. Test in
   Task 5 (`test_seerr_failure_starts_nothing`).
4. **Run already queued or running (e.g. the 2h nightly):** no run starts and entries carry over.
   Test in Task 5 (`test_never_stacks`).
5. **Person disabled, removed, or muted on the row, or row disabled or switched off:** they are
   never included in a run. Test in Task 5 (`test_ineligible_rows_or_people_excluded`).

---

### Task 1: Schema — the row switch column

**Files:**
- Create: `shortlist/server/db/alembic/versions/0116_requests_early_refresh.py` (`git fetch` first and
  number after origin/dev's newest; stamp with `python scripts/check_migration_freeze.py --write`)
- Modify: `shortlist/server/db/models.py` (`Collection`, beside `requests_tag_pattern` ~line 353)
- Test: the existing migration test file (`rg -l "0115" tests`)

**Interfaces:**
- Produces: `Collection.requests_early_refresh: Mapped[bool]` (default True, server_default "1").

- [ ] **Step 1:** Write a failing test: upgrade adds `collections.requests_early_refresh`, and an existing row
  reads 1. Downgrade removes it. Follow the 0115 test's shape.
- [ ] **Step 2:** Run that test file; FAIL. **Step 3:** Migration (idempotent `_columns` guard like 0115) +
  model. **Step 4:** Run; PASS. `python scripts/check_migration_freeze.py` OK.
- [ ] **Step 5:** Commit `feat(requests): early-refresh switch on requests rows (#191)`.

### Task 2: Row switch through API, editor service, assistant and templates

**Files:**
- Modify: `shortlist/server/services/row_editing.py` (field in the body model next to
  `requests_tag_pattern` ~:336; serialiser ~:810; the edit-field tuple ~:1308; keep it OUT of the
  schedule-rebuild set: the poll job is always registered and reads the column each tick)
- Modify: `shortlist/server/services/row_mutations.py:~162`, `shortlist/server/api/collections.py:~204`
  (`CollectionOut`), `shortlist/server/assistant/row_adapter.py:~378`,
  `shortlist/server/catalogs/templates.py` (the requests template ~:294, `"requests_early_refresh": True`; body-model default True)
- Modify: `web/src/components/rows/row-kind-settings.tsx` (a switch under the window-days field,
  shown only for requests rows), `web/src/lib/collections.ts:~63`, regenerate the template and API types
- Test: `tests/integration/test_api_collections*.py` (the file holding the requests-row create/edit
  tests: `rg -l "requests_window_days" tests/integration`), `web/src/test/row-requests-block.test.tsx`

**Interfaces:**
- Produces: API field `requests_early_refresh: bool` on create/update/read of a collection.

- [ ] **Step 1:** Failing integration test `test_requests_early_refresh_round_trips`. Create a requests
  row with `requests_early_refresh: true`; GET returns true. PUT false; GET returns false. A non-requests
  row saves it as false whatever is sent; mirror how the other requests-only fields are normalised in
  `validate_row`.
- [ ] **Step 2:** Run it; FAIL. **Step 3:** Plumb the field. **Step 4:** Run; PASS.
- [ ] **Step 5:** Web: failing test that the requests block renders a switch labelled
  "Update as soon as a request lands", with help text exactly:
  "Checks Overseerr every few minutes. When someone's request lands on Plex, their row updates without
  waiting for the schedule. Several arrivals close together go in one update, at most once an hour."
  Toggling it sets `requests_early_refresh`. Implement; run that test file;
  `web/node_modules/.bin/tsc -b` clean.
- [ ] **Step 6:** Regenerate the OpenAPI snapshot (command in `tests/unit/test_openapi_snapshot.py`
  docstring), `pnpm -C web gen:api`, and the row-templates generated file. Run
  `tests/unit/test_openapi_snapshot.py`; PASS.
- [ ] **Step 7:** Commit `feat(requests): per-row "update as soon as a request lands" switch (#191)`.

### Task 3: Overseerr read and shared requester matching

**Files:**
- Modify: `shortlist/engine/clients/seerr.py` (new method next to `requests()` ~:279)
- Modify: `shortlist/engine/requests_row.py` (factor identity out of `collect_requests` ~:168-215;
  make `_from_seerr_request` public as `seerr_request_title`)
- Test: `tests/unit/test_seerr.py`, `tests/unit/test_requests_row.py`

**Interfaces:**
- Produces: `SeerrClient.recently_completed(since: datetime) -> list[dict]`. Pages
  `/request` with `filter="completed", sort="modified"` (sorted by `updatedAt` desc,
  seerr `server/routes/request.ts:51-130`). It stops paging at the first row whose `updatedAt` is
  before `since`, returns rows newer than or equal to `since`, and raises on a malformed page
  (`_require_page`). It does not use `_paged` (that walks to the end); write a short loop reusing
  `_get` and `_PAGE_SIZE`, with a 10-page cap.
- Produces: `seerr_identity(people: list[UserProfile], read: dict[int, int | None]) -> tuple[dict[int, int | None], dict[int, int | None]]`
  returns `(seerr_plex, chosen)` exactly as `collect_requests` builds them today. `collect_requests`
  calls it, so there is one implementation.
- Produces: `seerr_request_title(r, seerr_plex, chosen, by_plex, exclude_uid) -> RequestedTitle | None`
  (renamed `_from_seerr_request`, body unchanged).

- [ ] **Step 1:** Failing tests in `test_seerr.py`, built on `tests/fixtures/overseerr_requests_page.json`
  (its rows carry `updatedAt`, status 5 = completed):
  `test_recently_completed_sends_filter_and_sort` asserts query params `filter=completed`, `sort=modified`.
  `test_recently_completed_stops_at_since` gives two pages, where page 1's last row is older than
  `since`: one request is made, and only rows `>= since` are returned.
  `test_recently_completed_rejects_malformed_page` expects it to raise.
- [ ] **Step 2:** Run `pytest tests/unit/test_seerr.py`; FAIL. **Step 3:** Implement. **Step 4:** PASS.
- [ ] **Step 5:** Failing test `test_seerr_identity_matches_collect_requests`. A chosen account beats
  `plexId`; an account two people chose maps to None; an unchosen account falls back to `plexId`.
  Then refactor `collect_requests` to call it.
- [ ] **Step 6:** Run `pytest tests/unit/test_requests_row.py`; all PASS (existing tests unchanged:
  do not edit any existing assertion).
- [ ] **Step 7:** Commit `refactor(requests): share Overseerr requester matching; read recent completions (#191)`.

### Task 4: Settings, schedule and trigger label

**Files:**
- Modify: `shortlist/server/scheduler.py` (`DEFAULT_CRONS["requests.landed_poll_cron"] = "*/5 * * * *"`;
  `REQUEST_LANDINGS_JOB_ID = "request-landings"`; `_register_request_landings`, called from
  `_register_settings_driven_jobs`)
- Modify: `shortlist/server/settings_store.py` DEFAULTS (`"requests.landed_min_gap_min": 60`),
  `shortlist/server/catalogs/settings.py` (catalog entries for both keys, modelled on
  `privacy.sync_cron`; the cron's effect is `Effect.PLEX_WRITE`, RECURRING, "Can start a requests-row
  run when a request lands."), `shortlist/server/services/settings_validation.py` (range 15..1440)
- Modify: `shortlist/server/api/schemas_runs.py:144` (add `"request_landed"`),
  `shortlist/server/db/models.py:511` comment, `web/src/lib/format.ts:~141`
  (`request_landed: "A request landed"`)
- Test: `tests/unit/test_scheduler.py`, `web/src/test/format.test.ts`, the settings catalog test
  (`rg -l "privacy.sync_cron" tests/unit`)

**Interfaces:**
- Consumes: `request_landings.tick(app) -> None` (Task 5). Until Task 5 lands, register a fire that
  imports it lazily.
- Produces: job id `request-landings`, registered with `max_instances=1` so a slow tick never overlaps
  the next. The fire runs `await request_landings.tick(app)` and catches and logs any exception.

- [ ] **Step 1:** Failing tests: `test_request_landings_job_registered_at_default_cron` (job exists,
  trigger fires at minute 0/5/10). `test_request_landings_job_follows_setting` (setting `*/15 * * * *`
  then `rebuild_schedule` → new trigger). The catalog/validation tests reject 10 and accept 60.
  The format test maps `request_landed`.
- [ ] **Step 2:** Run each file; FAIL. **Step 3:** Implement. **Step 4:** Run each file; PASS.
- [ ] **Step 5:** Commit `feat(requests): schedule and settings for early requests-row updates (#191)`.

### Task 5: The poll — find what's owed, batch, start

**Files:**
- Create: `shortlist/server/services/request_landings.py`
- Test: `tests/unit/test_request_landings.py`. Use `tests.db_helpers.disposing_engine`, real `Run`/`RunUser`/
  `Collection`/`User` rows, a fake `SeerrClient`, and a fake `run_service` whose `start_run` records calls
  and returns a run id.

**Interfaces:**
- Consumes: `SeerrClient.recently_completed` and `user_plex_ids`, plus `seerr_identity` and
  `seerr_request_title` (Task 3). Consumes `RunService.build_request_sources_only()`
  (`run_service.py:201`) for sources, roster and plex→DB id. Also
  `theme_rotation.rotating_users(session, collection)` (audience minus muted) and `scheduler.crontab_trigger`.
- Produces, in `request_landings.py`:
  - `SETTLE_MIN = 10`, `MAX_WAIT_MIN = 30`, `LOOKBACK = timedelta(hours=24)`, `MAX_EARLY_RUNS = 3`,
    `WAITING_VERDICTS = frozenset({"not_on_plex", "season_not_landed"})`.
  - `@dataclass(frozen=True) class Owed: request_id: int; tmdb_id: int; completed_at: datetime; collection_id: int; user_id: int`.
  - `landings(session, completed: list[dict], sources: RequestSources, people: list[UserProfile], db_ids: dict[int, int], read: dict[int, int | None]) -> list[Owed]`.
    For each completed request, `seerr_request_title` gives the person; skip if None. `completed_at`
    = the request's `updatedAt`. Pair it with every enabled collection where `requests_row` and
    `requests_early_refresh` are true, `rotating_users` includes the person, and a library of the
    title's media type exists. Use the same media-type check the row uses.
  - `still_owed(session, owed: list[Owed]) -> list[Owed]`, derived from runs only. A run *covers* an
    `Owed` when it started after `completed_at`, `stats.expected_rows` has the row's slug, and
    `stats.expected_users` has the person's slug. An `Owed` is settled (dropped) when any of these hold:
    - a covering run finished `ok` and its `RunUser.trace["selection"]` entry for that row has this
      `tmdb_id` with a result **not** in `WAITING_VERDICTS`, or has no entry for it;
    - a covering `request_landed` run finished `error` or `aborted`;
    - `MAX_EARLY_RUNS` covering `request_landed` runs exist.

    A covering run still `queued`/`running` drops it too, because it's in flight. Load a trace only
    when needed, once per (run, user) per tick.
  - `should_start(session, owed: list[Owed], now: datetime, min_gap: timedelta) -> bool`. False when:
    - `owed` is empty;
    - any Run is `queued`/`running`;
    - a `request_landed` run started less than `min_gap` ago;
    - the newest `completed_at` is under `SETTLE_MIN` old and the oldest is under `MAX_WAIT_MIN` old;
    - or every involved row's own cron next fires within `min_gap` (blank or invalid cron = never).
  - `async tick(app) -> None`. Return early, with no Overseerr call, unless some enabled requests row
    has the switch on and Overseerr is configured. Then:
    - `recently_completed(now - LOOKBACK)` in `asyncio.to_thread`; if it's empty, return.
    - `user_plex_ids()`, then `landings`, `still_owed`, `should_start`.
    - If it should start, one
      `start_run(trigger="request_landed", dry_run=False, user_ids=sorted(...), collection_ids=sorted(...))`,
      plus an `events` row (scope `requests.landed`, pattern of `run_persistence._add_event`) with the run
      id, people count and request count.

    Any Overseerr exception → one `logger.warning`, no run. Idle ticks write nothing.

- [ ] **Step 1:** Failing tests:
  - `test_landings_match_by_plex_id_and_chosen_account`;
  - `test_landings_skip_request_as_ambiguous_and_unmatched`;
  - `test_ineligible_rows_or_people_excluded` (switch off, row disabled, not in audience, muted, wrong media type);
  - `test_settle_window_holds_then_releases` (completed 4 min ago → False; 11 min → True);
  - `test_max_wait_beats_a_steady_trickle` (oldest 31 min, newest 1 min → True);
  - `test_burst_is_one_run` (40 people → one `start_run`, with all 40 user ids and the row id);
  - `test_min_gap` (a `request_landed` run 30 min ago → False; 61 min → True);
  - `test_never_stacks` (a queued or running run of any trigger → False);
  - `test_schedule_close_defers` (row cron fires in 20 min, gap 60 → False);
  - `test_left_out_title_settles` (covering ok run with verdict `watched`/`hidden`/`over_size` → not owed);
  - `test_not_on_plex_retries_then_stops` (verdict `not_on_plex` → still owed; after 3 early runs → not owed);
  - `test_failed_early_run_settles`;
  - `test_covered_by_nightly_settles` (scheduled run, unscoped users, verdict `in_row`);
  - `test_run_before_completion_does_not_cover`;
  - `test_seerr_failure_starts_nothing`;
  - `test_tick_noop_when_switch_off` (Overseerr never called).
- [ ] **Step 2:** Run `pytest tests/unit/test_request_landings.py`; FAIL.
- [ ] **Step 3:** Implement to the interfaces above.
- [ ] **Step 4:** Run the file; PASS. Break the never-stack check by hand and confirm
  `test_never_stacks` fails, then restore it by hand (not `git checkout`).
- [ ] **Step 5:** Commit `feat(requests): update a requests row soon after a request lands (#191)`.

### Task 6: Docs, changelog and final checks

**Files:**
- Modify: `docs/guides/requests.md` ("Which requests show up" paragraph, Your requests row section):
  add a short paragraph on the switch, its 5-minute check, batching, the one-per-hour limit, the
  "not on Plex yet" retries, and that requests made only with your own Radarr/Sonarr tags still wait
  for the schedule.
- Modify: `docs/guides/schedules.md` (the new schedule), the settings reference under
  `docs/reference/` (both keys), and `CHANGELOG.md` (Unreleased, Added).
- Then: `python scripts/build_llms_full.py`.

- [ ] **Step 1:** Write the docs. Run `pytest tests/unit/test_llms_full.py`; PASS.
- [ ] **Step 2:** `ruff check . && ruff format --check .` clean; `web/node_modules/.bin/tsc -b` clean.
- [ ] **Step 3:** Dispatch **Architecture Review** on the branch diff (migration, identity mapping,
  starts Plex-writing runs). Fix every HIGH and re-review until clean.
- [ ] **Step 4:** With the owner's OK, commit, push the branch, and run
  `gh workflow run ci.yml --ref <branch>`; the bar is green.
