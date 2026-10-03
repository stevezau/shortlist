# Phase 6: small app items (after phase 1 lands; parallel with phase 4)

Programme constraints: `.claude/docs/plans/2026-10-03-design-refresh.md`. Phase 1 primitives: see phase-4 plan header.
app-perf (code splitting) already shipped in phase 1.

**Owns:** `pages/setup/**` (wizard), `pages/uninstall.tsx`, `pages/row-rename.tsx`, `pages/run-user-trace.tsx` (person trace and
row trace), `components/ui/button.tsx` size tweak only, and their tests. Do not touch files owned by phase 4/2 agents
(row-edit, rows, settings, sharing/privacy, users, activity, run-detail, dashboard).

- **app-wizard:** on the Welcome step, "Plex cannot hide other people's rows from the server owner" becomes its own callout
  (info style) directly under the welcome copy; the TMDB/AI paragraph shrinks to one line. Test: the callout text renders inside
  an element with `role="note"` (or the callout component's role) before the Get started button.
- **app-uninstall:** the preview is inline and mandatory: the page loads the existing preview/dry-run data on mount and shows counts
  ("Restores N sharing snapshots, deletes M collections, removes K labels") before the confirm controls; the confirm button stays
  disabled until the preview has loaded. Same for Rename on Plex: show "Renames this row's collection for N people" with the count
  before the button, and make the Rename button the screen's primary. Tests: confirm disabled until preview resolves; counts render.
- **app-trace:** person trace stage counts reconcile: every stage shows one number drawn from the same data, and the funnel is
  monotonic (Watched → Searched → Shortlisted → Ordered → Delivered never increases). Find the source of "strongest 80 kept" /
  "Searched 9" / "52 made the shortlist" / "all 17 candidates" in `run-user-trace.tsx` and make the labels state what each number
  counts. The repeated "watched most recently" column collapses to one label. Test: a fixture trace renders a non-increasing funnel.
  If a number cannot be reconciled without an engine change, label it precisely instead and report it.
- **app-rowtrace:** the row-trace error state shows "This run built no shared rows" (or "This row was not part of run #N") with a
  BackLink to the run, never the raw API string, and no Retry when the error is a 404. Test for the 404 copy.
- **app-mobile:** touch hit areas ≥44px on coarse pointers for `size="icon"` and `size="sm"` buttons (padding or pseudo-element under
  `@media (pointer: coarse)`), visual size unchanged on desktop. Native checkboxes on Users get a 44px label hit area.
