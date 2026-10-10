# Branching, branch protection and release flow

Moved out of `.claude/CLAUDE.md`. Read before pushing to `master`, cutting a release, or touching branch protection / required CI checks.

- **Branch model** (mirrors media_preview_generator): `dev` is the default/working branch — commit
  and push here; every green `dev` push publishes `ghcr.io/stevezau/shortlist:dev`. `master` is the
  stable branch, advanced only by promoting `dev` → `master` via PR at release time. A `master` push
  runs NOTHING: master only advances by merging a PR that just ran the whole workflow on the same
  content, and the release tag points at that very merge commit, so master and the tag were testing
  one identical SHA twice (v1.7.0 ran the full suite four times over one tree before this was cut).
  The two runs that gate a release are the PR and the tag. Releases are cut by tagging `vX.Y.Z` on
  `master` (CI builds `:latest` + `:X.Y.Z` + `:dev`). Publishing is gated on lint+tests+e2e green.
- **Branch protection.** Force-pushes and deletions are blocked on both branches. `master` also
  requires `lint`/`test-python`/`test-web`/`e2e` — which are GATE jobs (`test-python`/`e2e` just
  assert their `*-shard` matrix legs passed), so the shard count can change without touching branch
  protection. Sharding those jobs under their old names is what blocked the v1.7.0 release PR with
  all checks green: the required contexts had been renamed out of existence and nothing noticed,
  because `dev` requires no checks. `dev` has no required checks on purpose — they
  would block the direct pushes that are how you work on it. `enforce_admins` is off on both,
  leaving an override for a genuine emergency.

  Required checks alone do **not** require a pull request, and this file used to claim they did
  ("only advances via a green PR"). They don't: `v1.0.0` was committed straight to `master` on
  2026-08-04 under exactly that config. Commit the release on `dev` and promote it — a commit that
  lands on `master` alone diverges the branches for real, and the next `dev` → `master` PR reverts
  it silently, because `dev` never had it.

  After a release, fast-forward `dev` to `master` (`git push origin master:dev`): the merge commit then
  lives on both branches and GitHub's "ahead/behind" counter reads 0/0. Until then it reads "`master` is N
  commits ahead of `dev`", which is cosmetic (every PR merge mints a merge commit on the base branch). The
  real drift check is content: `git fetch origin && git diff --quiet origin/master origin/dev`.

## Cutting a release

Only the owner decides to release. The checklist, in order:

1. On `dev`: add a dated `## [X.Y.Z]` section to `CHANGELOG.md`. The `release` CI job extracts it for the
   GitHub Release notes and fails the tag build if it is missing.
2. Bump `__version__` in `shortlist/__init__.py` (`pyproject.toml` reads it).
3. Regenerate `web/openapi.snapshot.json` (command in `tests/unit/test_openapi_snapshot.py`) and the two
   generated website files, `python scripts/build_feed.py` and `python scripts/build_llms_full.py`.
4. Open the `dev` → `master` PR, run the Architecture Review on it (a release PR always gets one), and merge
   once CI is green.
5. Tag `vX.Y.Z` on `master`, and wait for the tag build to finish before pushing anything to `dev`: a push of the
   same SHA shares the tag build's concurrency group and can displace it, so `:latest` never publishes.
6. Fast-forward `dev` to `master`.
