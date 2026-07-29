# Runs, Jobs, and Convergence — design

Status: **phases 1-10 shipped** (2026-07-28). Owner decisions recorded inline. §7's library question is
settled: build it, no dependency. Phases 1-4 are shipped; see §8 for what remains.

This is the design for making Shortlist's background work reliable, visible and self-healing. It is
written down because it is a large change spanning the engine, the server and the UI.

---

## 1. The problem

Every bug found on 2026-07-28 has one root cause: **Shortlist applies changes imperatively to the
users in tonight's run, and nothing ever reconciles anything else.**

Confirmed on the maintainer's production server (SFLIX, 96 Shortlist collections):

| Observation                                                                 | Cause                                                                                                                                                                                                         |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 5 collections belonging to 4 shared users sat on the OWNER's Home           | Until 2026-07-27 `_promote_one` wrote `home=spec.show_home` for every user; default placement `both` made that `True`. The fix landed, but nothing revisits collections the promote loop no longer reaches.   |
| A `user.disable.cleanup` event logged 2 removals, yet the collections exist | The cleanup is fire-and-forget with no retry. (A separate defect — the event omitted `dry_run` — is fixed.)                                                                                                   |
| Managed users' rows would land on the owner's Home                          | `_promote_one` routed `MANAGED` through the owner flag. Plex's docs: `promotedToOwnHome` "applies to the server owner"; `promotedToSharedHome` "applies to all shared users, including managed users". Fixed. |

**Nothing in the codebase ever cleared a promotion flag** except immediately before deleting a
collection. Promotion was write-only.

Frozen-state cases (all leave a collection with permanently stale flags): user paused, disabled,
restricted, `paused_all`, not selected in a scoped run, run raised, run cancelled, no active rows,
a filter write failed, the collection sits in a library no row targets, promote raised part-way
through a user's collections, dry run.

### 1.1 The real-time gap

Collections are handled eagerly on mutation (disable deletes rows; audience shrink deletes the
dropped users' rows; row delete/rename reconcile). **Share filters are not — no API path writes them
at all.** `sync_user_restrictions` is only ever called from a run.

Consequences, worst first:

1. **A new account that gains access has no excludes until the next run.** Rows are promoted to
   Shared Home, so they see _everyone's_ row on their Home for up to ~24h. This is a genuine leak.
2. Removing someone from a **shared** row's audience does not hide it until the next run (a shared
   row is one collection, so there is nothing to delete).
3. Disabling a user does not hide shared rows from them until the next run.

---

## 2. Principles

1. **Reconcile, don't apply.** Desired state is a pure function of (users, rows, settings). Every
   pass compares it to observed state and converges. How the server got wrong is irrelevant.
2. **Enumerate by ownership, never by run set.** Walk every `shortlist_*`-labelled collection, not
   just tonight's users. This is what makes paused/disabled/partial/crashed stop being special cases.
3. **Asymmetric confidence.** Demoting/hiding is monotonically private — always safe, needs no gate.
   Deleting needs a complete picture; if any read failed, demote and report, never delete on a guess.
4. **Preserve the leak-safe ordering.** Retire (demote/delete) may happen any time; promotion only
   after share filters are merged. Rule 1 of `.claude/rules/plex-safety.md` is unchanged.
5. **Idempotent, no churn.** Read before writing; write only on difference. A nightly converge over
   hundreds of collections must cost reads, not writes — plex.tv is adaptively throttled.
6. **One writer.** A job and a run must never write to Plex/plex.tv concurrently.
7. **Failures find the operator.** Anything that exhausts its retries raises a notification.

---

## 3. Owner decisions (2026-07-28)

| Decision                   | Choice                                                                                         |
| -------------------------- | ---------------------------------------------------------------------------------------------- |
| Pause semantics            | **Hide** — demote all flags, keep the collection (so excludes still match); restore on unpause |
| Unattributable collections | **Delete, but only when the picture is complete**; otherwise demote + report                   |
| The 5 live stale flags     | Let the next run fix them (proves the converge pass end-to-end)                                |
| New accounts               | Write their filter **the moment we see them**, not at the next run                             |
| Other visibility changes   | **Targeted background filter writes** — only the affected accounts                             |
| Runs vs jobs               | **Separate.** Runs keep their page, SSE, per-user results, cancel                              |
| Naming                     | **Jobs** ("tasks" reads like something the user must do)                                       |
| Scope of jobs              | Everything touching Plex/plex.tv **except runs**                                               |
| Interactive feel           | Enqueue, then wait briefly — fast jobs still return their result inline                        |
| Container dies mid-run     | Mark the run **interrupted** on boot, then **queue a fresh run**                               |

---

## 4. Runs — what changes

Runs keep their identity. Four changes:

1. **`_promote_phase` returns the ratingKeys it promoted** so converge knows what it need not revisit.
   _(done)_
