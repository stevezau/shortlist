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
model _filters_ the payload — any key not declared is dropped, silently, in production. That reason
was sound but the conclusion was not: `model_config = ConfigDict(extra="allow")` documents the shape
**without** filtering it, so undeclared keys pass through untouched and the failure mode cannot occur.

Now **65 routes / 115 schemas**, every model inheriting `PassthroughModel`
(`shortlist/server/api/schemas.py`), which is the single home for that rule.

Two things worth remembering from doing it:

1. **The obvious test does not catch a violation.** Asserting an endpoint's full key set passes
   whether or not the model declares every field — precisely _because_ `extra="allow"` lets the rest
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

## First-run copy audit (2026-08-02) — ALL RESOLVED

A full audit of user-facing strings in the row editor, sources/placement/artwork pickers and the
setup wizard, read as a non-technical person setting Shortlist up for the first time. The two
FACTUAL findings and the three worst jargon strings were fixed in `87b01c9`; **all 15 items below
were fixed in `41542f0`.** Kept as a record of the reasoning, not as a worklist.

Applying them turned up three further claims that were not merely unclear but WRONG. Each was
verified against the code before rewriting, and each is worth remembering as a shape:

- **The wizard told owners the opposite of the truth** — "your Home shows every row on the server",
  with a tip to make a Plex Home user to escape it. `_promote_one` (`engine/pipeline.py:962-966`)
  routes the owner through `promotedToOwnHome` and everyone else through `promotedToSharedHome`, so
  a friend's row never reaches the owner's Home. `owner-note.tsx` already had this right and
  `users-page.test.tsx` records the same claim being fixed there once before — the wizard was the
  copy that got missed. **Two places stating the same fact drift; the fixed one doesn't fix the other.**
- **MDBList's key is in Settings → Connections, not Settings → Requests.** A settings path that
  names the wrong screen reads as authoritative and costs the user a hunt.
- **"choose which under Search backend below" pointed at a control that isn't there.** `SOURCES`
  descriptions render in the row editor, but that sentence was written for the settings page.

One item described an "AI-from-library" source that has never existed (`sources.ts` has only
`tmdb_similar`, `tmdb_discover`, `trakt`, `llm_web`) — replaced with a real example.

1. **"TMDB" is never spelled out**, and it gates the wizard: `Next` on step 2 is disabled until a key
   is on file (`wizard.ts` `tmdb_set`). First use should read "The Movie Database (TMDB)"
   (`step-history.tsx:86-90`).
2. **"Plex Home user" is undefined** in the owner-privacy tip (`step-users.tsx:94-97`), and is easy to
   confuse with "Home screen", which the copy does assume. It is advice about a real limitation, so
   vagueness costs more than usual. Say "create a separate Plex account for your own watching".
3. **"share filter" is unexplained shorthand** (`placement-toggles.tsx:252-257`) — the single most
   load-bearing piece of the privacy mechanism, and it never appears defined anywhere.
4. **A sources example names an option that does not exist**: "an AI-from-library 'Hidden gems'"
   (`row-sources-field.tsx:85-87`). The four real sources are tmdb_similar, tmdb_discover, trakt,
   llm_web — and llm_web searches the WEB, not the library. Someone could hunt for a toggle that
   isn't there. Also "discovery engines" → "where this row looks for titles".
5. **Settings paths that don't say what is there** (repeated): `row-editor.tsx:553,647,858-861`,
   `row-shelf-placement.tsx:66` ("Use the default (Settings)"), `row-sources-field.tsx:143-145`.
6. **"MDBList" dropped in with no gloss** (`row-editor.tsx:816-817`) — IMDb/RT/Metacritic are
   recognisable, the service supplying them is not.
7. **Trakt and Exa named with no context** (`sources.ts:39-40,46-48`); "Search backend below" is a
   forward reference with nothing to land on.
8. **"runs" used as if defined** in the poster field (`poster-field.tsx:228-229`).
9. **Sonarr/Radarr + "global tag"/"each person's own tag"** assume prior context
   (`row-editor.tsx:890-891,906-910`).
