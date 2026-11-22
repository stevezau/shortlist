> Note: the two settings (`requests.landed_poll_cron`, `requests.landed_min_gap_min`) and the Jobs-page kind below were replaced by a fixed 5-minute poll and a 60-minute `MIN_GAP`.

# Issue #191 — update a "Your requests" row soon after a request lands

Status: design, 2026-11-22. Approach chosen by the owner: **poll Overseerr** (not a webhook).

**Later owner decisions (2026-11-22), which override the text below:** the switch is **on by
default**, existing requests rows included. There is **no state table**: what each request still needs is
derived from the runs table. There is **no Plex check in the poll**: the run's own verdict decides. The
run itself is not modified. The plan is `.claude/docs/plans/issue-191-requests-row-early-refresh.md`.

## Intent

A person asks for a title in Overseerr. It lands on Plex, Overseerr tells them "available", and their
"Your requests" row doesn't show it until the row's next schedule (nightly 03:30 by default), up to
~24h later. To them it looks like the request was missed. The reporter
(newandreas, who also uses managed profiles mapped by hand, #181) wants the row updated soon after
Overseerr completes the request. It must keep the row private and keep dropping titles they've
already watched.

## What happens today (verified in code)

- A row runs only when its own cron fires (`shortlist/server/scheduler.py:1-4`). New rows default to
  `30 3 * * *` (`shortlist/server/catalogs/templates.py:21`). Nothing else starts a row run except the
  owner pressing Run. This is by design, not a bug, so there is nothing to reproduce.
- A requests row (`RowSpec.requests_row`, `shortlist/engine/models.py:736`) is built from the request
  ledger (`requests_row.collect_requests`, then `build_requests_picks` at `requests_row.py:505`). It needs
  no seeds, TMDB or AI (`rows.py:4176`). It still reads the person's watch history (watched filter).
  A title shows when it is in the row's Plex libraries, its season landed, it's unwatched, it landed
  within `requests_window_days`, and the person's restrictions allow it.
- A run scoped to some people and some rows already exists: `run_service.start_run(trigger, dry_run,
  user_ids, collection_ids)` (`run_service.py:387`). Scheduled runs use the same path, scoped by row.
  So privacy ordering (plex-safety rule 1), first-row early excludes, remove-on-empty gated on a
  complete ledger, and the watched filter all come with it unchanged.
- Runs are serialised by one asyncio lock (`run_service.py:101`, `:465`). A second run waits; it is
  not merged or rejected. Identical requests make two runs.
- **A run's fixed cost is large on a big server.** Production (58 accounts), run 85, 2026-11-21:
  history read 33s, library index 15s, filters 3s, promoting 14s, **shelf ordering ~3 min**, finish
  30s. So even a one-person, one-title run costs roughly 4-5 min of Plex/plex.tv activity. The nightly
  run holds the lock for 1.5-2.3h.

## Overseerr facts this rests on (read from seerr-team/seerr `develop`, 2026-11-22)

- `GET /api/v1/request?filter=completed&sort=modified&take=N` returns requests with status COMPLETED,
  newest `updatedAt` first (`server/routes/request.ts:51-130`). `filter=available` adds "media status
  AVAILABLE"; `completed` does not, so it also returns DELETED media. Shortlist must still check Plex.
- A request turns COMPLETED when its media (movie) or all of its requested seasons (show) become
  AVAILABLE or DELETED (`server/subscriber/MediaSubscriber.ts`). A show request asking for three
  seasons completes only when the third lands.
- AVAILABLE usually comes from Overseerr's Plex "recently added" scan (every 5 min by default). Its
  daily Radarr/Sonarr scans (04:00/04:30) can set AVAILABLE from the Arr's `hasFile` alone, before
  Plex has indexed the file (`server/lib/scanners/baseScanner.ts:152-160`). The row's libraries can
  also differ from what Overseerr scans. So "completed" can come before "in this person's library".
- Overseerr has a single webhook slot (`settings.notifications.agents.webhook`). That is why the
  webhook approach was rejected.

## Goals

- A request that lands appears in its requester's "Your requests" row within about an hour, usually
  much sooner, without waiting for the row's schedule.
- Same privacy, watched filtering and readiness rules as the nightly build. No second build path.
- Bounded cost however many requests land, and however fast.

## Non-goals

- Requests made only through the owner's own tag pattern in Radarr/Sonarr (no Overseerr). They keep
  updating on the row's schedule.
- A show request that lands one season at a time updates when Overseerr completes the request (all
  requested seasons). Partial landings wait for the schedule.
- No inbound webhook endpoint. The design leaves room to add a "poll now" webhook later.
- No change to how shared rows, picks rows or the nightly run behave.

## Anti-goals (a failure even if it "works")

- A run per landed title. A burst of 40 completions must not become 40 runs.
- Early updates that crowd out or delay the nightly run.
- A title attributed to the wrong person. Identity resolution must be the row's own, never a name or
  email match from Overseerr's payload.
- A loop: a title the run legitimately leaves out (watched, hidden by restrictions, past row size)
  must not trigger a run on every poll.
- Removing anyone's row because a poll failed.

## Approaches considered

- **A. Poll Overseerr (chosen).** One cheap call every few minutes. No Overseerr setup, catches up
  after a restart, exposes nothing new. Latency is the poll interval plus batching.
- **B. Overseerr webhook.** Near-instant, but uses Overseerr's only webhook slot. It needs a new
  inbound endpoint with token auth, and loses events while Shortlist restarts (Watchtower recreates at
  05:30). Its payload has no Plex account ID, so a lookup is needed anyway.
- **C. A + a webhook that only says "poll now".** Possible later, if A's latency proves too slow.
- **D. Docs only: put the requests row on an hourly cron.** No code. It costs a full-audience run
  every hour, which collides with the nightly. It's the workaround to give the reporter today.

## Design

### 1. The switch

- Per requests row: **"Update as soon as a request lands"** in the requests-row editor, next to its
  schedule. Explainer: "Checks Overseerr every few minutes. When someone's request lands on Plex,
  their row updates without waiting for the schedule. Several arrivals close together go in one
  update." **Default off** (today's behaviour), my call per the "taste is not the default" rule. The
  owner may decide default-on for newly created requests rows.
- Global settings (Schedules view, so they appear with every other schedule):
  - `requests.landed_poll_cron`, default `*/5 * * * *`, added to `DEFAULT_CRONS` in `scheduler.py`.
  - `requests.landed_min_gap_min`, default 60: at most one early update per this many minutes.
- The poll job is registered only when Overseerr is configured and at least one enabled requests row
  has the switch on. Settings changes reconfigure it like every other schedule.

### 2. Each poll tick

1. Read `GET /request?filter=completed&sort=modified&take=50`, paging until `updatedAt` is older than
   the watermark or the 24h lookback. Normally one call. Add `SeerrClient.recently_completed(since)`,
   with a fixture recorded from a real Overseerr (plex-safety rule 11).
2. On the first tick after the switch turns on, set the watermark to now. Don't backfill: turning it
   on must not trigger an update for everything completed today.
3. For each new completion, resolve the requester **with the ledger's own resolution**: an account the
   owner chose (`UserProfile.seerr_user_id`), else Overseerr `plexId` = `users.plex_account_id`. An
   Overseerr id two people claim, the **Request as** account, and unmatched accounts are skipped and
   counted. Factor the existing mapping out of `collect_requests` rather than writing a second one.
4. Pick the rows: enabled requests rows with the switch on whose audience includes the person, not
   switched off for them (`user_rows`), and with a library of the title's media type.
5. **Defer until Plex has it:** check the title is in one of those rows' library indexes (the
   tmdb→ratingKey index, cross-run cached, `pipeline.py:265`). Not there → keep it **pending** and
   check again next tick. Drop it 24h after completion; the scheduled run owns it from then.