2. **`_converge_phase` runs after promotion** — walks every library, clears `promotedToOwnHome` on
   any Shortlist collection promote did not reach. Narrow on purpose: own-home only, clear-only, so
   it is monotonically private and needs no `filters_ok` gate. _(done)_
3. **Boot recovery — half of this already exists.** `create_app` already aborts orphaned
   `queued`/`running` runs at startup and stamps `finished_at` (`main.py:176`). It had no test; one
   now pins it. What is still missing is the second half: **queue a fresh run** after the reap, so a
   crashed nightly does not silently wait until tomorrow.
4. **Retire phase widens** to cover pause-hiding and confident orphan deletion (§6).

### Why runs need no mid-run resume

Delivery is unpromoted; filters merge; only then promotion. A run killed at any point leaves rows
visible to nobody. Correctness never required resume — only LLM cost would benefit, and the owner
chose a fresh run over that complexity.

---

## 5. Jobs — the new system

A job is a short, mechanical, durable unit of maintenance. Durability is the point: before this,
every maintenance action was a fire-and-forget executor call with no record, no retry, and nowhere
an operator would see it fail.

**Lifecycle:** `queued → running → done | failed`. `running` at boot means the process died
mid-job; startup requeues those. Every job kind must therefore be **idempotent**.

**Retries:** `attempts` / `max_attempts` (default 3) with backoff. Exhausted → `failed` → notification.

**Serialization:** a single worker. Plex-writing jobs additionally wait while a run is active (§2.6).

### 5.1 Job catalogue

| Kind            | Trigger                                                  | Does                                                             |
| --------------- | -------------------------------------------------------- | ---------------------------------------------------------------- |
| `user.cleanup`  | user disabled                                            | delete their collections (demote-then-delete)                    |
| `user.hide`     | user paused                                              | demote all flags, keep the collection                            |
| `user.restore`  | user unpaused                                            | re-promote to the row's placement                                |
| `filters.apply` | user enabled/disabled, audience change, new account seen | write the affected accounts' share filters only                  |
| `row.reconcile` | row deleted/disabled/renamed/audience shrunk             | existing reconcile, now durable + retried                        |
| `sync.check`    | Tools button, and post-run                               | the converge pass on demand                                      |
| `sync.users`    | Tools button, schedule                                   | existing user sync; enqueues `filters.apply` for any new account |
| `sync.history`  | Tools button                                             | existing                                                         |
| `backup.create` | Tools button, schedule                                   | existing                                                         |

---

## 6. The visibility matrix

Every case, and where it is handled. **E** = eagerly (job, seconds). **R** = reconciled (next run).

| Case                                | Their own row                                                   | Others' rows hidden from them                               |
| ----------------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------- |
| Active user                         | built each run                                                  | E `filters.apply` + R                                       |
| **Paused**                          | E `user.hide`; restored on unpause                              | unchanged — excludes still match, collection kept           |
| **Disabled**                        | E `user.cleanup` (retried)                                      | E `filters.apply` with `hide_all_shared`                    |
| **Removed from Plex**               | R — demote always; delete only when the roster read succeeded   | n/a (no share)                                              |
| **New account**                     | next run builds it                                              | **E `filters.apply` immediately** — closes the leak in §1.1 |
| **Not selected in a scoped run**    | untouched                                                       | R — excludes derive from server state, already correct      |
| **Run errored / cancelled**         | delivered unpromoted (safe)                                     | R                                                           |
| **Managed user**                    | none today (skipped) — see issue #20                            | ⚠️ **no excludes at all** — unresolved, see §9              |
| **Per-person row, audience shrunk** | E delete their collection                                       | R                                                           |
| **Shared row, audience shrunk**     | one collection, nothing to delete                               | **E `filters.apply`** for the dropped accounts              |
| **Row disabled**                    | E `row.reconcile` (per-person today; shared rows are a gap, F5) | R — union-only, stays excluded                              |
| **Owner**                           | own row only                                                    | ⚠️ **structurally impossible** — no share with yourself     |

---

## 7. Implementation

**Library choice: none — build it (~200 lines).** Researched 2026-07-28.

- Celery / RQ / arq / dramatiq / procrastinate all require Redis or Postgres. A broker dependency is
  a deployment regression for a single-container self-hosted app. Ruled out.
- **Huey** (`SqliteHuey`) is the only brokerless contender and it fails on the one axis that matters:
  its own docs state it "does not guarantee at-least-once delivery... does not do acknowledgement of
  completed tasks" — a task popped when the process dies is **lost**. Durability across a crash is
  the entire reason for this work. It also needs a second OS process (`huey_consumer.py`) plus a new
  `peewee` dependency.
- **APScheduler stays exactly as it is**: a pure in-memory trigger, rebuilt from the DB each boot.
  `scheduler.py`'s docstring already says it — _"the runs table is the durable queue."_

So `jobs` extends the pattern `runs`/`run_users` already proves. Claim a row inside
`BEGIN IMMEDIATE` (SQLite has no `FOR UPDATE SKIP LOCKED`), and have an APScheduler-fired sweep
requeue anything stuck in `running` past a timeout — that is the survives-a-crash story.

