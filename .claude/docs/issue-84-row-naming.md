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

---

# Attempt 3 (`61b26b8`, `b7b34b4`) — BLOCKED by review, do not deploy

Code is on `dev` and green in tests, but the Architecture Review found **four HIGH** issues, each
reproduced against real code. Fix these before the change is trusted; the tests passing means the
tests did not cover them.

## HIGH 1 — an unnameable row blanks the user's label, and "" is merged into share filters

`delivery.py:468`. `_deliver_one` now returns `("", CollectionDiff())`, but `deliver_rows` still does
`stored_labels[stored_key] = stored` unconditionally. `stored_key` is `profile.slug`, shared by ALL
of a person's rows — so one unnameable row erases the label a nameable row just recorded, and
`desired_excludes` then writes it:

    merge_label_excludes("label!=Shortlist_bob", {"", "Shortlist_mike"})
      -> "label!=Shortlist_bob,,Shortlist_mike"

That is a malformed filter on a real plex.tv share and, while it stands, NO exclude for that person's
label. Fires when a `{top_seed}` row has no `fallback_name` and a user has picks but no seed — the
default configuration.

**Fix:** only record a label when one was actually stored (`deliver_rows`' own docstring already says
so), and make `desired_excludes` drop falsy labels.

## HIGH 2 — migration 0070 misses the rows that actually exist, and is a no-op on the reporter's server

`0070`'s `WHERE name_template LIKE '%{top_seed}%'` is the wrong predicate:

- **The Rows page writes the template into `name`, not `name_template`** (pinned by
  `web/src/test/row-templates.test.tsx:91`; the editor reads `name_template || name`). Every
  UI-created `{top_seed}` row has `name_template = ''` and is skipped.
- **The default row** is seeded with `name_template = ''` (`0001_initial.py:295`); its effective
  template is the global setting. Never matched.
- **When the global template itself contains `{top_seed}`** — issue #84's exact server — the value
  backfilled is one `render_row_name` discards, so it achieves nothing.

So the migration whose stated purpose is "nothing disappears" does nothing on the reporting server,
and after upgrade those rows silently stop being built for everyone below `min_history`.

**Fix:** match `COALESCE(NULLIF(name_template,''), name)`; handle the default row explicitly; refuse
to backfill a value containing `{top_seed}` (leave NULL and warn, naming the affected rows). Add a
migration-test row for each of the three shapes.

## HIGH 3 — a fallback name disables `remove_row`'s ledger match, and can delete a SIBLING row

`delivery.py:529`. With a fallback, `unrenderable = not display` is False, so the EITHER/OR at `:556`
consults only the title — and that title is not what a seeded user's collection wears:

    no fallback,   ledger key: removed correctly
    WITH fallback, ledger key: NOT removed (mute/cold-skip silently stops working)
    WITH fallback, sibling row titled the same: deleted the WRONG collection

Migration 0070 grants a fallback automatically, so this becomes the common case.

**Fix:** for a `{top_seed}` template keep `unrenderable` True regardless of the fallback, or try the
ledger key AND the title. Also give `context_builder._retired_rows`' emitted RowSpec the
`fallback_name` it gates on (`context_builder.py:915` vs `:919`).

## HIGH 4 — PATCH never clash-checks `fallback_name`, and PATCH is what the editor uses

`collections.py:723`. POST checks it (pinned by `test_two_rows_sharing_a_FALLBACK_name_still_clash`);
PATCH gates on `sent & {"name","name_template"}` and writes the column unchecked, so two rows can end
up sharing a fallback name — the state POST returns 422 for, and the direct enabler for HIGH 3's
wrong-delete.

**Fix:** clash-check whenever `"fallback_name" in sent`, against the merged post-PATCH row, and add
the PATCH cell to `TestNoTwoRowsShareATitle`.

## MED, also outstanding

- `_deliver_one`'s comment claims `deliver_rows` removes the old copy by ledger key. **It does not.**
  The pre-upgrade "✨ Picked for You" collection stays, keeps its label, and is re-promoted every run.
  Either implement the removal (and record it in `report.removed_deliveries`) or drop the claim.
- POST stores `body.fallback_name or None`, so the deliberate empty string the migration promises not
  to overwrite is a state only the test can create. Store it verbatim, or gate the backfill on a
  one-shot marker.
- Changing `fallback_name` triggers no rename reconcile (`touching_name` covers only name/template).
- Five comments still reason from the old `DEFAULT_ROW_NAME` behaviour
  (`collection_reconcile.py:74,90`, `settings.py:164`, `context_builder._retired_rows`,
  `delivery.py:541`) — these are what the next person will read before touching the removal paths.
