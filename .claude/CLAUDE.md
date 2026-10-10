# Shortlist

A private, AI-curated "Picked for You" row for every user on a Plex server. One Docker container:
FastAPI backend + React SPA + SQLite, with a pure-Python engine (per-user watched set read from the
PMS via each share's server token → TMDB similar-titles (+ optional AI web search) → ranking and reasons in code → per-user Plex
collection + label-restriction privacy).

**Status: 1.x (run `git tag` for the current version).** In production on the maintainer's server: the FastAPI server runs the engine on
its own nightly schedule (APScheduler), with the React SPA and Docker
packaging, plus optional MCP assistant access (`shortlist/server/assistant*`, off unless
`SHORTLIST_MCP_URL` is set). Read these before any feature work:

- [.claude/docs/shortlist-design.md](docs/shortlist-design.md) — product/UX design (wizard, screens, engine, privacy system)
- [.claude/docs/shortlist-architecture.md](docs/shortlist-architecture.md) — repo layout, DB schema, API surface, testing, CI, phases
- [.claude/docs/jobs-and-runs-design.md](docs/jobs-and-runs-design.md) — runs vs. jobs, the durable
  queue, the delivery ledger, and §12's mutation audit: every state change and whether it actually
  reaches Plex. **Read §12 before touching any handler that changes who can see what** — it is the
  register of a recurring bug class in this codebase.