**UX: Tools triggers, Runs shows history.** Sonarr/Radarr — the convention this audience already
knows — split _System > Tasks_ (trigger + brief recent status) from _Activity > History_ (the log).
Only Jellyfin merges them. So Tools keeps its buttons and the existing Runs page grows to list
maintenance jobs alongside engine runs. **Do not rename Tools to Jobs** — it would conflate the
trigger with the log, against every convention in this niche.

**Data model** (`jobs` table, migration 0043 — written):
`id, kind, payload JSON, status, attempts, max_attempts, detail, error, result JSON,
created_at, started_at, finished_at`.

Payload is data, never a closure, so a job survives the process that queued it.

---

## 8. Phasing

1. ✅ Placement grid + `off` state + per-collection Recommended + managed-user routing fix
2. ✅ `_converge_phase` (own-home, clear-only) + `promote()` no longer defaults `home=True`
3. ✅ `dry_run` recorded in the disable-cleanup audit
4. ✅ Jobs table + worker + boot recovery + notification on failure (`services/jobs.py`;
   APScheduler drains every 10s and sweeps abandoned jobs every 5m; `BEGIN IMMEDIATE` claim)
5. ✅ Migrating background work onto jobs — `user.cleanup`, `user.hide`, `user.restore`,
   `row.reconcile` (every automatic reconcile: delete, build flip, audience shrink, row disabled,
   library narrowing), `sync.users`, `sync.history`, `backup.take`, plus `sync.check` /
   `privacy.sync`. Each is enqueued then drained inline, so it still feels instant but is retried and
   survives a restart. APScheduler is now purely the TRIGGER — `_queue_and_drain` is the only thing
   its cron jobs do.
6. ✅ Eager filter writes — every path that owes one goes through `jobs.queue_privacy_sync`: new
   accounts, disable/re-enable, a shared row's audience changing either way, a build flip, a shared
   row deleted, an account demoted out of `owner`, and the `hide_shared_from_disabled` toggle.
7. ✅ Pause = hide; unpause = restore (converge takes a paused user's rows off EVERY surface,
   keeping the collection + label so excludes still match and unpausing is a re-promote; `user.hide`
   job fires the moment someone is paused)
8. ✅ Confident orphan deletion in the retire phase (`may_delete_orphans`, gated on a non-empty
   roster — an incomplete picture demotes rather than deletes)
9. ✅ UI: Tools triggers (preview then fix), Runs shows history with a real empty state; job types + status (shape pending research on Sonarr/Jellyfin conventions)
10. ✅ Run boot recovery: aborts orphaned runs AND queues a `privacy.sync` so a crash does not wait
    for the next schedule. Deliberately not a full rebuild — a crash-loop would re-curate the whole
    server repeatedly.

---

## 9. Open items

- **Issue #20 / managed users.** `restricted=1` means "is a Plex Home sub-account", _not_ "has a
  restriction profile". Forum evidence says an unrestricted managed user sees collections normally.
  Shortlist skips their filter write entirely, justified by a comment claiming plex.tv returns 422 —
  but the code skips _before attempting_, so that claim has never been tested. Needs a live probe.
- **3 collections read `promotedToOwnHome=1` after being written `home=False`.** Candidates: they
  were never in the 92 (the per-user try wraps the whole section loop); the PMS coerces own-home
  from a `homeVisibility` enum; or the post-promote shelf `move` rewrites the hub. Testable by
  running once with `rows.manage_shelf_order=false`.
- **Owner sees every row in the Collections tab.** Structural, unfixable, must be documented.
- **`_promote_phase` read the RAW `config.rows`, not the effective specs** (fixed 2026-07-28). Every
  other phase uses `per_person_rows()`, which synthesizes the legacy default spec when no rows are
  configured. Promotion did not, so an unmanaged-rows config had an EMPTY title->spec map: every
  lookup missed, every collection fell to the no-spec fallback, and per-row placement was silently
  ignored for the whole server. Not reachable through `ContextBuilder` (it always passes
  `rows_defined=True`), but it made the fake-backed suite promote almost entirely through the
  fallback — which is how it came to look like the fallback was the common path. It is not: the
  title map matches 16/16 whenever rows are populated. **Do not "fix" the title lookup; it works.**
- **The fake-backed suite still under-tests the real branch.** 19 of 26 `_promote_phase` calls in
  `test_engine_vs_fake.py` run with `config.rows == []`, so most promote assertions — including the
  owner/shared Home-flag matrix — validate the fallback rather than the spec-carrying path a server
  always takes. A regression in placement decoding would be invisible to them.
- **F5–F10** from the audit: disabled _shared_ rows never retired; promote only walks targeted
  libraries; self-exclusion never healed; restricted accounts have no fixture; placement lost when a
  title cannot be mapped; roster snapshot staleness.


---