6. Skip it if the person's current delivery of that row already holds it (e.g. `row_delivery_snapshots`
   with `ended_at` NULL; builder to confirm what `picks` resolves to).
7. What's left is **ready**: (person, row) pairs that would gain a title.

### 3. Load and batching (the burst case)

The owner asked: what if lots land at once, or Overseerr keeps completing requests all day? Each
early update costs ~4-5 min of fixed run time on a 58-account server, so the rule is **one batched run
at a time, rate-limited, and never in the nightly's way**.

- **One run for everything ready.** All ready pairs go into a single
  `start_run(trigger="request_landed", user_ids=[...], collection_ids=[...])`. The fixed cost (index,
  filters, promotion, ordering) is paid once for 1 person or 40. Per-person cost is only that person's
  history read and row write.
- **Settle, then go.** Start the run only when nothing new became ready for 10 min (the settle
  window), or when the oldest ready item has waited 30 min (max wait). Then a steady stream of
  arrivals can't hold the run off for ever, and a burst (a season pack, a batch of downloads finishing
  together) collects into one run.
- **At most one early run per `landed_min_gap_min` (default 60).** Anything that becomes ready
  meanwhile waits for the next slot and goes in that batch.
  Worst case is 24 early runs a day, if requests land non-stop. Typical is a handful, or none.
- **Never stack.** While any run is queued or running (a triggered one, the nightly, or a manual
  one), the poll still reads and records but starts nothing. Ready items carry over. This avoids
  queuing a run behind the 2h nightly.
