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
5. ✅ Migrating background work onto jobs — **`user.cleanup` done** (enqueued then drained
   inline, so it still feels instant but is now retried and survives a restart). `sync.check` and
   `privacy.sync` handlers exist. Remaining: row reconciles, sync.users, sync.history, backups.
6. 🔶 Eager filter writes — **new accounts done** (`_hide_existing_rows_from_new_accounts`: a sync
   that adds anyone fires `engine_run(ctx, [])`, which merges every share filter while creating and
   promoting nothing). Remaining: disable, and shared-row audience changes.
7. ✅ Pause = hide; unpause = restore (converge takes a paused user's rows off EVERY surface,
   keeping the collection + label so excludes still match and unpausing is a re-promote; `user.hide`
   job fires the moment someone is paused)
8. ⬜ Confident orphan deletion in the retire phase
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