## 10. Architecture review, 2026-07-28 — what it caught

The first full-diff review returned **four HIGH findings**, all real, all in code written that same
day. Recorded here because each is a trap the next person could fall into the same way.

**H1 — converge demoted every SHARED row.** The "is this legitimately on the owner's Home?" test was
`label == shortlist_<owner_slug>`. A shared row is labelled `shortlist__shared_<rowslug>` and belongs
on the owner's Home whenever its placement says so, so converge stripped it on every pass that had
not just rebuilt it — a no-user run, a scoped cron run, a cancelled run, a sync check. Fix: an
`allowed` SET (owner label ∪ configured shared rows wanting Home), not a single label.
*Lesson: "whose row is this?" has two answers in this codebase, per-person and shared. Any check on
one must consider the other.*

**H2 — `is_running()` returned False during a cancelled run's merge+promote.** Cancellation is
cooperative: the engine stops taking new users, then still merges filters and promotes everyone
delivered. Treating "cancel requested" as "finished" opened the job queue inside the exact window
rule 1 protects. Fix: `bool(self._cancels)`.
*Lesson: "cancelled" is not "stopped" here.*

**H3 — the new-account filter write bypassed `RunService._lock`.** It called `engine_run` inline from
an HTTP handler that the scheduler also fires, so two full passes could merge the same accounts'
filters from independent roster snapshots — a lost update drops an exclude. Fix: enqueue
`privacy.sync` and drain, which inherits the busy check and retries.
*Lesson: anything doing a full engine pass goes through the queue, never straight from a handler.*

**H4 — disable cleanup lost its `events` audit row** when it moved to the queue; the code that wrote
it became unreachable. Deleting someone's collections is a destructive Plex write (rule 10). Fix: the
handler writes the Event itself.
*Lesson: moving work behind an abstraction can silently drop an audit obligation.*

MEDs fixed alongside: dry-run converge reported `0` regardless of state (so the Tools preview was
useless); `run_pending` had three callers and no mutual exclusion; boot recovery waited out
`STALE_AFTER` before requeuing anything; the no-spec promote fallback still defaulted
`recommended=True`, which could force an `off` row onto the Recommended shelf; and
`POST /api/system/sync-check` duplicated the `sync.check` job without its safety checks (deleted).

**The gate earned its keep.** Every one of these was in code that passed a green suite. The review
runs before the `dev` push, not at PR time, because a `dev` push is already live for the maintainer
and every `:dev` user.


---

## 11. Not built — the handover list

Everything above ships and runs on the maintainer's server. These do not, and are described here
precisely enough to pick up cold.

### A. Settings changes do not reconfigure anything

**The gap.** Changing a setting writes it to the DB and nothing else happens until the next run.
That is fine for most settings, but not for the ones that change what Plex should look like:

| Setting | What should happen on change |
| --- | --- |
| `privacy.hide_shared_from_disabled` | every disabled account's filter needs rewriting → `privacy.sync` |
| `rows.manage_shelf_order` / `rows.hub_anchor` | shelf order should be re-applied, or left alone if switched off |
| `row.name_template` | existing collections carry a title no future run will write (the rename reconcile exists for the per-user nickname case; the global template has no equivalent) |
| `label_prefix` (if ever exposed) | every label and every exclude in every filter changes — a migration, not a job |

**The shape.** A settings PATCH compares old vs new, maps changed keys to job kinds, and enqueues
them — the same enqueue-then-drain the disable path uses, so it still feels instant. `jobs.KINDS`
stays an allow-list; these are enqueued server-side, never from the generic button.

**The part that matters most:** a job that exhausts its retries already writes a `job.failed` Event.
`notifications.py` does NOT yet read that table, so a failed reconfigure is silent in the UI. Add a
`_failed_jobs` builder alongside `_recent_service_errors` and the bell surfaces it. Without that, the
retry machinery is invisible exactly when it matters.

### B. Issue #20 — managed users get no share filter

`sync_user_restrictions` returns early for `remote.restricted`, so those accounts carry no
`label!=` excludes at all. The justification (plex.tv answers 422) has never been tested, because the
code skips before attempting.

`tests/fixtures/plextv_restricted_user.json` changes the picture: a restricted account **already
carries** `filterMovies`/`filterTelevision` (a `contentRating=` parental filter), and
`merge_label_excludes` byte-preserves it while adding a `label!=` condition — proven by test. So
there is somewhere to write and a safe way to write it.

**Next step:** remove the early return and let it try. `FilterWriteRefused` handling already exists
and treats a 422 on a restricted account as safe-to-skip, so the failure path is covered. Verify
against a managed account with **no** rating profile — that is the case the issue's reporters
describe, and the one the 2026-07-25 "sees zero collections" note did not cover.

### C. F10 — filters merge against a roster snapshot read once per run

