# Issue #102 — "When it appears": per-row day scheduling

Status: **built** on `feat/row-visibility-schedule`. Design approved 2026-08-27, implemented 2026-09-02. Every measurement below was taken, not
estimated — see "What was measured".

## The report

`shonufgit`: rows are permanent fixtures. He wants the Home screen to change — "right now it might
show a because you watched row but a few hours later or tomorrow it shows picked for you instead".

Asked which he actually meant, he chose **different rows**, not different titles, and scoped it
himself:

> I personally don't care about having overly granular control beyond make row 1 appear Monday,
> Wednesday, Friday and row 2 on the other days

He also confirmed the client-cache question: his Roku re-reads Home on its own, his Shield needs him
to navigate off Home and back.

## What we build

One new per-row setting: **which days of the week the row is shown.** On its off days the row is
hidden but keeps its titles, so it returns without a rebuild.

**Out of scope, deliberately:** time-of-day windows, rotation groups, anything sub-daily. All three
reduce to the same yes/no this answers, so they can be added later without rework. The reporter said
he does not want them; building them on speculation buys a settings screen nobody uses.

## Why this is small

Shortlist already has a "show this row nowhere" state — the `off` placement. A row whose
`placement`/`placement_friends` resolve to `off` is delivered, browse-hidden, and promoted to no
surface at all.

**So the schedule does not need a hiding mechanism. It needs to make a row's EFFECTIVE placement
`off` on its off days.** That is one existing, already-tested code path
(`pipeline._promote_one` → `PlexClient.promote(home=False, shared=False, recommended=False)`).

Verified against the fake PMS: running the engine with `placement="off"` left the row off the user's
Home, all three promoted flags `False`, and not one title rewritten.

## Data model

Two columns on `collections`, plus Alembic migration `0088`.

```python
# Which weekdays this row is SHOWN, as ISO weekday numbers (1=Mon .. 7=Sun).
# [] -> every day, which is what every existing row gets on upgrade: nothing changes.
show_days: Mapped[list] = mapped_column(JSON, default=list)
# What the midnight job last applied, so a tick with nothing to do makes no Plex calls.
# NULL -> never evaluated (a row that predates this, or one never delivered).
shown_state: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
```

`show_days` is a JSON list rather than a 7-bit integer or seven booleans: it round-trips to the API
as-is, it reads in a database dump, and an empty list is unambiguously "unset" (the same convention
`library_keys` and `candidate_sources` already use).

**Backfill: none.** Empty means every day. An upgrade changes no row's behaviour.

## The decision, in one pure function

```python
def row_is_shown(show_days: list[int], now: datetime) -> bool:
    """Is a row with this day schedule shown at `now`? Empty schedule -> always shown."""
    return not show_days or now.isoweekday() in show_days
```

Evaluated in exactly one place — `context_builder._build_rows`, where the `Collection` row becomes a
`RowSpec`:

```python
shown = row_is_shown(collection.show_days, datetime.now())
placement = collection.placement if shown else "off"
placement_friends = collection.placement_friends if shown else "off"
```

**The engine never looks at a clock.** It receives a `RowSpec` whose placement already accounts for
today, so it stays pure and deterministic, every existing test keeps its meaning, and the nightly run
and the midnight job go down one identical path.

`datetime.now()` with no tz is the container's local time — the same clock every other schedule in
Shortlist runs on (`AsyncIOScheduler()` resolves to the local zone; verified `Australia/Sydney`).

## The midnight job is the mechanism, not a footnote

Rows build at **03:30** (`DEFAULT_CRONS`). If a run were the only thing flipping visibility, a Monday
row would sit on people's Home until 03:30 Tuesday — and a row that rebuilds weekly would be days
late.

So: one cron at `0 0 * * *`, `rows.visibility`, on the durable queue like every other scheduled task.

`shown_state` is what makes the steady state free: a tick where nothing changed costs one query and
not a single Plex or plex.tv call.

