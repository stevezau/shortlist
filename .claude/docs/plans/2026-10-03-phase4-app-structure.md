# Phase 4: app structure (after phase 1 lands)

Programme constraints: `.claude/docs/plans/2026-10-03-design-refresh.md`. Phase 1 delivered: tokens (`raised`,
`border-strong`, `faint-foreground`), `src/lib/selected.ts` (`selectedClass`), `PageHeader` without icon, `BackLink` naming the
destination, nav with Privacy (`/privacy`) and Activity (`/activity?tab=jobs|log`), lazy routes. Read `web/src/lib/selected.ts`,
`web/src/components/page-header.tsx` and `web/src/App.tsx` before starting.

Each task is owned by ONE agent; the file lists are the ownership boundary. Shared files (`src/lib/queries.ts`, `src/App.tsx`):
add, never restructure; re-read immediately before each edit because other agents edit them too. `tsc -b` will show other agents'
in-progress errors: fix only errors in your files; report others.

---

### Task 4A: Row editor + Rows list (cards app-row-edit, app-rows)

**Owns:** `pages/row-edit.tsx`, `pages/rows.tsx`, `components/rows/**` (except `row-rename` page), their tests in `src/test/row-*`.
**Spec:** `.impeccable/mocks/app-row-edit.html`, `app-rows.html`, renders `after-app-row-edit--*.webp`, `after-app-rows--*.webp`.

- One navigation model: a sticky jump list (Name & look, Who gets it, What goes in, Schedule, Placement, Requests, Danger zone)
  and every section rendered in one scroll. Delete the tab chips AND the accordions. Jump list highlights the section in view
  (IntersectionObserver), keyboard-operable links; on mobile it becomes a horizontal scroller.
- "Live on Plex" strip at the top with the label "Changes here apply to Plex immediately": the on/off switch, "Rename on Plex…",
  "Change artwork…", "Run now" — every control that writes immediately today (verify in code which ones do; move exactly those).
  The name field in "Name & look" is read-only with "Changed with Rename on Plex" (rename only happens through Rename today).
- Everything else is a draft saved by one sticky save bar: "N unsaved change(s) · <field>: old → new", Discard (ghost), Save changes
  (the one primary). Same save semantics as today.
- Audience ("Who gets it") and Schedule fully visible. "Who gets it" shows per person: libraries they get a copy in, and whether
  their account hides other rows (from `usePrivacyStatus()`), plus the owner caveat line.
- Schedule copy: "Runs on…" (schedule) and "Titles refresh every…" (cadence) with a "Next:" line computed from the same data the page has.
  The word "rebuild" no longer labels either.
- H1 renders the name template with placeholder chips. Header has no delete. The only delete is the Danger zone card:
  "Removes this row from N people's Plex Home. Shortlist's hide rules for it are cleaned up on the next run." + danger button (same dialog as today).
- Remove the "Outcome preview" sidebar and the "How this row is doing" tiles; their numbers become one line in the Live strip
  ("Last built 02:30 today · 60 titles delivered to 4 people · 0 watched so far · See runs →").
- Rows list: each row card shows a 4-poster collage (from the row's latest delivered picks, whatever the existing rows/picks query
  exposes; fall back to the row poster setting, then to the neutral placeholder), the on/off switch, a ghost "Run now", an overflow
  menu (Edit / Runs / Rename on Plex… / Remove or delete…) built on the existing dropdown primitive (keyboard + Escape), and
  "Last built <time> · <n> people". The placeholder explanation becomes one `note` line under the header. One primary: "Add a row".
- Tests: update the row-editor tests for the removed tabs/accordions (their assertions about SAVE behaviour must keep passing
  unchanged); new tests: jump list renders the seven sections in order; Live-strip switch still writes immediately (assert the
  mutation call and its arguments); a draft field change shows the save bar with the diff text and does not call the save mutation
  until Save; Rows overflow menu contains the four actions and no red text renders in the list until the menu opens.

### Task 4B: Settings split + Privacy page (cards app-settings, app-privacy)

**Owns:** `pages/settings.tsx`, `components/settings/**`, `pages/sharing.tsx` (the Privacy page) + any component it owns,
settings/sharing tests, the settings routes in `App.tsx`.
**Spec:** `.impeccable/mocks/app-settings-connections.html`, `app-settings-defaults.html`, `app-privacy.html` + renders.

- Settings becomes three routes under one PageHeader ("Settings" / one line) with a tab strip: `/settings/connections`,
  `/settings/defaults`, `/settings/system`. `/settings` → `/settings/connections`. Old hashes keep working: extend
  `settingsSectionForHash` so `/settings#recommendations|#defaults|#placement|#requests` land on Defaults (scrolled to that section),
  `#notifications` on Connections (webhook row), `#advanced|#api-access|#danger` on System. The left `SettingsSubNav` under the
  Settings nav item is removed (the tab strip replaces it).
- Connections: every service as one row (status pill Connected / Not set up / Optional, "Edit · Test" or "Set up"), grouped
  Essential / Discovery & watch history / Requests / Notifications (Webhook appears ONCE, here; delete it from any other section).
  Trakt's "Needs paid Trakt VIP" warning shows only inside Trakt's edit/setup state. Existing edit/test dialogs reused unchanged.