- **Let the schedule do it when it's close.** If every row in the batch has its own scheduled run due
  within the min gap, start nothing; that run picks the titles up. A run that started after an item
  was detected and covered its (person, row) settles that item without an early run.
- **Once only.** A request triggers at most one early run after it's in Plex, whatever that run
  decides (watched, hidden, past row size, or the run failed). Record it as settled so a restart
  doesn't trigger it again. That is what stops the loop in the anti-goals.
- **Failures don't speed up.** A triggered run that fails isn't retried early. Its items are settled
  and the scheduled run owns them. Overseerr unreachable means no run that tick: log once, retry next
  tick.

### 4. The run itself

The ordinary pipeline, scoped. No new build path, so privacy ordering, first-row early excludes, the
watched filter, readiness and remove-on-empty (only on a complete ledger read) behave exactly as at
night. A triggered run can therefore also *remove* a person's row if everything in it was watched;
that is the existing rule, and it is fine.

**The run is not modified (owner decision 2026-11-22).** The trigger calls the same
`run_service.start_run` the scheduler calls (`scheduler.py:201`). There is no special case for
`request_landed` inside the pipeline, including no skipping of shelf ordering. All new code lives in
deciding *when* to start that run. If early runs prove too costly, that is a separate decision.

### 5. Visibility

- An `events` row per tick that did something (scope `requests.landed`): detected, deferred (not on
  Plex yet), batched into run N, gave up after 24h, skipped (unmatched requester). Idle ticks write
  nothing.
- Runs list shows trigger `request_landed` as "Request landed". Check every place that maps
  `Run.trigger` to a label or filter.
- The requests-row editor shows "last early update: <time>, N people" under the switch.

### 6. Safety

- Safe mode (`SHORTLIST_DRY_RUN`) forces the triggered run to dry-run, as for any run. The once-only
  record still applies, so dry runs don't loop either.
- The poll itself never writes to Plex or Overseerr. It reads Overseerr, reads the Plex index, and
  starts an ordinary run.
- **Architecture Review is required**: it maps an Overseerr requester to a person (watch history /
  identity) and starts Plex writes. Add a migration if pending/settled state gets a table.

## Settled after the builder check (calls made by Claude, owner to confirm)

- **State is a small table plus an Alembic migration.** It holds one row per (Overseerr request id,
  requests row, person), with state pending/ready/settled, completion and detection times, and the
  run id that settled it. The watermark goes in `settings`. A settings blob is too awkward for 24h of
  per-request state, and the migration needs an Architecture Review anyway (§6).
- **The unit is (request, row, person).** A request ready for row A and deferred for row B is two
  independent entries; each settles once. Key by Overseerr **request id**, never media id. Re-seeing a
  settled request (Overseerr bumps `updatedAt` for edits or DELETED media) is a no-op.
- **"Covered" by another run** means: it finished `ok`, it started after the entry was detected, and
  its row scope includes the row and its user scope includes the person. An unscoped run (`None`)
  covers everything enabled. Failed or cancelled runs cover nothing.
- **"Scheduled run due soon"** uses the row's own cron through `crontab_trigger(...).get_next_fire_time`
  in server-local time, the same parser the scheduler uses. A blank or invalid schedule is never "due".
- **Safe mode:** triggered runs are dry runs, like any run in safe mode. Entries settle the same way.
  The editor's "last early update" says "(dry run)", so nobody reads it as a write.
- **The poll reads Overseerr only, and adds no gate.** It neither reads nor sets
  `RequestLedger.complete`. The triggered run reads the full ledger (Overseerr + Arr tags + own
  patterns) as every run does, and remove-on-empty stays gated there. Factor out only the Overseerr
  account → person resolution from `collect_requests` (override, `plexId`, ambiguity, Request-as
  exclusion) and use it in both places.
- **"In Plex" asks the row's own readiness question.** That is title in the row's library index, plus
  for shows the ledger's `seasons_landed`. It's title-level, like the nightly build. The defer check
  must not be stricter or looser than `build_requests_picks`; reuse its predicate rather than copy it.
  A run that then leaves the title out settles the entry, and the scheduled run still re-evaluates
  it every night.

## Left to the builder

- Exact Overseerr paging parameters, confirmed against a recorded fixture.
- Docs: `docs/guides/requests.md` (Your requests row section), `docs/guides/schedules.md`, the
  settings reference, CHANGELOG. Then `python scripts/build_llms_full.py`.
- Tests: poll decisions (resolution, defer, give-up, already-in-row, once-only), batching (settle,
  max wait, min gap, never-stack, schedule-close), and a run started with the right scope.