### One converge job, not a hide and a show

`rows.visibility` does not compute anything itself. `_build_rows` has already resolved every row's
schedule into its placement for today, so the handler's whole job is to notice that a row's answer
CHANGED and converge Plex onto it:

```
desired[slug] = the spec claims any surface today
changed       = rows where desired != collections.shown_state
if not changed: return            # no Plex calls, no plex.tv calls
merge every account's filters and CHECK it, then promote every enabled, unpaused user's rows
record shown_state, only after the converge landed
```

Two handlers (`row.hide` / `row.show`) were the first design and were dropped: with the schedule
already encoded in the placement, a "hide" is just a promote whose placement happens to be `off`, so
splitting them meant two spellings of one operation and two chances to disagree. The single pass also
self-heals drift from any cause, not only from a schedule.

It always merges filters first, even on a night that only hides. That is slightly more work than a
pure hide needs, and it is the safe direction: the merge is monotonically private, it is a no-op when
the filters already match, and making the gate unconditional means there is no branch that can be
taken wrongly.

`promote_user_rows` is reused per user rather than a new per-row write path, so promotion goes through
the code a run already exercises every night.

## Why the ledger, and not labels

A per-person row's Plex label names the **person**, not the row: all of Sarah's rows carry
`shortlist_sarah`. So nothing in `_converge_phase` — which classifies by label — can tell one of her
rows from another, and it cannot be extended to handle this.

`Delivery` can: `(collection_slug, user_slug, library_key) -> rating_key` is the only authoritative
answer to "which object on the server is this row, for this person, in this library".

**Plex re-uses `metadata_items.id`.** A ledger key whose collection was deleted can come to name a
different object entirely, so the ledger is never the sole authority for a write: `promote_user_rows`
walks each user's own `shortlist_<slug>` label and uses the ledger only to decide WHICH ROW a
collection it already owns belongs to (plex-safety rule 4).

## Editor save applies immediately

Changing the days is handled like enabling/disabling a row: `plan_row_changes` gains a rule, and the
`PATCH` handler enqueues and drains it. Otherwise you set "weekdays only" on a Saturday, nothing
visibly happens for hours, and it reads as broken — which is exactly the bug the `collection.disable`
rule was added to fix.

```python
if change.enabled_after and tuple(change.days_before) != tuple(change.days_after):
    plan.append(PlannedWork(kind=VISIBILITY, scope=f"the days row '{change.slug}' appears on changed"))
```

`VISIBILITY` is planned LAST and runs the same merge-first converge job, so the ordering guarantee
holds on this path too. It is skipped for a row being switched OFF, which already deletes its
collections — there would be nothing left to show or hide.

## UI

**Edit Row → a new "When it appears" group**, directly under "Where it appears" — where the reporter
asked for it, and where it reads as the pair it is.

- `Segmented`: **Every day** (default) / **Only on these days**
- Seven day chips, Monday-first, matching `RowScheduleField`'s existing shape
- A resolved sentence under the control: _"Shows on Monday, Wednesday, Friday. Hidden on Tuesday,
  Thursday, Saturday, Sunday — its titles stay, so it comes straight back."_
- `SettingsGroup` summary: `Mon, Wed, Fri`

**Rows list → a badge per row**: `Showing today` / `Hidden today`, from the API's `shown_today`
field — computed on the SERVER's clock. A badge derived from `new Date()` in the browser reads the
admin's timezone and can contradict what Plex is showing for as long as the offset lasts. Without it, "my row disappeared" becomes
a support question — this feature's one real ongoing cost is adding a fourth reason a row can be
missing, alongside disabled, paused, and cold-start-skipped.

**One guard, one deferral:**

- No day selected is not a reachable state. `[]` already means EVERY day, so letting the last chip
  off would turn "only Mondays" into "always on" — the opposite of the click. The editor refuses it;
  clearing the schedule is what **Every day** is for, and there is no second spelling of "never"
  because switching the row off already means that.