- Defaults: Title sources, Refresh & variety ("Titles refresh every…" presets + custom days as ONE control; already-watched; recent
  releases incl. the year-bars chart), Row defaults (template chips + "On Plex this looks like" preview, how many titles), Row
  placement (shelf-ordering switch with its own line "Moves other tools' rows on the shelf"), Requests ("Fill in the gaps
  automatically"), web search + "More recommendation controls" so nothing is lost. Jump list + one sticky save bar as today's save semantics.
- System: retention controls, console log detail, run concurrency, timeouts, API access, Danger zone (Pause all users stays here AND
  on Users? — no: remove the duplicate from Danger zone only if Users has an equivalent bulk pause; otherwise keep it).
- Search box "Search settings…" (top right, `/` shortcut) filters across all three tabs by label/description text and jumps to the match.
- Privacy page (`/privacy`): header "Privacy" / "Which rows each Plex account can see, read live from plex.tv.", action "Read again"
  (refetch). Status strip: Accounts hiding every row "N of M", Accounts that cannot be hidden, Last verified ("Not checked recently"
  as a WARNING pill, not grey), Snapshots kept. Existing red callout and Accounts ledger unchanged in content. New "Policy" panel:
  the `privacy.hide_shared_from_disabled` switch MOVES here from `advanced-section.tsx:182` (same key, same save call, saves as you
  flip), plus a non-control fact row "Your own account sees every row" with a "Plex limit" pill. "Is Plex applying the rules?" panel
  with the warning state and ONE primary "Verify now" (same action as today's "Go to Runs"/start-run path; caption says it starts a run).
- Tests: routes + hash redirects (each old hash lands on the right tab); the moved switch saves `privacy.hide_shared_from_disabled`
  with the right body from the Privacy page and no longer renders in Settings; Webhook appears exactly once across the three tabs;
  search finds "Disabled users see nothing" → navigates to /privacy (it moved) — or shows "Moved to Privacy" with a link.
- e2e string updates expected: `test_settings_e2e.py` heading "Settings" still exists; `link "Notifications"` (`test_notifications_e2e.py:161`)
  and `/settings#…` URLs across e2e must still work through the redirects (fix the test only if the label itself was renamed).

### Task 4C: Users + Activity "Changes on Plex" (cards app-users, app-activity) — starts after Task 4C-api

**Owns:** `pages/users.tsx`, `pages/activity.tsx` (adds the third tab), new `components/activity/changes-on-plex.tsx`, users/activity tests.
**Spec:** `.impeccable/mocks/app-users.html`, `app-activity.html` + renders.

- Users: one state vocabulary On / Paused / Off (status pill per person); "Restricted · <profile>" is a separate NEUTRAL pill
  (never red). Header: search, filter segment (All · Needs attention · Paused · Off with counts), ONE primary "Add people"
  (whatever today's equivalent action is). Columns: Person, Status, Rows, Picks watched (30 days), Privacy ("Hiding every row" /
  "Won't accept hide rules" — same words as the Privacy page; data from `usePrivacyStatus()`), Last built. Mobile: one card per
  person; "No request source connected" appears once at the top, not per person. Bulk actions that exist today stay reachable
  (selection mode) — do not drop functionality to match the mockup.
- Activity "Changes on Plex" tab: table grouped by run (run header row: "Run #N · <date time> · <trigger> · <n> people"),
  newest first: time (absolute, server local), who/what, change sentence, Real/Dry run pill, "Diff" ghost button opening the raw
  `message` JSON in a dialog. Filter input + All/Real/Dry run segment. "Kept forever. Change in Settings → System" line from
  `events.retention`. Intro line mentions the Jobs tab's "Changes Plex" / "Can delete" pills.
- Change sentences come from a pure function `describeChange(event): {who: string; what: string; change: string}` in
  `src/lib/describe-change.ts` covering every Plex-write scope listed in Task 4C-api; unknown scopes fall back to the scope name
  + a JSON summary. Vitest table test over one recorded example per scope (build the examples from real `message` shapes in
  the server code that emits them).

### Task 4C-api (backend, before 4C; after phase 2.1 finishes because both regenerate the OpenAPI types)

**Owns:** `shortlist/server/api/events.py`, a new schema module or `schemas_events.py` additions, `tests/integration/test_api_events*.py`, OpenAPI snapshot + `web/src/lib/api-schema.d.ts`.
- `GET /api/events/log` gains a typed response model `EventOut {id: int, ts: str, level: str, scope: str, message: dict[str, Any]}`
  and two optional query params: `scope_prefix: str | None` (e.g. `run.`) and `plex_writes: bool = False` which restricts to the
  Plex-write scopes: `run.user`, `run.shared`, `run.sweep`, `run.orphan_delete`, `run.privacy_sync`, `run.demote`, `run.hub_order`,
  `run.hub_unplaced`, `run.requests`, the poster-reset / row-removal / rename scopes in `collection_reconcile.py` (:710, :737, :1167),
  `user.disable.cleanup`, `user.pause.hide`, `uninstall.user`, `system.uninstall`, and `RESTRICTION_RESTORED_SCOPE` (audit.py:76).
  Define the set once as a module constant next to where those scopes are defined or in `services/audit.py`; a unit test asserts
  every scope string in that constant is actually emitted somewhere in `shortlist/` (grep-based test) so the list cannot rot.
  Existing params `scope`, `limit`, `before_id` unchanged. Integration tests: prefix filter, plex_writes filter, paging unchanged, response shape.
