# Design refresh programme (Impeccable audit, approved 2026-10-03)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Each phase below
> has its own task list; build phases in order, except the two website phases, which touch only `docs/`
> and may run beside the app phases.

**Goal:** ship every item the owner approved on the review page
(https://claude.ai/artifact/21Dm5LWioZaQh5Prk6ChGZ, collection `decisions`): 29 approved, 1 approved with a change.

**Spec:** the review page's cards + the mockups in `.impeccable/mocks/` (HTML, shared `shortlist.css`,
renders in `.impeccable/mocks/renders/`) + the direction contracts in `.impeccable/surfaces/` + `PRODUCT.md`.
The mockup is the visual contract; the current source is the behavioural contract. A mockup never
licenses a behaviour change the card did not name.

## Global constraints

- Visual system (app): Source Sans 3 UI face with tabular numerals; JetBrains Mono only for code, cron, log lines and filter strings (ligatures off for `!=`). One page header: title, one line, actions, NO icon tile. Exactly one filled-amber (`variant="default"`) button per screen. Selected states (nav, tabs, segmented, preset chips, filter chips) are a raised neutral surface (`--raised: 240 6% 15%`) with a 2px amber edge, never filled amber. One card depth: never a card inside a card. The sparkle is the drawn mark, never the ✨ emoji, in app chrome (user-authored row names keep whatever the user typed).
- Nav order: Dashboard · Rows · Users · Privacy · Runs · Requests · Activity · Settings. **Owner change (app-nav):** "Star on GitHub" and "Buy me a coffee" stay visible in the rail's bottom block, not behind an About/sub-menu.
- Dark-only stays. No light theme this round.
- Copy: plain English; controls say what happens; "Runs on…" for schedule, "Titles refresh every…" for cadence; the word "rebuild" never means both. User states: On / Paused / Off; "Restricted" is a separate neutral pill.
- Privacy semantics are owned by the engine. The UI and API only REPORT them. Nothing in this programme writes to Plex differently.
- Tests: write the test first; run only that file during the loop; one pytest at a time (hook). Full pass at the end: `pytest`, `pnpm -C web test`, `pnpm -C web exec tsc -b --force`, `pnpm -C web lint`, `pytest -m e2e` (UI changed), `python scripts/build_llms_full.py` after docs changes.
- API types are generated: `pnpm -C web gen:api` after any schema change; never hand-write response types.
- Never edit a test to match new behaviour unless the card changed that behaviour; renamed labels are the only expected test edits, and each one is listed in its task.
- Do not commit. The owner reviews the working tree first (global rule: ask before committing).

## Review focus (inputs no task's happy path exercises)

1. A run with NO privacy measurement (`unhideable_measured` false, older runs, dry runs): must read plain OK, never "with warnings" and never "0 of N private".
2. A server with zero enabled people / zero rows: dashboard, privacy strip and run detail render a designed empty state, no `NaN`, no "0 of 0 hide every row" presented as a warning.
3. Long names (40-char username, 60-char row name with placeholder chips) at 390px: no horizontal page scroll; text wraps or truncates with a title.
4. Old bookmarks: `/sharing`, `/logs`, `/jobs`, `/settings#…` still land on the right new page (redirects), and the docs' old URLs (merged SEO pages, split Rows guide anchors) redirect.
5. Keyboard: every new tab strip, jump list and overflow menu is reachable and operable with Tab/Enter/Escape; focus-visible shows.

## Phases

| Phase | Cards | Plan |
|---|---|---|
| 1 Visual system + nav + header | app-system, app-nav | `2026-10-03-phase1-visual-system.md` |
| 2 Run privacy + dashboard | app-run-privacy, app-dashboard, app-dashboard-empty | `2026-10-03-phase2-run-privacy.md` |
| 3 Website landing + comparison + plumbing | site-landing, site-comparison, site-cta, site-a11y, site-schema, reach-analytics, reach-feed, reach-llm, reach-links | `2026-10-03-phase3-site.md` |
| 4 App structure | app-row-edit, app-rows, app-settings, app-privacy, app-users, app-activity | `2026-10-03-phase4-app-structure.md` |
| 5 Docs template + splits + SEO | site-docs, site-seo | `2026-10-03-phase5-docs.md` |
| 6 Small app items | app-wizard, app-uninstall, app-trace, app-rowtrace, app-mobile, app-perf | `2026-10-03-phase6-small.md` |

Owner-only (cannot be done by an agent): reach-gsc (needs `op-login` for the Google session),
reach-analytics site code (owner creates the GoatCounter site; the build ships the tag behind an empty
`goatcounter:` key), reach-windows (posting on Reddit/forums is the owner's).

## Finish

Impeccable finish: one batched screenshot round (desktop 1440 + mobile 390) of every changed screen
against fake Plex, `impeccable detect --json` on `web/src` and `docs/_layouts docs/_includes`, then the
`impeccable:impeccable-finish-reviewer` agent with the direction contracts and mockups, then
`impeccable:impeccable-documenter` to write DESIGN.md. Architecture Review agent on the phase-2 backend diff
(it reports privacy state). Then the full test pass above.