**One note on the Run action:** "Run now" on a hidden row rebuilds it and it stays hidden. Correct,
and confusing, so the button says so.

## Decisions taken

| Question                                                   | Decision              | Why                                                                                                                                                                                                                      |
| ---------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Does a hidden row still rebuild?                           | Yes                   | The rebuild schedule already exists and already does this. Coupling them would make one control silently change another.                                                                                                 |
| Does a hidden row still request titles from Sonarr/Radarr? | Yes                   | Requests are driven by what a row was built with, not what is shown (`_request_phase` runs last, independent of promotion). The titles are for when it appears. Cost: a row shown 3 days a week spends request budget 7. |
| When does an editor change take effect?                    | Immediately           | See above.                                                                                                                                                                                                               |
| A day with no row scheduled?                               | Allow, warn           | Overriding the owner is worse.                                                                                                                                                                                           |
| Time windows / rotation groups?                            | Later, if asked twice | They compute the same boolean; no rework needed to add them.                                                                                                                                                             |

## What was measured

Live on SFLIX, 2026-08-27, throwaway collection deleted in a `finally` (verified `leftover: []`):

```
Movies    9,946 items          visibility write 0.005s   (create 1.88s  / delete 0.58s)
TV Shows  4,879 shows ~120k ep visibility write 0.005s   (create 26.44s / delete 14.49s)
```

**A hub visibility write is ~5ms on every library, TV included** — ~3,000x cheaper than changing what
is _in_ a collection. Five scheduled rows across the roster is roughly 460 flips at midnight: **under
3 seconds**. The 16.5s TV anomaly is membership-only and does not apply here.

## Checked, not a problem

- **Upgrade**: `show_days = []` means every day. No row changes behaviour.
- **Shelf-contention notification**: needs one row moved 3× in a day (`_CONTENTION_REPEATS`); a
  scheduled row returns at most once. No false alarm.
- **DST**: the day turns at local midnight, so no day is skipped or doubled.
- **Timezone**: one server clock. A viewer abroad flips at the server's midnight — same as every
  other Shortlist schedule.
- **Concurrency**: the midnight job and a run compute visibility from the same schedule, so they
  agree; there is no last-write-wins conflict to resolve.

## Testing

Per `.claude/rules/testing.md`, and the matrix that matters here is **which reason a row is hidden**:

- `row_is_shown` — every weekday, empty schedule, all seven selected, none selected.
- **A run does not resurrect a scheduled-off row.** Full-stack against `fake_plex`: schedule off, run,
  assert not on Home. (This is the probe that shaped the design — a run _does_ put back a row demoted
  behind the engine's back.)
- **A paused user's row is not restored by the visibility tick.** The §12 mutation class; its own test.
- **`rows.visibility` promotes nothing when the filter merge fails**, and leaves `shown_state` alone so the retry still has work to do. Assert no promote call after a plex.tv 503.
- **A ledger key never authorises a write on its own.** Promotion walks the user's own label; the
  ledger only says which row an already-owned collection belongs to.
- Property test: `show_days` round-trips API → DB → `RowSpec` → placement.
- Editor: saving days applies immediately; the badge reads `Hidden today` on an off day.
- `--dry-run` previews both transitions and writes nothing (rule 8).

## Build order

1. `show_days` + `shown_state` + migration `0088`; `row_is_shown`; `_build_rows` resolves placement.
2. The `rows.visibility` cron and its single converge handler, with an audit event per pass (rule 10).
3. Runs agree with it — `_build_rows` already does this, so this step is the full-stack test proving
   03:30 does not undo midnight.
4. Editor group, Rows-list `Showing today` / `Hidden today` badge, the preview-panel line. Docs in the
   same PR (`.claude/rules/docs.md`).

Architecture Review is required before commit: this touches privacy, share filters, what reaches
Plex, and adds a migration.
