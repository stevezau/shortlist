# The implementation standard

**Stated by the owner, 5 Sep 2026. Binding on every item in
`.claude/docs/audit-2026-09-programme.md` and on every agent working on it.**

The owner's exact words: *"It just needs to be super high quality. I need you to do several audits
and reviews on literally everything that you do. Find all bugs, find all issues. Don't bring in any
regressions and issues... You often seem to forget my instructions so make sure you do it all, do it
correctly, and do it right."*

That last sentence is why this file exists. Read it before starting work, and again before claiming
anything is done.

## The bar

1. **Code quality is great.** Match the surrounding idiom. Comments explain WHY, never WHAT
   (`.claude/rules/commenting.md`). Type hints everywhere. Google-style docstrings on public APIs.
2. **The UX design is amazing, and so is the layout.** Not "functional". Every data view handles
   loading / error / empty / success. Copy follows the house voice: plain English, controls say
   exactly what happens, errors say what went wrong AND how to fix it. Test at 320 / 1024 / 1280,
   not just 390 and 1440. Never reason about layout — measure it.
3. **Do not complicate code that does not need it.** Simple and readable beats clever. If a fix is
   one line, it is one line — do not build an abstraction around it.
4. **Reuse where possible.** Before writing anything new, look for the thing that already does it.
   The audit designs found this repeatedly: `QueryBoundary` already existed for `four-states`, the
   seed-from-settings pattern already existed for `wizard-dataloss`, the job queue already exists for
   notification retry, `resolve_outcomes` already exists for interleaving attribution. A new
   abstraction that duplicates an existing one is a defect, not a feature.
5. **Test everything, and prove the tests have teeth.** Tests written FIRST where logic is
   non-trivial. Run them against the UNFIXED code and confirm they fail for the RIGHT reason. Assert
   the kwargs the SUT controls, not that a call happened. Cover the matrix, not one cell.
6. **No regressions.** Full `pytest`, full `vitest`, `tsc -b`, AND `eslint .` before every commit —
   `eslint .` is part of CI's bar and `react-hooks/set-state-in-effect` is an ERROR here; a green
   vitest + tsc is NOT enough (learned the hard way in `1179be2`).
7. **Several audits and reviews on everything.** Not one pass. Re-review until a pass finds nothing:
   in a previous round, 7 of 13 findings were introduced by the fixes themselves.
8. **Prove it live** — on the running app, and for anything privacy-shaped from a NON-OWNER account,
   because the owner sees everything and owner-side checks prove nothing.

## Verify with CI's exact commands

```bash
.venv/bin/python -m pytest -q --no-cov          # full suite
/opt/homebrew/bin/ruff format <files> && /opt/homebrew/bin/ruff check shortlist/ tests/
cd web && ./node_modules/.bin/vitest run        # pnpm is NOT on PATH
cd web && ./node_modules/.bin/tsc -b            # catches what vitest cannot
cd web && ./node_modules/.bin/eslint .          # CI runs this; errors fail the build
cd web && ./node_modules/.bin/vite build
```

## Standing rules that outrank speed

- **Never push to `dev` without the owner saying so.** A `dev` push publishes `:dev` and watchtower
  recreates the maintainer's live container within ~4h. Pushing is a production deploy to a server
  40 people use.
- **Ask before live Plex writes.** Deploying is authorised; MUTATING real accounts' shares or
  collections is not. `--dry-run` first, always.
- **Stage explicitly, never `git add -A`.** The owner edits this repo concurrently and other agent
  sessions may be running.
- **Never commit `CLAUDE.local.md` details** — hostnames, IPs, personal paths — into the public repo.
- **The dossier stays outside the repo.** It holds unpublished third-party security findings.
- **A pick is not a contract.** If research changes the verdict on an item, record it in the
  programme file's changed-verdicts register and say so — do not build a fix for a problem that
  turned out not to exist, and do not silently drop one either.

## Definition of done, per item

1. Premise verified against real code (the audit was wrong about several).
2. Tests written first where non-trivial, proven to fail before the fix.
3. All six checks above green.
4. Committed with a message that says what was wrong, what changed, and what was deliberately NOT
   changed.
5. Programme file updated: status, and any changed verdict.