`_privacy_sync_phase` reads the whole roster once, then merges each account against that snapshot.
On a 48-account server the write loop takes minutes, so a filter edited in Plex Web meanwhile is
clobbered. Still a merge (rule 3), just against a stale read. Fix is a re-read immediately before
each write, at the cost of one extra plex.tv call per account.

### D. Jobs page UX

The page lists kind / status / detail / when, with an empty state. Not built: filtering, a detail
view (payload, per-attempt errors, timings), grouping, or a live-progress indicator for a running
job. `Job.result` already holds structured output that nothing renders.

---

## 12. Mutation audit, 2026-07-28 — every state change, and whether it reaches Plex

A full walk of every state change reachable from the API/UI, asking: does the necessary Plex or
plex.tv action actually happen, and when? Ranked by EXPOSURE first.

**Status 2026-07-29: the CRITICAL and all thirteen HIGH findings are fixed.** What each was, and what
closed it, is below — kept rather than deleted because the *shapes* recur, and because several of the
fixes are load-bearing in ways the code alone doesn't explain.

### The three root causes, and what replaced them

1. **Reconcile addressing was wrong.** Per-person collections were addressed by "the title the latest
   completed run recorded" (`_delivered_titles_by_user`). Rows have their own crons, so the latest run
   is routinely scoped to ONE row — delete row B the morning after row A ran and nothing was removed,
   audited as "removed 0". `DELETE /api/runs` emptied the record outright while claiming to change
   nothing on Plex.

   Now every reconcile addresses collections **from Plex, by label + the title the row's own template
   renders to** (`_rendered_titles`), unioned with the recorded titles as a fallback. The union is
   deliberate: rendering covers every static / `{library_name}` / `{user}` template regardless of run
   history, and the recorded title covers `{top_seed}`, which renders differently every run and so
   cannot be predicted. The template is read from the DB (`row_template`) so every door gets it; the
   DELETE path carries it in the job payload instead, because the row is gone by the time a retry runs.

   A rename of the PERSON rather than the row (nickname edit, Tautulli rename) needs the *previous*
   display name to find the collection — rendering `{user}` from the new name on both sides matches
   nothing. `old_display_names` carries it.

2. **The job catalogue was half-wired.** `row.reconcile` existed with zero callers; `user.restore` did
   not exist. Every reconcile was a bare `run_in_executor`: no retry, no `Job` row, no `_plex_busy()`
   check, so a Plex outage lost the work permanently and a row delete could write mid-delivery.

   Now the automatic reconciles (delete, build flip, audience shrink, row disabled, library narrowing)
   are queued as `row.reconcile` and drained inline, and `user.restore` mirrors `user.hide`. The one
   still inline is the interactive **cleanup** button, which has to return what it removed.

3. **Settings PATCH was inert.** It stored values and did nothing. It now compares before/after for the
   two settings that change Plex — `row.name_template` and `privacy.hide_shared_from_disabled` — and
   fires the rename or the filter pass.

### The one entry point for owed filter writes

`jobs.queue_privacy_sync(state, reason)` — enqueue `privacy.sync` and drain. `privacy.sync` is
`engine_run(ctx, [])`: it merges every account's filter and creates, promotes and delivers **nothing**,
so it can only ever make the server more private (rule 1). That is what makes it safe to fire straight
from a mutation handler, and why a targeted `filters.apply` was not built — it would be a second,
less-tested implementation of the same merge.

Callers: a shared row's audience changing in either direction (C1), a build flip, a shared row deleted,
someone turned off (H5/H6) or back on, un-paused, an account demoted out of `owner` (H12), and the
`hide_shared_from_disabled` toggle (H4).

### Ordering — and why the queue is NOT the mechanism

Rule 1 says a row must never be visible before the exclusion that hides it exists. Outside a run the
only job that makes something more visible is `user.restore`, so that is the one place the ordering
has to be enforced.

**It is enforced inside the handler, in straight-line code**: `_user_restore` calls `engine_run(ctx,
[])` (merge every filter, build nothing) and only then `promote_user_rows`. If the merge raises, the
handler raises and the whole job is retried — nothing is promoted.

The first attempt at this was two queued jobs, `privacy.sync` then `user.restore`, relying on the queue
being FIFO. **It is not, once a retry is involved.** `_claim` steps over any job whose backoff has not
elapsed and takes the next one, so a filter pass that failed against a 503 plex.tv is skipped and the
promotion behind it lands anyway against a healthy PMS — the single most likely partial outage for this
app, and the exact leak the ordering exists to prevent. Caught in architecture review, 2026-07-29.

The rule for anything added later: **if job B must not run before job A, they are one job.** The queue
guarantees durability and retry, never sequence.

Disable is the opposite case and safe either way — every `user.cleanup` is enqueued before the single
`privacy.sync`, so the filters are computed from what is left on the server, but both directions only
ever remove visibility. One pass for a bulk disable, not one per user.

### Destructive sweeps need a floor check

`sync_users` switches off anyone missing from the plex.tv roster (H13) and queues deletion of their
collections — unattended, on the daily sync. It is bounded twice:

