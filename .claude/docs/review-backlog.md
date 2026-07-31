# Full-repo review backlog

Findings from the nine-reviewer pre-`beta.8` sweep (July 2026). **Sections 1–8 are all closed** — see
the `dev` history for 2026-07-31. What remains is one item the sweep did not find and the last pass
did, recorded here because it is real, sizeable, and not safe to do at a release gate.

---

## Open: the API declares almost no response models

**Found 2026-07-31, while generating the SPA's types from the OpenAPI schema (old item 4.14).**

Of 68 paths, **exactly one JSON response declares a Pydantic model** (`GET /api/requests` →
`RequestCandidateOut`). Every other operation is annotated `-> dict` or `-> list[dict]`, so the schema
publishes `{ [key: string]: unknown }` and nothing downstream can check anything. Four operations
declare no response content at all: `GET /api/runs/{run_id}/log` and the three `/api/system/backups*`
endpoints. (Five more — SSE `/api/events`, the rename stream, poster image/preview, `logs/download` —
are streams or binary and correctly have no JSON schema.)

Consequences, all live today:

- `web/src/lib/types.ts` still hand-writes **65 response interfaces**, which `.claude/rules/frontend.md`
  explicitly forbids ("never hand-write request/response types"). Only 15 types could be generated.
- Those hand-written types were measurably wrong. The pass that generated the other 15 found the UI
  sending `prefs.row_size` and `prefs.max_rating` — **fields the server had deleted** — accepted and
  silently discarded by Pydantic's `extra="ignore"`, with a frontend test asserting the broken body.
  It also found `defer_rename` declared on the response type rather than the request, which forced an
  `as never` cast in `pages/row-rename.tsx` — a cast worse than `any`, caused purely by the
  hand-written type being wrong. Six nullability mismatches besides.
- Nothing catches the next such drift.

**Why it is not done yet.** A Pydantic response model does not merely describe a response, it
**filters** it — any key not on the model is dropped from the payload. Adding ~65 of them to endpoints
whose exact dict shape the SPA already depends on is a change that fails _silently and in production_,
by removing a field the UI reads. It wants doing at the start of a cycle, endpoint by endpoint, each
diffed against a recorded real response — not in the same push as everything else.

**Suggested order**, by how much UI depends on the shape:

1. `GET /api/runs/{id}` → `RunDetail` (~8 nested interfaces)
2. `GET /api/report` → `EffectivenessReport` (the largest single hand-written type)
3. `GET /api/runs/{id}/users/{uid}/trace` (13 `Trace*` interfaces)
4. `GET /api/users`, `GET /api/collections`
5. The three `/api/system/backups*` endpoints (no declared content at all)

**Method that works** (proven on the 15 already done): add the model → regenerate
`web/openapi.snapshot.json` → `pnpm -C web gen:api` → repoint the type in `types.ts` → `tsc -b`.
`tests/unit/test_openapi_snapshot.py` fails if the snapshot drifts from the app, so the generated
types cannot silently go stale.

Six frontend-only unions stay hand-written until their server fields stop being bare `str`:
`UserType`, `RunTrigger`, `SyncKind`, `TraceFate`, `ReportWindow`, `TestableService`. `Placement` and
`PosterMode` no longer need to be — those closed sets are now advertised in the schema by
`_closed_set()` in `api/collections.py`, which is the pattern to copy.

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
