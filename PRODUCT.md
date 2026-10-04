# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The Plex **server owner** (a homelab admin) is the only person who uses the Shortlist web UI. They sit at a
desk, mostly on a desktop browser: once during setup, then occasionally to check what last night's run did,
fix a connection, add or edit a row, or press "Run now". Phone use is occasional, not the primary scene
(confirmed 2026-10-03).

The owner's **shared users** and **managed users** never see this UI. They experience Shortlist only as a
"Picked for You" row on their own Plex Home. Everything the admin does here is on their behalf, and the
admin cannot see what those people see without switching to a non-owner account.

## Product Purpose

Shortlist gives every person on a Plex server a private, personal recommendation row on Plex Home, built from
their own watch history and refreshed nightly. One Docker container: FastAPI backend, React SPA, SQLite,
and a pure-Python engine.

Success for the admin: the nightly run completed, every enabled person got a row, nobody can see anyone
else's row, and when something went wrong the UI says exactly what, for whom, and what to do.

## Positioning

The only maintained open-source tool with genuinely private per-user rows. Comparable tools (Curatarr,
Immaculaterr) are dormant or closed, or build one global row. Shortlist's privacy is structural, not a
setting: rows are hidden from everyone else via Plex share-filter excludes BEFORE they are promoted to
Home, so a row is never briefly visible to the wrong person. This became possible only after Plex fixed
label-restriction behaviour in PMS 1.43.2 (May 2026).

## Operating Context

- Runs as one container on the admin's server; the engine runs on its own nightly schedule (APScheduler).
- Admin signs in once with Plex (PIN flow, no passwords), completes a 7-step setup wizard, picks which users
  get rows, and leaves it.
- Each run: read each person's watch history from the PMS, fetch candidates (TMDB similar/recommendations,
  Trakt, optional web search), optionally ask an AI to suggest titles through web search, write a per-user Plex collection,
  merge privacy excludes, then promote. Optional: auto-request great picks not in the library via
  Radarr/Sonarr/Seerr.
- Coexists with Kometa, Agregarr and other tools that manage collections on the same server; it must touch
  only collections it created.
- Admin checks results in the UI: Dashboard, Runs (per-user timeline, diffs, warnings), Logs (live SSE),
  Sharing (share filters and snapshots), Requests queue.
- Writes to Plex are the risky part; everything supports dry-run and uninstall restores snapshots.

## Capabilities and Constraints

- Candidate sources: TMDB, Trakt, optional web search (Exa/SearXNG) or offline.
- Curation: Anthropic, OpenAI, Google (admin's own key), local Ollama/llama.cpp, or heuristic-only with no
  AI key.
- Seasonal rows: built-in holidays plus custom dates, hidden between seasons.
- Cold start: people with little watch history get a "Popular on <server>" row.
- Plex limits that shape the UI and copy: the owner's Home shows every user's row (owner cannot be
  restricted); no per-user row position control; rows update in place; a TV library write can take ~16s per
  collection, so a run is long by design.
- Every state change is audited (structured events) and must be answerable from the UI.
- Settings live in the DB; the UI is the configuration surface.
- Frontend stack is fixed: React 19, Vite, TypeScript strict, Tailwind, shadcn/ui, TanStack Query, API
  types generated from OpenAPI. See `.claude/rules/frontend.md`.

### Terminology

**row** (a collection on Home) · **pick** (a title in a row) · **run** (one nightly engine execution) ·
**job** (a scheduled or manual run request) · **share** (an invited user with their own token) ·
**managed user** (a Plex Home profile without its own token) · **owner** (the server admin; never
restricted) · **share filter / restriction** (Plex's hide-by-label mechanism) · **snapshot** (a person's
pre-Shortlist filter state, restored on uninstall) · **dry run** (a run that logs would-be writes).

### Decided

- **Scope of the 2026-10 design update** (owner, 2026-10-03): the whole audit, under direction A "amber,
  tightened" for the app and "show the row" for the website. Built and shipped on `dev` 2026-10-03/04.

## Brand Commitments

- Name: **Shortlist**. Fixed.
- Service glyphs (Plex, Claude etc.) are hand-drawn in `web/src/components/brand-glyphs.tsx`.
- **The mark is decided (owner, 2026-10-03):** the Plex-gold rounded square with two drawn sparkles, flat
  (no gradient, no glow). The same drawing is the app's rail mark, the browser-tab icon and the website logo.

- Voice (from `.claude/docs/shortlist-design.md`): plain English; controls say exactly what happens; errors
  say what went wrong and how to fix it, never raw codes; constraints are stated up front; every pick carries
  a "Because you watched X" reason.

## Evidence on Hand

- In production on the maintainer's own server (40+ users, PMS 1.43.x); v1.9.3 released 2026-09-27.
- Public docs site (docs/ in repo, 15+ pages) and MIT GitHub repo with CI (ruff, pytest, Playwright e2e).
- A real Plex Home screenshot in README.md.
- `tests/fakes/fake_plex.py` lets the whole app run with no real Plex server, so any screen can be
  rendered and screenshotted locally (`bash scripts/devrun.sh` on :5960, `pnpm -C web dev` on :5173).
- No testimonials, user counts beyond the maintainer's server, or benchmarks. Do not invent any.

## Product Principles

1. **Privacy is the product.** Any screen that touches who can see what must make the consequence legible
   before the write happens.
2. **The admin acts on behalf of people they cannot see as.** Surface what each person will experience,
   and say plainly when the owner's own view is not representative.
3. **A run is the unit of trust.** Last night's result, per person, with what changed and why, is the
   question the UI exists to answer.
4. **Say the constraint, not the apology.** Plex limits are facts; state them where the admin meets them.
5. **Operate, not persuade.** This is a tool used a few minutes at a time; scanability and consistency
   outrank expression. Brand lives in details.

## Accessibility & Inclusion

Project rule (`.claude/rules/frontend.md`): real `<button>`/`<label>` elements, visible `:focus-visible`,
respect `prefers-reduced-motion`, every data view handles loading/error/empty/success. Desktop-first per
the confirmed usage scene, but layouts are verified at 320/1024/1280 as well as the wide desktop width.
