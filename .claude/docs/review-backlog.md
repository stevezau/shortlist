# Full-repo review backlog

Findings from the nine-reviewer pre-`beta.8` sweep (July 2026). **Everything is closed** — sections
1–8 from the sweep itself, plus the one item the sweep missed and a later pass found. See the `dev`
history for 2026-07-31.

Kept as the record of what was fixed, so a future reviewer who rediscovers one of these checks the
history before "fixing" it again. Nothing here is outstanding.

---

## The item the sweep missed (closed 2026-07-31)

**The API declared response models on 1 of 68 endpoints.** Everything else was `-> dict`, so the
OpenAPI schema published `{ [key: string]: unknown }` and the SPA hand-wrote ~65 response interfaces
— a standing violation of `.claude/rules/frontend.md`. Those hand-written types had measurably
drifted: the UI was sending `prefs.row_size` and `prefs.max_rating`, **fields the server had
deleted**, silently swallowed by Pydantic's `extra="ignore"`, with a test asserting the broken body.

I initially deferred this as too risky for a release gate, on the grounds that a Pydantic response
model *filters* the payload — any key not declared is dropped, silently, in production. That reason
was sound but the conclusion was not: `model_config = ConfigDict(extra="allow")` documents the shape
**without** filtering it, so undeclared keys pass through untouched and the failure mode cannot occur.

Now **65 routes / 115 schemas**, every model inheriting `PassthroughModel`
(`shortlist/server/api/schemas.py`), which is the single home for that rule.

Two things worth remembering from doing it:

1. **The obvious test does not catch a violation.** Asserting an endpoint's full key set passes
   whether or not the model declares every field — precisely *because* `extra="allow"` lets the rest
   through. Those assertions protect the passthrough; nothing protected the passthrough itself.
   `tests/unit/test_response_models.py` walks the live route table and checks the config directly.
2. **It caught a real regression immediately.** Consolidating three different passthrough mechanisms
   into one, a script deleted the config line before the rebase ran, leaving 26 models filtering
   their payloads. No other test noticed. That is the exact bug the rule exists to prevent, and it
   happened within an hour of the rule being written.

Deliberately left as open maps, because their keys vary by DATA rather than by branch: `Run.stats`,
`RunUser.diff`, `RunUser.breakdown`, `RunUserTrace.trace`, the run log's `counts`, `UserOut.prefs`,
`Collection.hub_anchor` values' parent map, and `GET /api/settings`. A model over any of them would
either 500 on legacy rows or invent absent keys into every payload. Each is commented where it lives.

---

## Closed — do not redo

Listed so a future reviewer who rediscovers one of these checks the history before "fixing" it again.

**Security / data integrity** (shipped before the structural pass): forgeable session secret on an
empty secret file; `build_context()` and `GET /api/report` each leaking a pooled DB connection per
call; the watched read treating one page as the complete set; backup label path traversal + leaked
SQLite handles; SSRF bypass on `POST /settings/curator/models`; `LIMIT -1` on four endpoints;
`history_depth` reset to 0; the `caches` table never swept; `Placement` "off" badged "Home & Library".

**Structural** — `_run_user` 564 → 154 lines behind a `RowPolicy` dataclass (and `seeds_for` now binds
on the cold path); `_deliver_one` 221 → 170 with the identity match extracted; the `rows.py` ↔
`pipeline.py` cycle broken via `engine/context.py`; `services/jobs.py` no longer imports upward from
`api/users.py` (now `services/user_sync.py`); `RunService` 1165 → 315 plus `run_log.py` /
`watch_sync.py` / `run_persistence.py`; `report.effectiveness` moved to `services/report_service.py`
and off the event loop; `update_collection`'s eleven flags replaced by `plan_row_changes`;
`run-detail.tsx` 1108 → 329, `run-user-trace.tsx` 1516 → 1207, `row-editor.tsx` 882 → 609,
`jobs.tsx` 913 → 689, and the 127-line IIFE inside JSX extracted.

**Correctness** — the duplicate `version_check` module that disagreed with its twin about this build;
`all_public()` raising `KeyError` where `get()` tolerates; a box-less `SettingsStore` silently storing
plaintext secrets; migration `0053` tightening eleven columns the ORM already declared NOT NULL
(proven on a real 0052 database, `compare_metadata` 11 → 0, with a zero-diff guard); pre-migration
backups rotating the real ones away on a crash loop; retention pruning inside the run-persist
transaction; `MediaType.TV` (which does not exist) silently disabling the Sonarr v3 fallback;
unvalidated `audience_user_ids` 500ing instead of 422ing; a blocking `queue.get()` inside `async def`
stalling the event loop through a rename; the run-log poll re-reading the whole log every tick;
`_request_outcomes` scanning the entire request table.

**Consistency** — `system.py` now declares auth at router construction, with `/health` on a separate
bare router and the aggregation at the BOTTOM of the file so a stray `@router.get` is an import-time
`NameError` rather than a silently open endpoint; one audit writer (`services/audit.py`) replacing
five; one `dry_run` idiom across seven job handlers, with the audit recording the EFFECTIVE value;
`Event.level` standardised on `"warning"` (migration `0054`, plus a runtime guard and a source guard —
the source guard alone missed three positional call sites); one source of truth per cron default;
`redact()` strengthened to match `scrub()`.

**Three bugs the nine reviewers missed**, all found by writing tests for existing behaviour:

1. **`X-Api-Key` was redacted by neither scrubber** — the exact header `arr.py` sends to
   Radarr/Sonarr, reachable in an API 502 body and in persisted `events` rows (plex-safety rule 9).
2. **`dismissable: False` was decorative** — `build_notifications` filtered on id alone, so the "all
   runs are paused" alert could be silenced for ever, leaving an owner believing a stopped server was
   building rows nightly. Now enforced on READ, so an id already in a dismissed list re-surfaces.
3. **The dry-run chokepoint could be bypassed** — a context that dropped the flag turned "show me what
   this would delete" into a real deletion. A test caught it; the fix is a floor that can force a
   preview on but never off.

**Coverage** — `tests/unit/test_curator.py` (35 tests) where the provider matrix had zero;
`test_collection_reconcile.py` (46); `test_notifications.py` (26); plus `test_audit.py`,
`test_openapi_snapshot.py`, `test_settings_store.py`, `test_migrations.py`. The 4830-line
`test_api.py` is now ten files (250 tests before, 250 after, node-id diff byte-identical) — and the
split surfaced `test_row_templates_are_real.py` importing a fixture out of the monolith.

**CI** — a `docker-smoke` job now boots the built image and asserts it both reports healthy AND serves
the SPA (health alone is answered by Python and would pass with no `web/dist` in the image).
Publishing depends on it, and it runs on PRs, where publishing never does. Before this, nothing ran
the container at all — five docs claimed e2e did, and e2e runs uvicorn in-process.