- **Empty roster → do nothing.** A 200 with an empty container is a real plex.tv response, and "nobody
  is on the roster" would have disabled **every user on the server**. `PlexTvClient.list_users` also
  skips non-`<User>` elements now, so a response-shape change cannot manufacture the same emptiness out
  of a non-empty body (rule 11).
- **Half or more departing at once → refuse and report.** A *truncated* read is as indistinguishable
  from a mass departure as an empty one. One person leaving is routine and still acts immediately; half
  of them at once is a partial read, so it writes a `user.departed.refused` Event instead of acting.

Both mirror `_converge_phase`, which gates orphan DELETION on `bool(known)` for the same reason. Caught
in review, 2026-07-29 — the happy-path test passed throughout.

### Safe mode is per call site, not per client

`SHORTLIST_DRY_RUN=1` makes `build_context` OR the flag into `ctx.config.dry_run` — but neither
`PlexClient.promote` nor `demote_all` has a dry-run branch of its own, so **the guard has to be where
the call is made**. `_promote_phase` had one; the `promote_user_rows` extraction did not, which stayed
invisible until `user.restore` became a second caller. Under safe mode an un-pause then previewed the
hiding (`engine_run` honours the flag) and performed the showing — preview the private half, perform
the public half, on the one job that increases visibility.

The guard now lives inside `promote_user_rows` and `_user_hide`, so every caller inherits it. **Anything
added later that calls a `PlexClient` write method directly must check `ctx.config.dry_run` itself**
(rule 8). Caught in review, 2026-07-29.

### What each finding was

| # | Mutation | Was | Now |
| --- | --- | --- | --- |
| C1 | Someone removed from a SHARED row's audience | Gated on `build == "per_person"`, never true for a shared row. A shared row is ONE collection, so deletion cannot hide it — only a filter write can, and none was enqueued. **The dropped accounts kept seeing it.** | Both directions queue `privacy.sync` |
| H1 | Any per-person reconcile | Addressed by the latest run's breakdown — see root cause 1 | Addressed from Plex by label + rendered title |
| H2 | `DELETE /api/runs` | Silently disarmed every reconcile | Reconciles no longer read run history as their primary source |
| H3 | Row `enabled: true→false` | Nothing fired; the next run removed it only for users it processed, and a row with no schedule has no next run | Queues `row.reconcile` |
| H4 | `privacy.hide_shared_from_disabled` toggled | Settings PATCH inert | Queues `privacy.sync` on a real change |
| H5/H6 | Disable a user (single or bulk) | Collections removed, but their OWN filter never written — so an opted-out account kept seeing public shared rows | `user.cleanup` per user, then one `privacy.sync` |
| H7 | `row.name_template` changed in **Settings** | Only the Rows page reconciled; the Settings door did not, so the next run built a SECOND collection and left the old one labelled and promoted for ever | Renames via `run_row_rename_from_plex`, keyed on the previous template |
| H8/H9 | Row `media` or `library_keys` narrowed | Collections in the dropped libraries were never removed, never refreshed, and re-promoted every run by promotion's no-spec fallback | `_stranded_sections` diffs old vs new `target_sections`; only the libraries it left are swept. An unreadable Plex removes NOTHING |
| H10 | Row `build` per_person→shared | Inherited H1 | Fixed with H1; also queues `privacy.sync` |
| H11 | ALL collection reconciles | Bare `run_in_executor` — no retry, no `Job` row, no `_plex_busy()` | Queued as `row.reconcile` |
| H12 | `_sync_owner` demotes a stale OWNER→SHARED | That account had NO excludes while typed owner (rule 5), and nothing fired | `_sync_owner` reports demotions; the caller queues `privacy.sync` |
| H13 | User disappears from the plex.tv roster | Stayed enabled: every run tried their dead share token, and their collection sat promoted to Shared Home with no user left to see it | The sync turns them off (keeping their history) and runs the whole tested disable path |

### Still open

- ~~`{top_seed}` restore placement~~ **CLOSED.** `promote_user_rows` takes `placement_keys`
  ({ratingKey -> row slug}) from the ledger and prefers it over any title, so an un-paused
  `{top_seed}` row gets its configured placement even with run history wiped. Verified live on SFLIX
  with `DELETE /api/runs` first: the row came back Recommended-only, as configured, not on Home.
- **MED, unchanged and by design**: placement / pin_top / mute / content settings are next-run-only —
  they change what a row IS, not who may see it, so there is nothing to write between runs.
  (Adding someone to a shared row's audience is no longer in this list: it now fires a filter pass
  like every other audience change.)
- **`POST /system/backups/restore`** still re-widens `shared_labels` when the restored copy predates
  an audience narrowing — correct for the config being restored, and now SAID: the response carries a
  `privacy_note`, the Jobs page shows it before the confirm as well as after, and a `backup.restore`
  Event records it (rule 10). What it does not do is re-narrow anything; that is the operator's call,
  which is why the warning names Rows as the place to check.
