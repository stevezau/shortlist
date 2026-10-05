# Discussion #138 — handoff: build phases 2–4 (Steve said "do it", 2026-10-04)

You own the rest of the #138 feature. Phase 1 (AI instructions on rows + Settings default) is DONE:
shipped to dev at `43d9d26b`, force-deployed to plex, proven live (dry runs on a throwaway MooHouse row:
"Only pick TV series" → 33/33 AI proposals were shows; default → 13 shows / 8 films).

## Read first (in this order, nothing else up front)
1. `.claude/docs/discussion-138-ai-custom-rows.md` — the design, measurements, Decisions 1–10 (all decided:
   the owner delegated them to the recommendations), and "Where it sits in the redesigned app".
2. Mockups (design canvas, boards B1–B5, C1–C2): https://claude.ai/artifact/Y8CMBv8UQYbVTgFmE8cnwC — read with
   the Artifact tool only if a UI detail is unclear; the doc is the authority.
3. `.claude/docs/plans/2026-10-03-ai-instructions-phase1.md` — the phase-1 plan, as a format/quality template.
4. `DESIGN.md` (app design rules) and the project `.claude/CLAUDE.md` + `.claude/rules/*`.

## What's left
- **Phase 2 — Length, year and rating limits on every row** (Decision 5): "What goes in" gains Max length
  (minutes), Released between (years), Minimum rating; all default OFF (today's behaviour). Engine filters
  candidates (runtime via TmdbClient.details, cached 7 days; year/rating from discover data). Row recipe gets a
  part ONLY when a limit is set (byte-identical otherwise — copy phase 1's pattern and its pinned-recipe test).
- **Phase 3 — The AI row, fixed theme** (Decisions 3, 6, 7, 10): new row type "AI row"; templates "Describe a
  row" and "AI Picks" in the Add-a-row gallery; describe → AI builds ~60 named titles + TMDB tags + limits once
  per theme; Shortlist checks every title against TMDB/library/limits and fills per person every run with NO AI;
  AI-written one-line reason per title + code's personal hook; "Change it" refine with a diff; "Try it" = scoped
  dry run; "AI prompts" section (Build the list / Next theme / Change it — guidance editable, mechanics locked);
  usage + "Pause AI for this row". Reuse custom seasons (#137: `seasons.load_titles`, the `season` source,
  pool_key/recipe patterns, `{season}` naming) — a theme is a dateless custom season. Needs a migration
  (themes table + Collection columns) → **Architecture Review** (`.claude/agents/architecture-review.md`).
- **Phase 4 — Explore mode + over-time controls** (Decision 4): new theme every N days (default per-person theme
  from taste), "Up next" built a day early, theme history (avoid last 6), "How much changes each time" (default
  a third = today), "Don't repeat a title for N days" (default off, from the `picks` table), "Keep out titles
  already in <rows>" (default none). Migration → Architecture Review.
- **Open check from phase 1:** after the first nightly run following the 2026-10-04 deploy (02:30 Sydney), confirm
  NO row rebuilt with decision "settings_changed" (read-only probe: `DOCKER_HOST=ssh://plex docker exec -i
  shortlist python -`, see memory `probe-live-pms-read-only`). If any did, stop and debug before phase 2 ships.
- Fix the design doc's Status line (still says phase 1 "uncommitted") in phase 2's first commit.

## How to work (owner's rules — non-negotiable)
- One phase at a time: `superpowers:writing-plans` → plan in `.claude/docs/plans/` → `superpowers:subagent-driven-development`.
- **Models (global CLAUDE.md):** start at `haiku` (scouts, lookups, running/verifying); `sonnet` for implementers
  working from the plan and for per-task reviews; `opus` ONLY for the one final whole-branch review per phase and
  the Architecture Review. Pass `model:` on every dispatch.
- TDD; during edits run only the task's test files. One pytest at a time on this host (a hook enforces it).
  Full suites once per phase at the end: `pytest`, `pnpm -C web test`, `tsc -b --force`, `eslint .`, build, ruff,
  and `pytest -m e2e` when UI changed (rebuild `web/dist` first).
- Environment: no `python` on PATH in this worktree — use `PYTHONPATH=. /home/data/workspace/shortlist/.venv/bin/python`
  (and `.../.venv/bin/ruff`). `pnpm gen:api` tries localhost:5959 (production) first — regenerate
  `web/openapi.snapshot.json` (command in `tests/unit/test_openapi_snapshot.py`) and generate types from the snapshot.
- Every new setting defaults to today's behaviour; nothing rebuilds on upgrade night unless the owner changed something.
- Commits: Conventional Commits, staged by explicit path (never `git add -A`), ending with the attribution line.
  When a phase is fully green (and its Architecture Review clean for 3/4), merge `origin/dev` in, re-verify, push
  `HEAD:dev`, wait for CI, then prove it live with DRY RUNS on a throwaway MooHouse row (memory `live-run-proof-recipe`;
  phase 1's proof script pattern). Ask Steve before any force-deploy that would interrupt a run, and before ANY live
  Plex write beyond normal nightly runs (memory `ask-before-live-plex-writes`).
- Keep Steve's messages short: lead with the result; bug/fix reports in his Cause/Fix/Proof shape.

## Phase-1 leftovers you may fold into phase 2 (all minor)
- Rows page badge "AI instructions" still shows when a row no longer uses AI web search (`web/src/lib/collections.ts` rowOverrides).
- A row with blank Add/Own text whose web search is then turned off gets a 422 naming a now-hidden field.
- Native "Exactly what's sent" footnote omits the user-message recency line on default/add paths.
- Settings built-in preview query key lacks the backend (brief stale text after switching).
- No byte-for-byte test of the built-in native USER message.