10. **Inconsistent wayfinding** in `audience-picker.tsx`: line 58 names the Users page, lines 92-93
    say "import your Plex users first" without saying where.
11. **"All ticked = every library"** reads as a formula, not a sentence (`library-picker.tsx:154-156`).
12. **"works just as well"** (`step-welcome.tsx:44-46`) is an unverifiable comparative claim about
    output quality, and leans on the same wrong mental model of the curator that `87b01c9` fixed.
13. **"cadence"** where "how often it refreshes" would do (`step-customize.tsx:156`).
14. **"Library Recommended"** as a grid row label has no verb and parses badly
    (`placement-toggles.tsx:189`); the columns already establish audience, so the row only needs to
    say WHAT — "Recommended shelf".
15. **Ordering**: `step-connect.tsx:135` says "Hit Next to choose a history source", but the next
    screen's required action is a TMDB key and it never calls itself that. And `step-customize.tsx`
    references "after the first run" before "run" is introduced (the following step).

Not audited: the Dashboard, Users, Runs, Jobs, Requests and Settings pages. (All but **Requests**
were audited on 2026-08-02 — see the section below.)

---

## Copy audit — Dashboard, Users, Runs, Jobs, Logs, Settings (2026-08-02)

The pass the wizard/row-editor audit above never reached, read as a non-technical Plex owner on
their first day. Setup, `components/rows/**` and `lib/wizard.ts` were deliberately left alone (a
concurrent edit). Everything in **Fixed** is on `dev` as of this date; everything in **Open** is not.

### Fixed — factual errors (the valuable ones)

1. **MDBList's key was pointed at the wrong Settings section** — again, and in a second place.
   `recommendations-section.tsx` ("Rate titles using") said the non-TMDB scores "need its API key in
   **Requests**". The key is `requests.mdblist.apikey` but its only input is the MDBList card in
   **Connections** (`connections-section.tsx:246-266`), which `requests-settings.tsx:211` states
   outright. Now links to `#connections` and says the card is where you paste it. This is the same
   error the wizard pass found; it lived in two files, and only one was fixed.
2. **"Run for <person>" told you to watch the run on the Dashboard.** The Dashboard renders nothing
   but `ImpactReport` (`pages/dashboard.tsx`) — no live view of anything. Runs are watched on
   `/runs`. `user-detail-header.tsx` now links there.
3. **"Disable all" claimed share filters are left untouched.** They are written: disabling queues a
   privacy pass (`api/users.py:238` → `services/user_sync.py:66` `queue_privacy_sync`), which is
   precisely what stops a disabled account still seeing the shared rows. The dialog now says sharing
   settings are updated, and that the pre-install snapshot survives for uninstall.
4. **Settings → Advanced "Log level" described the wrong sink.** `configure_logging` adds the file
   sink at a hardcoded `level="DEBUG"` (`logging_config.py:85`) and only the stderr sink takes the
   setting. The Logs page and the `.zip` download both read that file (`api/system.py:244-274`), so
   the control cannot quieten them and **TRACE — the setting people are told to use for a bug report
   — never reaches either**. Renamed "Console log detail", and it now says where each level lands.
   A matching line was added to the Logs page saying its buttons filter what is _shown_.
5. **Two pages implied a single global nightly run.** Runs' empty state said "wait for the nightly
   schedule" and Jobs' subtitle said "rather than waiting for the nightly run". There is no global
   cron: rows carry their own (`Collection.schedule`; the old `schedule.cron` is gone —
   `settings_store.py:52`) and a row with a blank one never runs on a timer at all. Each background
   job has its own separate cron too.
6. **"Enable all" promised a row to accounts that cannot have one.** An account with a Plex
   restriction profile is skipped by `enabled_profiles` (`context_builder.py:492-495`) and its
   toggle is already disabled on the same page. The dialog now says so.
7. **"Pause all" said it "stops all runs".** A run still starts; `enabled_profiles` returns `[]`
   (`context_builder.py:481`) and the engine still does its privacy sweep on an empty user list
   (plex-safety rule 1). Reworded to the true consequence: nobody is processed, no row is rebuilt.

### Fixed — jargon, dead ends, empty states