Personal deployment details (Steve's servers) live in `CLAUDE.local.md` — gitignored, never commit
it, and never leak environment-specific hostnames/IPs/paths into the public repo or docs.

## Commands

```bash
# Backend
pip install -r requirements.lock && pip install -e ".[dev]"   # lock first: same versions the image ships
pytest                       # unit+integration, parallel, coverage (target ≥80%)
pytest -m e2e                # Playwright vs an in-process app (uvicorn + built SPA) + tests/fakes/fake_plex.py
ruff check . --fix && ruff format .

# Regenerate requirements.lock — REQUIRED whenever pyproject's dependencies change.
# Resolves for the image's interpreter/OS, not your laptop's, so it is safe to run from macOS.
# CI's lint job fails if pyproject declares a dependency the lock doesn't carry.
uv pip compile pyproject.toml \
  --extra anthropic --extra openai --extra google --extra posters \
  --python-version 3.12 --python-platform linux \
  --output-file requirements.lock

# Frontend
pnpm -C web install
pnpm -C web dev              # Vite dev server on :5173; proxies /api to :5959 by default
SHORTLIST_API_PROXY=http://localhost:5960 pnpm -C web dev   # ...or to a scripts/devrun.sh backend
pnpm -C web test             # vitest
pnpm -C web build

# Run (dev) — throwaway config, safe mode, port 5960. Refuses to start if a running container
# already mounts that config dir (two schedulers on one DB duplicates real Plex writes).
bash scripts/devrun.sh
# Overridable: PORT=... SHORTLIST_CONFIG=... SHORTLIST_DRY_RUN=0 bash scripts/devrun.sh
# Under the hood — module-level `app` only exists when SHORTLIST_CONFIG is set:
SHORTLIST_CONFIG=./devconfig uvicorn --factory shortlist.server.main:create_app --reload --port 5960

# Docker
docker build -t shortlist:dev .   # multi-stage: node web build → python runtime
```

## Row privacy (leak-safe ordering)

Rows are made private by share-filter excludes: each account's `filterMovies`/`filterTelevision`
gets `label!=shortlist_<otheruser>` for every row that isn't theirs. The ordering is what keeps it
leak-safe — rows are delivered UNPROMOTED, all filters merged, and only then promoted, so a new row
is never promoted before the exclusions that hide it exist. Unpromoted is not invisible: a row is listed
in the library's Collections tab until its exclude is on an account's filter, so a person's FIRST row — and a
new shared row, from everyone outside its audience — is excluded as soon as it is written, before any other
row is — unless plex.tv fails, when the end-of-run merge does it (plex-safety rule 1).

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

- **Write the test first, then run only that test** (owner decision 2026-09-12). What made testing
  feel like it set the pace of development was running the WHOLE suite mid-edit. Writing the test
  before the code fixes that: it gives you one file to run, and one file is ~3.5s.
  So, during the edit loop: `superpowers:test-driven-development`, then
  `pytest tests/unit/test_foo.py` or `-k <name>` — that file, nothing else. Never `pytest` bare,
  never `-m e2e`, never the whole `pnpm test`, until the work has settled.
  At the END, once: `pytest`, `pnpm test`, `tsc -b --force`, `eslint .`, plus `-m e2e` when a UI flow
  or the wizard changed. That pass is the bar before any commit — CI runs it all regardless, so a
  green full pass immediately before the commit is what counts, not a green one mid-edit.
- **One pytest at a time on this host.** Several agent sessions share it, and each run fans out to
  its xdist workers (`-n 2` by default) — four overlapping runs is four times that, which is the
  shape that took the plex host down (2026-09-12: 189 workers, ~30 GB into swap). A `PreToolUse` hook
  (`.claude/hooks/pytest-serialize.sh`) denies a `pytest` command while another one is in flight; when
  it fires, wait and retry rather than working around it. Scoped runs stay cheap and stay encouraged —
  it is the OVERLAP that costs, not the frequency.
- **Keep backend test scratch on disk.** The test setup defaults to a stable per-worktree
  directory under `/var/tmp`, rejects RAM-backed locations and explicit `--basetemp`, and retains
  at most two completed failed runs. Set `PYTEST_DEBUG_TEMPROOT` to choose another disk-backed
  parent. Ordinary runs use two workers. Never reuse or delete another active run's scratch.
- **Close test database resources.** Use `tests.db_helpers.disposing_engine` around engine setup
  and fixture `yield`, including each property-test example. SQLite's connection context manager
  only commits/rolls back; use `contextlib.closing` as well to close it. `tests/resources.py` fails
  Linux tests that leave database files open in their temporary directory, and collects unreachable
  metadata every 50 tests.
- **Don't re-verify what a tool already told you.** No re-reading a file you just wrote, no re-running
  a suite after a formatting-only change, no full-suite run to confirm a docs edit.
- **Architecture Review is the only strong-model subagent** (a hook blocks other Opus/Fable subagents). It found
  `immutable=1` unsafe on a WAL database and an episode/show key-space mismatch; make it RARE, not cheap.
- Prove a test has teeth by breaking the code when the logic is **risky or subtle** — not for every
  test. Never use `git checkout <file>` to undo it: that wipes uncommitted work (done twice). Copy to
  a backup first, or re-apply by hand.

## Text the owner is going to paste somewhere

Issue replies, release notes, PR bodies, forum posts — anything written to be copied out of the
terminal:

- **Never use a markdown blockquote (`>`).** The terminal renders it as a leading vertical bar on
  every line, and that bar comes along with the selection — so the paste has to be cleaned up by
  hand, line by line. Put copy-out text in a fenced code block instead, which selects cleanly and
  keeps the markdown intact for GitHub.
- Same reason to avoid tables and hard-wrapped prose in that text: reflow it once and it's ruined.
- Say what the person on the other end has to DO, in its own short paragraph. A diagnosis they
  can't act on is a status update, not a reply.

## Code style

- `ruff format` / `ruff check` (config in pyproject.toml), 120-char lines, 4-space indent
- Type hints on all params and returns; modern annotations (`list[str]`, not `typing.List`)
- Google-style docstrings on public APIs
- Logging: `from loguru import logger` — never stdlib `logging`
- Imports: stdlib → third-party → local
- Frontend rules: `.claude/rules/frontend.md`

## Conventions

- **Branch model**: `dev` is the working branch — commit and push there; every green `dev` push publishes
  `ghcr.io/stevezau/shortlist:dev`. `master` advances only by a `dev` → `master` PR at release time; release
  tags `vX.Y.Z` go on `master`. Never commit straight to `master`. A `master` push runs nothing. Before
  promoting, tagging, or touching branch protection, read `.claude/docs/release-and-branching.md`.
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

  Run it before the push, not just before the PR: a `dev` push deploys to the maintainer's server and every `:dev` user.

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
| React 19 + Vite + TypeScript + Tailwind + shadcn/ui | pytest (+xdist, hypothesis) | Playwright
