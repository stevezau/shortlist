# Shortlist

A private, AI-curated "Picked for You" row for every user on a Plex server. One Docker container:
FastAPI backend + React SPA + SQLite, with a pure-Python engine (per-user watched set read from the
PMS via each share's server token → TMDB similar-titles → LLM curate/explain → per-user Plex
collection + label-restriction privacy).

**Status: beta.** In production on the maintainer's server: the FastAPI server runs the engine on
its own nightly schedule (APScheduler), with the React SPA and Docker
packaging. Read these
before any feature work:

- [.claude/docs/shortlist-design.md](docs/shortlist-design.md) — product/UX design (wizard, screens, engine, privacy system)
- [.claude/docs/shortlist-architecture.md](docs/shortlist-architecture.md) — repo layout, DB schema, API surface, testing, CI, phases

Personal deployment details (Steve's servers) live in `CLAUDE.local.md` — gitignored, never commit
it, and never leak environment-specific hostnames/IPs/paths into the public repo or docs.

## Commands

```bash
# Backend
pip install -e ".[dev]"
pytest                       # unit+integration, parallel, coverage (target ≥80%)
pytest -m e2e                # Playwright vs built image + tests/fakes/fake_plex.py
ruff check . --fix && ruff format .

# Frontend
pnpm -C web install
pnpm -C web dev              # Vite dev server (proxies /api to :5959)
pnpm -C web test             # vitest
pnpm -C web build

# Run (dev) — module-level `app` only exists when SHORTLIST_CONFIG is set
SHORTLIST_CONFIG=./devconfig uvicorn --factory shortlist.server.main:create_app --reload --port 5959

# Docker
docker build -t shortlist:dev .   # multi-stage: node web build → python runtime
```

## Row privacy (leak-safe ordering)

Rows are made private by share-filter excludes: each account's `filterMovies`/`filterTelevision`
gets `label!=shortlist_<otheruser>` for every row that isn't theirs. The ordering is what keeps it
leak-safe — rows are delivered UNPROMOTED, all filters merged, and only then promoted, so a new row
is never visible before the exclusions that hide it exist.

The old automatic Privacy Check + write gate that _verified_ this before each write was **removed at
the owner's request** (2026-07-16). Writes are no longer gated on a recorded check; the hiding still
happens, but nothing verifies it after the fact — see `.claude/rules/plex-safety.md`.

## Architecture (the one contract that matters)

```
shortlist/engine/    pure library — NO imports from shortlist/server/; takes config dataclasses + clients, returns reports
shortlist/server/    FastAPI + SQLAlchemy/Alembic + APScheduler + SSE; the thin adapter over the engine
web/              React + Vite + TS + Tailwind + shadcn/ui; API types generated from OpenAPI
tests/fakes/      fake_plex.py — stub PMS + plex.tv; e2e runs the full wizard with no real server
```

See shortlist-architecture.md §2–§4 for the full tree, DB schema, and API surface.

## Working economically

Long sessions are the single biggest cost: every turn re-sends the whole conversation. So:

- **Say when a `/clear` is due.** When the next request is a genuinely new task — a different
  feature, a different bug, a different area — say so in one line before starting, and let the owner
  decide. Don't nag mid-task; the cost of losing context you still need is higher than the tokens.
- **Test what you changed, not everything, while iterating.** `pytest tests/unit/test_foo.py` (or
  `-k`) during the edit loop; the FULL suite once before committing, plus `pnpm test`/`pnpm build`
  when web files changed and `-m e2e` when a UI flow or the wizard changed. CI runs everything
  regardless, so a green full suite immediately before the commit is the bar — not after each edit.
- **Don't re-verify what a tool already told you.** No re-reading a file you just wrote, no re-running
  a suite after a formatting-only change, no full-suite run to confirm a docs edit.
- **Keep tool output small**: `-q`, `| tail`, targeted `grep`/`sed -n` over dumping whole files.
- **Say when a cheaper model would do.** Before starting a chunk of work, if it is mechanical —
  renames, updating test fixtures to a changed signature, docs, log wording, copy, dependency
  bumps, boilerplate — suggest `/model sonnet` in one line and carry on. Stay on the strong model
  without asking for: diagnosis, anything privacy/migration/identity-shaped, design decisions,
  reviewing someone else's assumptions, and debugging behaviour that isn't yet understood.
- **Run subagents on the cheapest model that can do the job** (`model:` on the Agent tool). Mechanical
  fan-out — grepping, collecting, applying a known edit across many files — is `haiku` or `sonnet`.
  The Architecture Review agent stays on the strong model: its value is catching assumptions nobody
  questioned (it found `immutable=1` unsafe on a WAL database and an episode/show key-space
  mismatch, neither visible without real reasoning). Make it RARE, not cheap.
- **Answer at the length the question deserves.** A yes/no question gets a yes/no and the one caveat
  that matters. Save the long write-up for a diagnosis, a design decision, or something that went
  wrong. Never re-explain what was just said.
- Prove a test has teeth by breaking the code when the logic is **risky or subtle** — not for every
  test. Never use `git checkout <file>` to undo it: that wipes uncommitted work (done twice). Copy to
  a backup first, or re-apply by hand.

## Code style

- `ruff format` / `ruff check` (config in pyproject.toml), 120-char lines, 4-space indent
- Type hints on all params and returns; modern annotations (`list[str]`, not `typing.List`)
- Google-style docstrings on public APIs
- Logging: `from loguru import logger` — never stdlib `logging`
- Imports: stdlib → third-party → local
- Frontend rules: `.claude/rules/frontend.md`

## Conventions

- **Branch model** (mirrors media_preview_generator): `dev` is the default/working branch — commit
  and push here; every green `dev` push publishes `ghcr.io/stevezau/shortlist:dev`. `master` is the
  stable branch, advanced only by promoting `dev` → `master` via PR at release time. Releases are cut
  by tagging `vX.Y.Z` (CI builds `:latest` + `:X.Y.Z`). Publishing is gated on lint+tests+e2e green.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `chore:`)
