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

  Ignore GitHub's "`master` is N commits ahead of `dev`". Every PR merge mints a merge commit that
  lives only on the base branch, so that counter can never read 0 and is not drift. The real check
  is content: `git fetch origin && git diff --quiet origin/master origin/dev`.
