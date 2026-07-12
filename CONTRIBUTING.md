# Contributing to Rowarr

Thanks for considering it! Rowarr is a small, safety-critical codebase — it modifies other
people's Plex views — so the bar for write-path changes is deliberately high.

## Dev setup

```bash
pip install -e ".[dev]"          # backend
pnpm -C web install               # frontend
pytest                            # unit + integration (no network, ever)
pnpm -C web test && pnpm -C web build
ruff check . --fix && ruff format .
uvicorn rowarr.server.main:app --reload --port 5959   # with ROWARR_CONFIG=./devconfig
pnpm -C web dev                   # Vite on :5173, proxies /api to :5959
```

## The rules that matter

1. **`rowarr/engine/` never imports from `rowarr/server/`.** The engine is a pure library.
2. **Read `.claude/rules/plex-safety.md` before touching any code path that writes to Plex
   or plex.tv.** Highlights: snapshot before restriction writes; share filters are
   read-modify-write merges, never rebuilt; only `rowarr_*`-labeled collections may be
   touched; every write path takes `dry_run`; tokens never in logs or exceptions.
3. **Tests are required.** No test may touch the network — use the conftest fixtures,
   recorded fixtures in `tests/fixtures/`, or `tests/fakes/fake_plex.py`. Privacy/merge code
   changes need property tests.
4. **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
5. **Docs ship with the feature** — README/docs updated in the same PR.

## Reporting bugs

Use the issue templates. For anything privacy-related (a user saw a row that wasn't
theirs), please mark it clearly — those get fixed first, always.
