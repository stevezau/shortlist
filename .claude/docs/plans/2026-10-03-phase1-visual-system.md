# Phase 1: visual system, nav, page header, route shells, code splitting

Programme constraints: `.claude/docs/plans/2026-10-03-design-refresh.md` (Global constraints are binding).
Spec: review cards app-system, app-nav (owner change: Star on GitHub + Buy me a coffee stay visible in the rail, never in a
sub-menu), app-perf; mockups `.impeccable/mocks/shortlist.css` (token + component vocabulary) and any `app-*.html` for the rail,
header and selected-state look; renders in `.impeccable/mocks/renders/`.

All paths below are under `web/` unless prefixed `tests/`. Verified facts from the code scout:
- No font set anywhere (Tailwind default stack). Server CSP is only `frame-ancestors 'none'` (shortlist/server/main.py:100).
- Selected states using filled/tinted primary: `components/layout/app-shell.tsx:176,188` (nav), `components/segmented.tsx:39`
  and `components/number-presets.tsx:68,81` (Button `default` variant when selected), `components/ui/tabs.tsx:25`,
  `components/settings/settings-nav.tsx:51`, `pages/users.tsx:497,519`, `components/settings/idle-hold-field.tsx:132`,
  `components/rows/row-template-gallery.tsx:226,252,257`, `components/rows/row-kind-picker.tsx:49`,
  `components/rows/row-based-on-field.tsx:42`, `components/rows/row-show-days-field.tsx:74`,
  `components/seasons/season-when-fields.tsx:120`, `components/runs/user-tabs.tsx:56`, `pages/run-user-trace.tsx:608,905`,
  `pages/requests.tsx:868,877,901,1667,1772`, `components/dashboard/impact-report.tsx:1181` (rank numbers),
  `components/layout/activity-indicator.tsx:190`, `activity-pill.tsx:75`, `components/ui/badge.tsx:11`.
- `PageHeader` (`components/page-header.tsx:41-47` renders the icon tile) is used by rows, uninstall, runs, users, sharing,
  requests, logs, jobs, dashboard, watching-account; `users.tsx:278` and `requests.tsx:1467` pass `className="[&>div>span]:hidden"`;
  `settings.tsx:34-38` and `issue.tsx:368` have their own headers. Check row-edit, row-rename, run-detail, user-detail too.
- `BackLink` labels vary: "Users", "Back to Rows", "All users", "Back to run #N", "Rows", "All runs", "Settings".
- Nav: `NAV_ITEMS` at `app-shell.tsx:38-47`; `HelpLinks` :55 (Help & docs :62, Have an issue :70), version :86-131,
  star + coffee :142-153; `SettingsSubNav` under Settings at :199; separate `components/layout/mobile-navigation.tsx`.
- Routes: `src/App.tsx:107-146`, every page imported statically, no React.lazy.
- Tests: vitest files live in `src/test/` (mobile-navigation, sharing-page, jobs-page, logs-page, users-page, settings-nav …).
  e2e strings that depend on today's labels: "Sharing and privacy" (`tests/e2e/test_sharing_and_posters_e2e.py:95,140`,
  `tests/e2e/test_mobile_audit.py:160`), `/sharing` URLs (`test_sharing_and_posters_e2e.py:93,120,130,144`), `/jobs` `/logs`
  (`test_mobile_audit.py:167-168`).

## Task 1.1: Tokens, fonts, primitives

- [ ] `pnpm -C web add @fontsource-variable/source-sans-3 @fontsource/jetbrains-mono` (self-hosted: Shortlist runs on LANs that
  may have no internet). Import in `src/main.tsx`. Tailwind `fontFamily.sans = ["Source Sans 3 Variable", "Segoe UI", "system-ui", "sans-serif"]`,
  `mono = ["JetBrains Mono", "ui-monospace", …]`. `body` gets `font-variant-numeric: tabular-nums`. Mono only where code/cron/log/
  filter strings are shown; `font-variant-ligatures: none` on filter strings (`!=` must not render as ≠).
- [ ] Tokens in `src/index.css` (keep every existing one): `--raised: 240 6% 15%`, `--border-strong: 240 5% 24%`,
  `--faint-foreground: 40 4% 52%` (verify ≥4.5:1 on `--background`; adjust lightness until it is, show the ratio in a comment).
  Expose in `tailwind.config.ts` (`raised`, `border-strong`, `faint-foreground`). Theme `::selection`, scrollbar colours and caret from tokens.
