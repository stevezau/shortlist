---
version: 1
slug: "web-src"
primary_target: "web/src"
related_targets: []
---

# Surface brief: Shortlist admin SPA (web/src)

Scope: refinement of the whole authenticated app. Visitor mode: Operate. Platform: web, desktop-first, dark-only.
Audience: the Plex server owner, at a desk, a few minutes at a time. Job: see what last night did, per person, and whether anyone can see a row that is not theirs; then fix or tune.
Proof/content: real run data, posters, people, reasons. Constraints: React 19 + Tailwind + shadcn; no light theme this round; privacy semantics are owned by the engine, the UI only reports them.

## Direction contract

THESIS: A run is the unit of trust, so every top-level screen opens on last night's run and its privacy state. Refuses the analytics-first dashboard and the "everything is a card in amber" dashboard default.
OWN-WORLD: Near-black warm neutrals (existing tokens kept). Amber is the one action colour: exactly one filled-amber control per screen; selections, active nav and chips become a raised neutral surface with a 2px amber edge. Source Sans 3 as the UI face with tabular numerals, no monospace except code, cron and log lines. The sparkle becomes a drawn vector glyph, never the emoji. One page header (title, one line, actions) with no icon tile. One card depth; never a card inside a card; hairline dividers inside panels. (Applies to app chrome. Row names are the user's literal Plex titles and render exactly as Plex shows them, emoji included.)
STORY: The owner opens the app and within five seconds knows: last run OK or not, next run when, how many accounts hide every row, Plex connected. Then they drill into a person and see the posters they got and why.
FIRST VIEWPORT: Dashboard. Top: a status strip of four facts (last run, next run, privacy, Plex) with one "Run now" (the only filled amber). Then the privacy callout when an account cannot be hidden. Below: Impact as one compact panel (inline stats, window control) and today's summary panels. NO per-person poster strips (owner rejected them, 2026-10-03). Run detail keeps today's row-first layout (owner asked).
FORM: Refinement of the incumbent world; no concept seed (local extension).
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.