- **Connections cards now say what each service _is_** on first mention: Tautulli ("the monitoring
  dashboard many people run alongside Plex"), TMDB ("The Movie Database … the free film and TV
  catalogue", marked Required — `wizard.ts:87` blocks setup without it), Radarr/Sonarr, Trakt ("a
  site where people log what they watch"), Exa ("a web search built for AI to read"), MDBList.
- **MDBList's card under-claimed.** It named only Requests; it also backs any row ordered by
  "Highest rated" (`recommendations.rating_source`). Both consumers are now named.
- **Dashboard defines "delivered" once**, in the page subtitle, rather than using it throughout
  undefined. "Landing rate" → "Picks that get watched"; "Landing best" → "Most watched"; "Avg to
  watch" → "Time to watch". The all-empty state now says _where_ to press the button.
- **`ImpactReport` rendered a second `<h1>`** under PageHeader's — now `<h2>`.
- **"Blocked seeds"** (user page) → "Blocked titles", with one sentence explaining why a watch
  shapes picks at all. The screen-reader-only "Block X as a seed" lost the jargon too.
- **"No rows reach this person"** was wrong-ish: the endpoint lists per-person rows only
  (`api/user_rows.py:67` filters `build="per_person"`), so a shared-row-only server hit an empty
  state that read as a bug. Now says so explicitly.
- **Runs' "Clear runs" dialog** named "hit rate", a metric that appears nowhere on the Dashboard.
- **Users' empty state** said "check the Plex connection under Settings" — now names the card.
- **Settings → Finding titles: the two seed knobs were in the wrong order.** "Watches the AI
  searches from" sat above "Watches to build from", giving no clue that the second governs every
  source and the first only slices the front of that same list (`candidates.py:183` searches
  `seeds[:recent_count]`). Swapped, and each now says how it relates to the other.

### Open — not fixed, with the reason

1. **`maintenance.prune` is invisible on the Jobs page.** It is `manual=True` (`services/jobs.py`),
   so it is filtered out of the "Automatic" group (`pages/jobs.tsx:281` `!e.manual`) — and it is not
   one of the five hardcoded `JobRow`s in "Run now". Its counts _are_ in the page totals, so a failed
   prune shows "1 failed" in the header chip with **no row anywhere to click**. Needs a `JobRow` +
   a run mutation, which is a functional change, not copy.
2. **"Watches to build from" / "Watches the AI searches from" are still near-identical names.** They
   want renaming to something like "Recent watches a row is built from" / "How many of those the AI
   searches the web for" — but the labels live in `max-seeds-field.tsx` and `recent-count-field.tsx`,
   shared with the row editor, and `row-editor.test.tsx` asserts them. Renaming in Settings alone
   would regress `68f36c5` ("one name per setting"), so all three must move together.
3. **`advanced-section.tsx:38` falls back to `runs.retention ?? 100`.** The server default is `3`
   and the API bounds the field to 0–24 (`api/settings.py:160`), so 100 is both unreachable
   (`all_public()` always returns the default) and out of range if it ever were reached.
4. **The Plex card's "Plex token" field has no "where do I get this" link**, unlike TMDB/MDBList/Exa.
   Normally filled by the wizard's PIN flow, so it only bites someone re-entering it by hand. Left
   alone rather than assert a support URL I could not verify.
5. **The read-only Plex audit sits at the top of the Danger zone** (`danger-zone-section.tsx:22`).
   It is the safest control on the page under the scariest heading; it reads better under Advanced.
6. **`JobDetail` renders raw result keys** ("Asked to" + a JSON blob, then `fixed`/`orphans`/
   `demoted` verbatim). Diagnostic rather than everyday, so left as is.
7. **The Jobs "Run now" group mixes reads and writes** with nothing distinguishing them: "Sync watch
   history" only reads, while "Sync check" writes corrections to Plex and can delete a collection.
   The consequence is only visible once a row is expanded.
8. **Backend job-catalogue copy is in `services/jobs.py`**, not the SPA — it reads well, but it is
   the one place a copy pass over `web/` will always miss. Worth noting for the next audit.
