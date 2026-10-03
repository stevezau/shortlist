# Phase 2: run privacy reporting + dashboard

Programme constraints: `.claude/docs/plans/2026-10-03-design-refresh.md`.
Spec: review cards app-run-privacy, app-dashboard, app-dashboard-empty; mockups `.impeccable/mocks/app-run-detail.html`,
`app-dashboard.html`, `app-dashboard-empty.html`.

**Decision (recorded here, not in the cards):** no new `Run.status` value. Five places filter on
`status.in_(("ok","error"))` (`runs.py:86`, `report_service.py:1120`, `notifications.py:373`, `system.py:348`, `support.py:527`),
the SSE `RunFinishedEvent.status` is a Literal, and `run-outcome.ts` maps statuses: a new value would silently drop runs from
all of them. Instead the API exposes the privacy facts the run already persisted, and the UI shows "OK with warnings" when
`status == "ok"` and the run's privacy object carries a finding. Reporting only: nothing here writes to Plex
(jobs-and-runs-design §12 scope confirmed).

Facts: `run_persistence.py:1823-1838` writes `stats.unhideable_rows` (`{username: [ratingKey]}`) and
`stats.unreadable_filters` ONLY when `report.unhideable_measured`; `stats.filters_not_enforced` only when
`report.filters_enforcement_measured`. Absent key = the run did not measure. `_record_unhideable` (pipeline.py:812) records an
account only if it can actually see other people's rows.

## Task 2.1 (backend): `privacy` on run summaries

**Files:** `shortlist/server/api/schemas_runs.py`, `shortlist/server/api/runs.py` (`_run_summary`), OpenAPI snapshot + generated
`web/src/lib/api-schema.d.ts` (`pnpm -C web gen:api`, or the repo's snapshot script — find it), Test: `tests/integration/test_api_runs.py`.

**Produces (exact):**
```python
class RunPrivacyOut(PassthroughModel):
    """What this run measured about who can see whose rows. Reporting only."""

    #: Accounts Plex refuses hide rules for that can nonetheless see other people's rows (stats.unhideable_rows keys with a non-empty list).
    can_see_others: list[str]
    #: Accounts whose share filter Plex itself cannot read (stats.unreadable_filters keys).
    unreadable_filters: list[str]
    #: Accounts whose filter Shortlist wrote and Plex is not applying (stats.filters_not_enforced keys); [] when that check did not run.
    filters_not_enforced: list[str]


# RunSummaryOut gains:
privacy: RunPrivacyOut | None  # None = this run did not measure privacy (absent stats.unhideable_rows)
```
All lists sorted case-insensitively. `RunDetailOut` inherits it.

- [ ] Tests first (integration, real DB fixture the file already uses):
  1. a finished ok run whose stats carry `unhideable_measured`-style keys `{"unhideable_rows": {"kid": [1,2,3], "zed": []}, "unreadable_filters": {}}` → `privacy == {"can_see_others": ["kid"], "unreadable_filters": [], "filters_not_enforced": []}` on both list and detail endpoints (the empty `zed` list is not a finding);
  2. a run with no `unhideable_rows` key (older run, dry run, run that died early) → `privacy is None`;
  3. `filters_not_enforced: {"mike": [5]}` → listed;
  4. status is unchanged (`"ok"`) in all cases.
- [ ] Implement; tests pass; regenerate the OpenAPI snapshot + TS types; run the OpenAPI snapshot test if one exists.

## Task 2.2 (frontend, after phase 1 lands): run detail

Per `.impeccable/mocks/app-run-detail.html`: keep today's row-first layout (one section per row; people list left; selected
person's picks right; "How we picked"). Add: summary strip with Result / Duration / People / Privacy / Titles changed; Result
pill "OK with warnings" (warning colour) when `status==="ok" && privacy && (can_see_others.length || unreadable_filters.length || filters_not_enforced.length)`;
Privacy cell "N of M accounts hide every row" where M = `users.length` and N = M minus accounts in any privacy list, plus a
link to /privacy naming the first account; plain "Not measured" when `privacy === null`; the amber callout naming the
account(s); each person in the people list shows their new-pick count and a "not private" pill when listed; times absolute +
relative. People list includes every person in `users` (kid included). Tests: testing-library cases for the three result states
(ok+finding, ok+no finding, privacy null) and the privacy cell text.

## Task 2.3 (frontend, after phase 1): dashboard + empty dashboard

Per mockups: status strip (Last run: status from `/api/report` `runs.last_status` + the latest run's `privacy` from
`GET /api/runs?limit=1` → "OK · 1 warning" when a finding; Next run: earliest `next_run` from `GET /api/schedule`; Privacy: from
`usePrivacyStatus()` — accounts in state `hiding` vs accounts that should hide; Plex: connected/version from the existing system
status query the app already uses — find it). Amber callout when an account cannot be hidden. Below: today's ImpactReport
sections, restyled, unchanged in data. NO per-person poster strips (owner rejected them). Empty state (no finished run): status
strip + one panel "Build everyone's rows for the first time" with Run now (primary) and Dry run first, wired to the same
mutations Runs uses. Tests: empty state renders the two buttons and no link to /runs; strip shows "Not measured"/"Nothing to hide
yet" without a run; warning pill when the latest run has a finding.
