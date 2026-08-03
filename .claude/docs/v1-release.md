# v1.0.0 — state of play and what is left

**Status: releasing now.** The owner gave the go-ahead on 2026-08-04. If a step below is already
done, it is marked; if you are picking this up cold mid-release, read this file first.

Written 2026-08-03, updated 2026-08-04.

## What "ready" means here

`dev` is 38 commits ahead of `master`, CI green on every one, and deployed to the maintainer's
server as `ghcr.io/stevezau/shortlist:dev` (migrated to head, health `ok`, no errors on boot).

Already done, do NOT redo:

- `__version__ = "1.0.0"` (`shortlist/__init__.py`), OpenAPI snapshot regenerated to match.
- Beta labels removed: the sidebar footer (`app-shell.tsx`), the README banner, `.claude/CLAUDE.md`
  ("Status: 1.0").
- `CHANGELOG.md`'s `## [1.0.0]` entry is written and DATED 2026-08-04.
- Mobile: every route, the wizard, the nav drawer and all four dialogs fit 390px, enforced by
  `tests/e2e/test_mobile_audit.py`.
- A database seeded at revision `0001` is proven to reach head with its rows intact and no drift
  from the models (`tests/unit/test_migrations.py::TestUpgradingAnOldInstall`) — 1.0 is the version
  people jump to from an old beta, and every other migration test starts empty.
- Four review-backlog items closed; three more were already fixed and their entries were stale
  (verified, not re-fixed). See `review-backlog.md`.

## To release (authorised 2026-08-04)

Per `.claude/CLAUDE.md`'s branch model:

1. ~~Set the `CHANGELOG.md` date.~~ Done.
2. Open a PR `dev` → `master`.
3. **Run the Architecture Review agent on it.** Required for every `dev` → `master` release PR
   whatever it contains, and this one carries watch-history deletion (see below).
4. Merge once `lint` / `test-python` / `test-web` / `e2e` are green — `master` requires all four.
5. Tag `v1.0.0` **on `master`**. CI builds `:latest` + `:1.0.0` + `:dev`. Tagging is what publishes.

## Known and accepted, so nobody rediscovers them at 2am

- **320px is measured but not enforced.** Two pages still exceed it: the dashboard's per-row counts
  by 13px and the row editor's two stat tiles by 60px. Every min-content cause has been dealt with;
  what is left needs a layout decision — stacking what is currently side by side, at a breakpoint
  below `sm` — which is the owner's call, not a containment fix. The audit prints them every run.
  Do not "fix" this by widening the tolerance.
- **Tap targets are 36px/32px app-wide** (shadcn's `h-9`/`h-8` defaults), below Apple's 44pt and
  Google's 48dp. Reported by the audit, deliberately not failed on. A real decision, still open.
- **The un-watch change is new.** `38154ee` deletes cached watch history when an incremental read
  proves it covered its window. It was written and reviewed on 2026-08-03 and first deployed the
  same day; the owner chose to move forward rather than soak it. If anything odd shows up in watch
  history after 1.0, start at `watch_cache._drop_vanished_since` and `WatchedRead.covers_window`.
- **Nothing verifies row hiding after the fact.** The automatic Privacy Check was removed at the
  owner's request on 2026-07-16, so leak-safety rests entirely on the write ordering in
  `.claude/rules/plex-safety.md` rule 1. Shipping 1.0 with that is a decision that has been made,
  not an oversight.

## Announcement timing (not a blocker, but it shapes when to tag)

r/PleX bans self-promotion outright. r/selfhosted's standalone-post window opens ~12 Oct 2026 and
awesome-selfhosted ~21 Nov 2026. Tagging 1.0 and announcing 1.0 do not have to be the same day.