- **Architecture Review — by risk, not by habit.** The agent
  (`.claude/agents/architecture-review.md`) costs ~100k tokens a run, so spend it where bugs are
  expensive. Dispatch it, and block on HIGH findings, when the diff:
  - touches **privacy, share filters, restrictions, or anything writing to Plex/plex.tv**;
  - adds or changes an **Alembic migration**;
  - touches **auth, secrets, or tokens**;
  - reads or writes **watch history / user identity** (mapping one person's data onto another);
  - **reads an external system's storage directly** (e.g. the PMS database);
  - is a **`dev` → `master` release PR**, whatever it contains.

  Skip it for UI-only, docs, logging, comments, test-only, and dependency-bump commits — CI and the
  test suite already cover those, and a review there finds style, not bugs.

  NOT "before the PR" alone: a `dev` push deploys to the maintainer's server and to every `:dev`
  user, so risky code is already live by then. The 0032 migration that was a no-op on every real
  database sat on `dev` for days before any PR existed.

- Settings live in the DB (`settings` table via `settings_store`); env vars are one-time seeds
  migrated on first boot (infrastructure vars like `PORT`, `TZ`, `PUID/PGID`, `APP_BASE_PATH` stay live)
- Every schema change ships an Alembic migration
- Docs follow `.claude/rules/docs.md` — README/docs/reference updated in the same PR as the feature

## Security & Plex safety (non-negotiable)

`.claude/rules/plex-safety.md` governs every code path that writes to Plex or plex.tv. Highlights:
snapshot before restriction writes; share filters are read-modify-write MERGES, never rebuilt;
never touch collections/labels Shortlist didn't create (Kometa coexistence); owner never restricted;
tokens encrypted at rest, never logged; everything supports `--dry-run`.

## Key dependencies

Python 3.12 (the Docker runtime; the only version CI tests) | FastAPI | SQLAlchemy 2 + Alembic | APScheduler | plexapi | httpx | loguru | Pydantic v2
| React 18 + Vite + TypeScript + Tailwind + shadcn/ui | pytest (+xdist, hypothesis) | Playwright