- **LOW — all three closed**: `DELETE /collections/{id}/poster/image` now clears `mode` and reverts
  the artwork on Plex · a dead `shortlist__shared_*` exclude is pruned once the collections have been
  successfully enumerated and the label is provably absent (never on a failed read — see below) ·
  repointing `plex.url`/`plex.token` at a DIFFERENT machine is refused with an explanation rather than
  silently accepted, because every record Shortlist holds is scoped to one server.

### Corrections to this document

- §11.A said `notifications.py` doesn't read failed jobs — it does (`_failed_jobs`).
- §6's "Row disabled → E `row.reconcile`" was aspirational when written; it is true now.
- §8 phase 7's "unpause = restore" was next-run-only; `user.restore` now exists.


---

## 13. The delivery ledger (2026-07-29)

`deliveries` — `(collection_slug, user_slug, library_key) → rating_key, title, updated_at`, migration
0045. Written on the run's persist path (`run_service._record_deliveries`) from the per-(row, library)
breakdown, which the engine now stamps with the collection's ratingKey.

**Why it exists.** Every on-demand reconcile has to answer *which object on the server is this row,
for this person, in this library?* Three sources were tried, in this order:

1. **The latest run's breakdown.** Scoped to one run — and rows have their own crons, so the latest
   run is routinely a DIFFERENT row. `DELETE /api/runs` erased it while promising to change nothing
   on Plex. This was root cause 1 of the whole §12 audit.
2. **Rendering the row's name template.** Computed from config, so history cannot break it — but a
   `{top_seed}` title is different every run, so it is unrenderable by construction. `_rendered_titles`
   deliberately returns nothing for those rather than match every row.
3. **The ratingKey.** The identity Plex itself writes on. Survives renames, template changes, cleared
   history, and a title that never repeats.

All three are still used, unioned, because each covers what the others cannot: the ledger is empty for
a row delivered before 0045 or never delivered at all, and nothing backfills it — there is no source to
backfill FROM, which is the original problem.

**Scoping.** A ratingKey narrows the search; it never widens ownership. Every candidate is still found
via `find_owned_collections(section, shortlist_<userslug>)`, and `delete_owned_collection` still refuses
anything without a `shortlist_` label — so a stale key cannot reach another user's row or a Kometa
collection (rule 4).

**Lifecycle.** Rows are upserted per delivery and deleted when their collection is removed, and only
after a REAL removal — a dry run leaves the ledger intact, or the next live attempt would have nothing
to address by. Keyed by slug rather than foreign key on purpose: the row being described is usually the
one being deleted, and a cascade would take the answer with it.


---

## 14. What the second review round found (2026-07-29)

Three HIGH, all in the work of §13 and the LOW cleanups. Recorded because the *shapes* are the point.

### Removing an exclude needs evidence, not just the absence of an exception

`collections_known` was set from "`owned_collections()` did not raise". A PMS mid library-index rebuild
answers **200 with no collections** — a successful, empty read, indistinguishable from "every row is
gone". Since `wanted` is derived from that same empty enumeration, one such read stripped every
`shortlist__shared_*` exclude from every account on plex.tv, and nothing re-added them.

Tracing it showed the hole was wider than the branch under review: `stale_shared` — the pre-existing
prune, the one that un-hides a row when someone is added to its audience — has the identical
dependency. So **every** removal is now gated on `existing_lower is not None`, which means
`collections_known AND stored_labels`. `dead_shared` additionally requires the config to have stopped
declaring the row shared, so a live row cannot reach it at all.

The cost is deliberate: on a server whose PMS read fails, an account added to a shared row's audience
stays hidden from it until a read succeeds. Fail-safe, and it self-heals on the next run.

**The rule:** an empty result is not evidence of absence. This is the third time that shape has
appeared in two days — the plex.tv roster sweep (§12), converge's orphan deletion, and now the prune.
Anything that DELETES or UN-HIDES on the strength of "I looked and it wasn't there" needs a floor
check on the lookup itself.

### "Attempted" is not "removed"

`_forget_deliveries` dropped ledger rows per (row, user), but a NARROWED row only removes the libraries
it walked away from. So narrowing media to movies deleted the TV collection correctly and forgot the
*Movies* entry too — and for a `{top_seed}` row that entry is the only thing that could ever address
it again. It now filters on `in_sections`.

### A patch script that half-applied

`sync.users` shipped reading `state.app`, which nothing sets, so the nightly roster reconcile failed on
every attempt. Cause: a `python3` edit script asserted its anchor, raised on a LATER assertion, and the
symptom it printed got fixed while the earlier un-applied edits went unnoticed. **Re-verify the whole
file after a multi-edit script fails partway**, not just the line it complained about.

It survived a green suite because the scheduler test mocked `enqueue` and `drain_now` — proving the
scheduler CALLS the queue and nothing about whether the handler works. `test_each_handler_actually_runs`
now enqueues and drains for real against a live `app.state` and asserts the error carries no
`AttributeError`/`TypeError`. That is the two-line probe that catches this whole class.


