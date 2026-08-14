# Issue #84 — a row that cannot name itself

Status: **planned, not built**. Two previous attempts were reverted; read "What went wrong twice"
before writing any code.

## The report

`PottierLoic`, 22 users. One per-person row, movies only, named
`Car vous avez regardé {top_seed}`, "Built from 1 watch".

Result on Plex: **3** collections named `Car vous avez regardé <film>`, and **~19** named
`✨ Picked for You` — in English, on a server whose row-name template is
`Spécifiquement pour le grand {user}`.

Not two rows. One row, delivered to 22 people, where 19 of them could not be named.

## Why

Two settings contradict each other and nothing notices.

1. The row's name needs a watch: `{top_seed}` is filled from a pick that traces back to something
   the person watched.
2. **Enough watch history** (`recommendations.cold_start`) defaults to `"popular"` — someone below
   the threshold still gets a row, filled with the server's top-rated titles. Those picks carry no
   `seed_title` by construction (`rows.py::_cold_start`, `reason="Popular on this server"`).

So Shortlist builds a row _defined by a watch_ for people with no watch, fills it with titles that
followed from nothing, then cannot name it — and `render_row_name` substitutes the hardcoded
`DEFAULT_ROW_NAME` ("✨ Picked for You").

Both halves are wrong. Even correctly translated, "Car vous avez regardé X" over generically popular
films is a false claim.

## The principle the owner set

> Whoever asks for the row provides the name. If you don't ask for it, it isn't built. If you do,
> you name it. Shortlist never makes one up.

Note the codebase already agrees elsewhere: `render_poster_text` returns `""` and drops the text
rather than stamping a substitute onto artwork. Row titles never got the same treatment.

## The fix, in five parts

1. **New per-row field `fallback_name`** — "Name to use when there's no watch yet".
   `collections.fallback_name`, nullable `String(255)`, plus `RowSpec.fallback_name: str = ""` and
   the mapping in `context_builder` (~line 839, beside `cold_start=`).

2. **`render_row_name` stops inventing.** Order becomes: the row's own template → `fallback_name` →
   `""`. `DEFAULT_ROW_NAME` survives only as the literal default-row title, never as a substitute.

3. **An empty title means the row is not built for that person**, and any copy an earlier version
   wrote is removed by LEDGER KEY (its title cannot be recomputed — `remove_row(..., delivered_keys=)`
   is the path that exists for exactly this).

4. **The row editor requires it.** When a row's name uses `{top_seed}` and its cold-start setting is
   `"popular"`, the fallback name is required — the incoherent combination cannot be saved silently.
   New `{top_seed}` rows default their cold-start to `"skip"`.

5. **Migration 0070 preserves existing behaviour.** For every collection whose `name_template`
   contains `{top_seed}` and whose `fallback_name` IS NULL, set `fallback_name` to the current global
   `row.name_template`. Existing rows inherit `cold_start=None` → global → `"popular"`, i.e. they have
   effectively already chosen "build it", so rule 2 applies and they must get a name. **No row
   disappears on upgrade**, and the reporter's 19 become "Spécifiquement pour le grand <name>" —
   honest, because that name claims nothing about a watch.

## Sentinel migration (the risky part)

`display == DEFAULT_ROW_NAME` is used in four places to mean "this row has no title of its own":
`delivery.py::_remove_row_for_user` (~506), `collection_reconcile.py` (547, 582),
`context_builder.py` (~913). Each becomes an empty-string test. Every one of these guards a path that
MATCHES a collection by title — get it wrong and a row is never promoted (placement bug) or the wrong
collection is matched (privacy bug).

`seed_source` (delivery.py) already unifies the per-library seed rule across delivery and the
placement stamp; keep it that way.

## What went wrong twice

**Attempt 1** (`5cebfac`, reverted in `33ba725`) — skipped delivery whenever the title collapsed to
the default. Cold-start picks carry no seed _by construction_, so it silently turned
`cold_start: popular` into `cold_start: skip` **and deleted rows people already had**, against
`effective_cold_start`'s own promise. Also row-level where titles render per library, and it left the
skipped row's claim in `placement_titles`, so promote would apply the wrong row's placement to the
person's real default row. Caught by review AND independently by e2e.

**Attempt 2** (`85f6e9d`) — correct, but only fixed the per-library half: a library with no seed now
borrows the row's. That is in and shipping. It does not help when NO library has a seed, which is
this issue.

Lesson for attempt 3: route the skip through the _configured_ behaviour, never bolt it onto
delivery; and never let a naming change delete a row.

## Proving it

- Unit: the matrix on `render_row_name` (own template / fallback / neither) and on the delivery skip.
- Mutation-test each new test — two of the tests written for attempts 1 and 2 passed for the wrong
  reason and had to be thrown away.
- Migration: `test_migration_recovery.py` replays every revision; assert the backfill on a DB holding
  a `{top_seed}` row.
- Architecture Review is MANDATORY here (Plex writes + a migration + title matching).
- Live on SFLIX: a `{top_seed}` row against real users, checking that nobody gets an English title and
  that no row is deleted that should not be.