- [ ] Remove the warm radial glow on `body` (index.css:57) — it is a template tell; flat `--background`.
- [ ] One "selected" vocabulary, used everywhere listed above: a `selected` class recipe exported from `src/lib/selected.ts`
  (e.g. `export const selectedClass = "bg-raised text-foreground border-border shadow-[inset_0_-2px_0_0_hsl(var(--primary))]"`
  for horizontal controls and an `inset 2px 0 0 0` variant for vertical lists). `Segmented` and `NumberPresets` stop using the
  Button `default` variant for the selected option. Tabs: `text-foreground` + amber underline, no amber text. Rank numbers: neutral.
  Badges: `default` badge becomes neutral; an `accent` badge variant keeps amber for the rare case that needs it.
- [ ] Vitest: `src/test/selected-states.test.tsx` renders Segmented and NumberPresets with a selected option and asserts the
  selected button has `aria-pressed="true"` and does NOT carry `bg-primary`; Tabs selected trigger does not carry `text-primary`.

## Task 1.2: One page header, one back link

- [ ] `PageHeader`: drop the icon tile entirely; `icon` prop removed. Signature: `{ title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; className?: string }`.
  Update every caller; delete the `[&>div>span]:hidden` hacks; settings.tsx and issue.tsx use PageHeader.
- [ ] `BackLink` label = the destination's page name: "Rows", "Users", "Runs", "Run #N", "Settings". Arrow icon + label, same style everywhere.
- [ ] Page titles never show raw `{library_name}`-style braces; where a row name is a title, render placeholders with the
  existing chip component (find it: row-name template rendering in the Rows list).
- [ ] Vitest `src/test/page-header.test.tsx`: renders title/subtitle/actions; no element with `aria-hidden` icon tile.

## Task 1.3: Rail and routes

- [ ] Nav order: Dashboard · Rows · Users · Privacy · Runs · Requests · Activity · Settings (lucide icons: gauge, rows-3, users,
  shield-check, list-checks, inbox, activity, settings). Privacy shows a warning dot when any account is `refused_by_plex` or
  otherwise not hiding (use `usePrivacyStatus()`; no extra request if it is already cached; staleTime ≥ 60s). Requests renders
  dimmed (muted text, still clickable) when request sources are off (find the settings/requests-enabled query the app uses).
  Rows/Users show counts if the data is already in the query cache; otherwise no count.
- [ ] Active item: `selectedClass` vertical variant (raised surface, 2px amber inset edge), never filled amber.
- [ ] Bottom block (owner change): Help & docs, Have an issue?, **Star on GitHub, Buy me a coffee — both visible links in the rail**,
  account, version. Same in `mobile-navigation.tsx`.
- [ ] Routes in `App.tsx`:
  - `/privacy` renders today's Sharing page (rename the page title to "Privacy"); `/sharing` → `<Navigate to="/privacy" replace />`.
  - `/activity` = new `pages/activity.tsx`: PageHeader "Activity" / "What Shortlist is doing, what it did, and what it changed on Plex.",
    a tab strip Jobs | Log driven by `?tab=` (default jobs), rendering today's Jobs and Logs page bodies (extract their bodies into
    components without their own PageHeader; keep their behaviour byte-identical). `/jobs` → `/activity?tab=jobs`, `/logs` → `/activity?tab=log`,
    `/schedule` and `/tools` → `/activity?tab=jobs`. Phase 4 adds the "Changes on Plex" tab.
  - Every links/navigate in the app that points at /sharing, /jobs or /logs points at the new route.
- [ ] Code splitting (app-perf): every page route `React.lazy` + one `<Suspense>` with the existing skeleton component as fallback;
  `vite.config.ts` `build.rollupOptions.output.manualChunks` splitting react/react-dom/react-router, @tanstack, recharts (if present)
  and lucide into vendor chunks. Report main-chunk size before and after (`pnpm -C web build`, `ls -la dist/assets`).
- [ ] Favicon: `index.html` emoji favicon → `/favicon.svg` (exists in `public/`).
- [ ] Tests: update `src/test/mobile-navigation.test.tsx` for the new items (Star and Coffee present as links); new
  `src/test/routes-redirects.test.tsx` asserting `/sharing`, `/jobs`, `/logs` land on `/privacy`, `/activity?tab=jobs`, `/activity?tab=log`.
  Update the e2e label/URL strings listed above (renamed labels only; this is the expected test edit).

## Task 1.4: Verify

- [ ] `pnpm -C web test -- <each touched test file>` during the loop; at the end `pnpm -C web test`, `pnpm -C web exec tsc -b --force`,
  `pnpm -C web lint`, `pnpm -C web build`.
- [ ] Screenshot check: build the SPA, boot the app on :5960 against fake Plex using the harness pattern in
  `tests/e2e/conftest.py` (see `tests/e2e/test_screenshots.py` for a capture example; a temporary test file under tests/e2e that you
  DELETE afterwards is fine), capture Dashboard, Rows, Users, Privacy, Activity, Settings at 1440 and 390, Read each once, fix
  in one batch, recapture once. Report the PNG paths.

Do not commit.