### Two more, from verifying the fixes

**A durable job must distinguish "broken" from "not applicable".** Making `sync.users` a job turned
"Plex is not connected yet" from a silent log line into three retries, a `failed` row and a bell
notification — nightly, on any install whose wizard is unfinished. `sync_watched` already treated the
same condition as a skip. A nightly false alarm is precisely how an owner learns to ignore the bell, so
`_sync_users` now catches the 409 and returns a skipped result. **Anything moved onto the queue needs
its not-applicable states classified, or durability turns them into noise.**

**A successful-but-empty read is now logged.** It blocks nothing and prunes nothing — both correct —
but it defers every legitimate un-hide by a cycle with no signal at all. "I added Sarah to the Popular
row and she still can't see it" has to be answerable from the log.

### Defence in depth on the linked server

`ContextBuilder.build` now refuses to build a context when the PMS reports a different `machine_id`
than the linked `Server` row. The settings guard only fires when the new server ANSWERS at save time,
so a box that is down then and up later slipped past — and a stranger's PMS enumerates zero Shortlist
collections, which is exactly the empty-read input the prune must never act on. Two independent checks
now, at the door and at the point of use.


---

## 15. Live verification on SFLIX, 2026-07-29

Run against the real server (50 accounts, 105 MB database) after deploying `199c6fa`. A throwaway row
and Steve's own MooHouse account were used; no other user's row was touched.

| What | Result |
| --- | --- |
| Migration 0045 on the production DB | applied, pre-migration backup taken, `deliveries` created |
| Ledger records a real ratingKey | `574466` — matched the actual collection on the PMS |
| **C1**: narrow a shared row's audience | `privacy.sync` fired; the dropped account gained the exclude on plex.tv |
| Widen it back | the exclude was pruned again — both directions |
| Narrow media both→movie | removed ONLY the TV copy (`574467` gone, `574466` alive), ledger kept the live library |
| Delete a row | both copies gone, ledger emptied |
| Pause → unpause | all three flags off, then restored identically; filters merged first, in one job |
| Disable a user | collections removed AND their own filter written (`hide_shared_from_disabled`) |
| `sync.users` / `sync.history` / `backup.take` as jobs | all `done`; 50 accounts synced, real backup file written |
| Dead shared-row excludes | pruned from every account once the row was gone from config and server |
| Owner's Home | 2 rows, both the owner's own — still converged |

### Robustness

| Failure | Behaviour |
| --- | --- |
| `docker kill` mid-RUN | run marked `aborted` (not left `running`); boot queued a `privacy.sync` |
| Job left `running` by a dead process | requeued at boot and completed by the worker unaided (attempts 1→2) |
| Job stuck `running` >30 min, container UP | caught by the 5-minute sweep and completed (attempts 1→2) |
| Handler removed in an upgrade | failed cleanly, retried to its limit, reached the notification bell |

### The one bug the unit tests missed

`user.cleanup` removes a user's WHOLE label in one call, which the per-row `_forget_deliveries` never
sees — so disabling someone left the ledger pointing at ratingKeys that no longer existed. Bounded
(a stale key still has to find a collection under one of OUR labels, and re-enabling overwrites the
row) but it grew for ever and made the audit lie. `forget_user_deliveries` now runs on the cleanup
path. **Only a live disable surfaced it**: every unit test exercised the per-row path.


---

## 16. The bug live testing found that no test would have (2026-07-29)

**A scoped run rebuilt a DIFFERENT row's collection as itself.** Found by building a second row on
SFLIX and watching the first row's Movies collection disappear.

`deliver_rows` takes `sole_row`, which licenses it to treat a title mismatch as an in-place RENAME —
safe only when there is genuinely one row that could have moved. It was derived from the rows the run
BUILDS (`specs`, already filtered by `cfg.should_build`), not the rows the user HAS.

Every row has its own cron. So **every scheduled run is scoped**, and on a multi-row server row A's
3am cron announced "this user has one row", found row B's collection alone in that library (all of a
user's rows share one label — only the title tells them apart), and rebuilt it as row A. Row B was
destroyed, and the run reported a normal delivery.

Now derived from `owned` — audience-and-mute filtered, but NOT scope filtered.

### Why nothing caught it

- The unit tests build with `build_only=None`, so `specs == owned` and the two are indistinguishable.
- The full-stack test built both rows in one run, which leaves TWO collections per library — and the
  rename path additionally requires exactly ONE, so it never fired.
- The failing shape needs three things at once: two rows configured, only one built so far, and a
  scoped run for the other. That is not an exotic state — it is what happens the first night after
  anyone adds a second row.

The regression test now reproduces exactly that sequence, and fails on the old code.

**The lesson for this codebase:** `should_build` is a *scope* filter, and anything that reasons about
what a user HAS must not read through it. Worth grepping for other uses before adding more.
