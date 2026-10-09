# Row editor cleanup — brief (2026-09-27)

Owner's request, after editing row 2 ("🎯 Because you watched {top_seed}") on production: "editing
the row is not clear… it feels messy, let's get it all cleaned up. Think through everything carefully."

## What the owner raised (design every item — see memory "design-every-item-not-just-in-time")

1. **Seasons shows on a "Because you watched X" row.** Owner thinks it belongs only to the seasonal row
   template.
2. **"Make this a 'watch it again' row" shows on every row.** Owner thinks it belongs only to the
   watch-again template.
3. **Rows should have a TYPE/TEMPLATE, and only that type's settings should show.** Open question:
   a changeable template picker at the top, or a fixed template (delete and recreate to change) — "so
   templates are fixed, meaning settings are?"
4. **Requests settings (in the row editor):** are they all meaningful, and why do they use checkboxes
   when most other settings use toggles? Make it consistent.
5. **"Recent watches to choose from" (rotation) cannot be found.** Fact: it is rendered only when
   "Watches every source builds from" is 1 or 2 (`web/src/components/rows/row-editor.tsx:1197`). Row 2
   is max_seeds=3, seed_window=1, so the setting is invisible and nothing says it exists.
6. **The right-hand "What this row will do" summary must be accurate** and reflect every setting.

## Facts already established

- Row 2 on a large production server: media=both, max_seeds=3, seed_window=1, seasons=[] (read from the live DB).
- Seasons group is shown on every row except the default one (`row-editor.tsx:754`).
- The editor already warns: "This row's name mentions one title, but it's built from 3 watches… Set it
  to 2 — one film and one show."
- Rotation (`seed_window`) cycles once per UTC day, not per run (`rows.py` `seed_cycle_offset`, run_day).
- Issue #133 fix (79cb98a7, deployed 2026-09-27): a `{top_seed}` row is named after the watch it was
  built from; each library names its own watch.

## Read first

- `.claude/docs/row-types-architecture-review.md` (row types may already exist as a concept)
- `.claude/docs/discussion-124-seasonal-rows.md`
- `.claude/rules/frontend.md`, `docs/guides/rows.md`
- Memories: design-questions-in-prose, my-taste-is-not-the-default, fix-the-complaint-not-its-
  generalisation, measure-layout-dont-reason-about-it, test-frontend-at-1024, browser-verify-with-fake-plex

## Process

`superpowers:brainstorming` first. Owner wants careful thinking, not a quick patch. Design questions in
prose while the shape is open; option picker once decisions are concrete.
