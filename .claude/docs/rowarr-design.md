# Rowarr — Product & Technical Design

**Status:** design complete; execution gated on one live privacy test · **Date:** 2026-07-12 ·
**Predecessors:** [`ai-recommendations-design.md`](ai-recommendations-design.md) (the personal-script design this productizes) + [`ai-recommendations-research.md`](ai-recommendations-research.md) (May research) + July 2026 deep-research re-validation (99-agent sweep, 25/25 claims verified).

---

## 1. Vision

> **Rowarr — a private, AI-curated "Picked for You" row for every user on your Plex server.**

Self-hosted, one Docker container. The server owner logs in with Plex, picks their users, and every
night each user's Home screen gets a personal row built from _their_ watch history — visible only to
them. Netflix's killer feature, on Plex, without Plex's involvement.

**Why now (the moat):** per-user private collections were _impossible_ until Plex fixed label
restrictions on Home/Recommended (PM-617, v1.43.1, Feb 2026) and Related hubs (PM-5174,
v1.43.2.10687, public 2026-05-19). Verified July 2026: **no maintained open-source tool exploits
this yet.** Curatarr (closest) is dormant, never promotes to Home, no LLM. Immaculaterr is
closed-source. PlexAI_Personal_Curator is dead and playlist-only. First mover wins the niche.

**Positioning sentence for the README:** _"Rowarr watches what each of your users watches, asks an
LLM to curate what they should watch next from what you already own, and puts it on their Home
screen — privately, automatically, every night."_

### Non-goals (v1)

- Multi-server support (single PMS per instance)
- Jellyfin/Emby (roadmap)
- Per-user "dismiss this" feedback (no Plex API for it; implicit feedback via hit-rate instead)
- Hosted/SaaS anything — this is a self-hosted OSS tool, MIT license, BYO API keys

---

## 2. Product principles

1. **Ten minutes to magic.** `docker run` → Plex PIN login → wizard → first rows exist. Every wizard
   step must justify its existence; anything skippable is skipped by default.
2. **Trust is the product.** This app modifies _other people's_ Plex views and touches share
   restrictions. So: nothing writes to Plex until the built-in **Privacy Check** passes; every
   restriction write is snapshotted and reversible; **Uninstall** is a first-class feature that
   provably restores the server to its prior state.
3. **AI that can't hallucinate.** The LLM only ever re-ranks titles verified to exist in the
   library. It's an editor, not an oracle. Works with zero LLM too (heuristic mode) — this defuses
   the r/PleX "AI slop" backlash and the no-cloud crowd in one move.
4. **Native to the homelab.** Single container, `/config` volume, dark UI, GHCR multi-arch image,
   env-var overrides, healthcheck endpoint, works behind a reverse proxy at a subpath.
5. **Explainable.** Every pick carries "Because you watched X." The dashboard shows _why_ the app
   did everything it did (runs, diffs, restriction changes).

---

## 3. Onboarding wizard (the UX centerpiece)

Seven steps. Progress bar, every step has a "what & why" sentence, all state persisted so refresh
resumes mid-wizard. Target: **under 10 minutes** including first run.

### Step 0 — Welcome

Animated mock of a Plex Home screen gaining a "✨ Picked for You" row. One button: _Connect Plex_.

### Step 1 — Connect Plex

- **"Login with Plex" button** (MPG-style): opens the plex.tv auth popup
  (`app.plex.tv/auth#?clientID=…&code=…`), which auto-completes the underlying
  `plex.tv/api/v2/pins` flow — no typing. Fallback for popup-blocked/headless browsers: show the
  4-char code + "enter it at plex.tv/link". Same API, two presentations. No password ever touches
  Rowarr.
- On link: enumerate the account's servers via plex.tv resources; owner picks one from a **server
  picker** (name, owned badge, local/remote). Rowarr auto-tests each advertised connection URI and
  shows what worked — and the chosen **URL is always editable** (manual URL field + "insecure
  (self-signed)" toggle), both here and later in Settings → Connections. Never trap the user behind
  auto-discovery.
- **Capability probe** (background, instant):
  - PMS version ≥ **1.43.2.10687**? (from `/identity`) → else the **Compatibility Gate** (§9)
  - **Plex Pass** on admin account? (plex.tv subscription check) → required for label restrictions
  - Library sections found (movie + show types listed with counts, checkboxes, all on by default)
- UX detail: results render as a live checklist ✅/❌ with plain-English explanations, not error codes.

### Step 2 — History source

- Auto-detect **Tautulli** (common hosts/ports probe + manual URL/API-key fields; validated with a
  test call). Copy: _"Tautulli gives Rowarr deeper, more reliable watch history. Optional."_
- Fallback (always available, zero config): Plex's own history API (`/status/sessions/history/all`
  per accountID with the owner token — works for invited users, verified in Curatarr's code and the
  May audit).

### Step 3 — Choose your curator (LLM)

- Provider cards: **Anthropic / OpenAI / Google / Ollama / None**. Each card: what it costs
  (qualitative + link), a key field, a **Test** button that makes one tiny call and shows the reply.
- **Keys are BYO** for the three cloud providers (user pastes their own API key; card links to each
  provider's "get a key" page; stored encrypted at rest in `/config`, never logged, redacted in UI
  after save). **Ollama needs no key** — just a URL (default `http://host:11434`), free and fully
  local. **None** needs nothing.
- **None** = heuristic mode: frequency-across-seeds × rating × recency ranking, template reasons
  ("Because you watched Suits"). The app is fully functional without any AI key — say so proudly.
- Default model per provider pre-selected (e.g. Anthropic → `claude-haiku-4-5-20251001`; cheap tier
  is plenty for re-ranking 40 titles). Advanced: model override, max monthly spend estimate shown.

### Step 4 — Pick your users

- Table of all shared + managed users (avatars from plex.tv), toggle per user, "select all."
- Per-user intel badges, computed live: **history depth** ("342 items"), **cold start** warning
  (<10 items → will get library-popular fallback row), **managed-user** flag (label restrictions
  require their restriction profile = None — shown with a doc link, _not_ auto-changed, since that
  profile is parental controls).
- **Admin caveat surfaced here**, not buried: _"Plex cannot hide collections from the server owner —
  your own Home will show every user's row. Tip: watch on a non-owner account."_

### Step 5 — Privacy Check ★ (the feature nobody else has)

Runs automatically, ~60 seconds, with a live log:

1. Create throwaway collection `Rowarr Privacy Probe` with one item, label `rowarr_probe`,
   promote to shared Home.
2. Write an exclude-restriction for `rowarr_probe` to a **canary user** (owner picks from a
   dropdown; a managed/Home user is auto-suggested when one exists).
3. **Verify in tiers:**
   - **T1 (always):** read the share filters back from plex.tv and assert the exclusion persisted.
   - **T2 (managed canary):** switch into the Home user server-side (Plex Home user switch yields a
     scoped token), fetch _their_ Home hubs, assert the probe collection is **absent** for them and
     **present** for a non-excluded view.
   - **T3 (invited-only servers):** guided manual check — QR code / short URL opens Plex as the
     canary on the owner's phone; two screenshots-style prompts ("Do you see a row called…? Yes/No").
4. Delete probe collection, restore canary's filters from snapshot. Server untouched.

- Pass → big green "Your server keeps rows private ✅". Fail → **Shared Mode** offer (§9) with a
  clear explanation of what leaked. Either way the user knows the truth about their server in one
  minute — no other tool in this space verifies anything.

### Step 6 — Make it yours (customization)

- **Row name template:** `✨ Picked for You` · `Because you watched {top_seed}` (dynamic nightly!) ·
  custom text + emoji picker. Live preview rendered as a fake Plex row.
- **Row size:** 10 / 15 / 20. **Schedule:** nightly / weekly / custom, with a time picker (default
  03:30 server-local). Advanced disclosure: full **cron expression** editor with a human-readable
  preview ("every Mon+Thu at 04:00"), plus **per-user schedule overrides** later in each user's
  detail page (e.g. the kids' rows refresh weekly, everyone else nightly). Full flexibility exists,
  but only behind an "Advanced" fold — the default path stays two clicks.
- **Collection poster:** auto-generated branded poster with the user's name (PIL template, three
  styles) or none.
- **Acquisition (off by default):** "when the curator loves something you don't own" → connect
  Radarr/Sonarr _or_ Seerr → cap per week (default 2) → auto-add or suggest-only queue in dashboard.

### Step 7 — First run

- Fires the pipeline for all enabled users, streaming per-user progress (SSE): history → candidates
  → curating → collection → privacy sync, with counts at each stage.
- Success screen: per-user cards with their actual top-3 poster thumbnails + "Open Plex as <user>"
  deep links; a copy-paste announcement snippet the owner can send their users.

---

## 4. Main app UI

Four sections in a left rail: **Dashboard · Users · Runs · Settings**. Dark theme default,
Plex-adjacent accent color, responsive (phone-usable — owners administer from couches).

### Dashboard (wireframe)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Rowarr        ● Privacy: verified 2026-07-12   Next run: tonight 3:30│
│──────────────────────────────────────────────────────────────────────│
│  40 users enabled · last run 6h ago · 0 errors · hit rate 31% ▲      │
│                                                                      │
│  ┌─ Sarah ────────────┐ ┌─ Mike ─────────────┐ ┌─ Jamie ──────────┐ │
│  │ [▓][▓][▓][▓][▓] 15 │ │ [▓][▓][▓][▓][▓] 15 │ │ [▓][▓][▓][▓] 12  │ │
│  │ "Because you       │ │ ✓ 6h ago · 2 new   │ │ ⚠ thin history   │ │
│  │  watched Fargo"    │ │ hit rate 40%       │ │ (genre fallback) │ │
│  │ ✓ 6h ago  [Run now]│ │ [Run now] [⏻ on]   │ │ [Run now] [⏻ on] │ │
│  └────────────────────┘ └────────────────────┘ └──────────────────┘ │
│  … (40 cards, search/sort by name · hit rate · last error)          │
└──────────────────────────────────────────────────────────────────────┘
```

- **Hit rate** = % of recommended items the user actually watched within 30 days — the app's own
  proof of value, computed from the same history source. Shown globally and per user.

### User detail

Current picks in ranked order with reason lines + which seed produced each; the history sample used
(transparency); per-user overrides (row name, size, excluded genres, max content rating); pause
toggle; regenerate button; their restriction status (which labels are excluded on their share, when
last synced, with a "view snapshot history" link).

### Runs

Table of runs → drill into a run → per-user timeline with the diff ("added Sneaky Pete, removed
Bosch (watched ✓)"), LLM token usage, API timings, warnings. Errors are first-class rows with
copy-for-GitHub-issue buttons.

### Logs (first-class, MPG-style)

"What happened, when" must never require SSH:

- **Live log viewer** page (streamed via SSE, level filter, search, pause/follow) — the MPG logs
  page pattern.
- **Structured events feed** (the `events` audit table): every Plex/plex.tv WRITE — collection
  created/updated, label added, visibility promoted, restriction merged — logged as a structured
  entry with the diff, per user, filterable. This is the audit trail for "why did user X's share
  change at 03:31."
- Rotating file logs at `/config/logs/` (loguru), download-a-zip button for bug reports.
- Every run links its slice of the log; every error links its run.

### Settings

Connections (Plex/Tautulli/LLM/arr — all re-testable in place, **Plex URL editable** post-setup) ·
**Schedules** (global time-of-day, cadence, cron editor for power users, per-user overrides) ·
Defaults (row template, size) · Advanced (label prefix override, restriction sync mode
read-only/dry-run, plex.tv throttle, log level) · Notifications (Discord webhook / ntfy / SMTP
digest "your rows updated + hit rate") · **Danger zone:** pause all · full uninstall.

### Full uninstall (trust feature)

One flow, with preview: deletes all Rowarr collections, strips `rowarr_*` labels from collections,
restores every user's share filters from the **original pre-Rowarr snapshot** (merging around any
filters the owner changed since, shown as a diff before applying), then wipes local config. Ends
with "your server is as we found it."

---

## 5. The engine (nightly pipeline, per user)

Identical to the validated May design, productized. Runs per enabled user inside one process;
users are independent (`try/except` per user), shared caches across the loop.

```
0. LIBRARY INDEX (once/run)  plexapi → {tmdb_id → rating_key} per section; cached in SQLite,
                             invalidated by section.totalSize change or 24h TTL
1. HISTORY                   Tautulli get_history(user_id) [preferred] | Plex history API [fallback]
                             → last ~30 "meaningful" watches (completion ≥ threshold), recency-weighted
                             negative signals: dropped shows (started, <25% complete, abandoned)
2. CANDIDATES                TMDB /movie|tv/{id}/recommendations + /similar per seed → pooled,
                             tagged with seed; cached (tmdb_id, endpoint) 7 days
3. FILTER                    ∩ library index · unwatched by this user · minus user's excluded
                             genres/max rating · minus items recommended in last N runs (staleness
                             guard, N=3 configurable)
4. PRE-RANK (heuristic)      score = seed_frequency × rating × library-recency → top 40
5. CURATE (LLM, optional)    one structured-output call: user taste summary + 40 owned candidates →
                             top K ranked + one-line reason each (JSON schema; reasons ≤ 90 chars).
                             Provider=None → keep heuristic order, template reasons.
6. DELIVER                   upsert collection (rename if template dynamic) · clear+add items ·
                             custom sort (best first: sortUpdate("custom") + moveItem, verified in
                             plexapi source) · label rowarr_<slug> · poster ·
                             collection mode = "hide" (modeUpdate — row shows on Home but the
                             collection stays out of library browsing, so 40 collections never
                             clutter anyone's Collections tab; same trick Kometa uses) ·
                             promote via collection.visibility() → ManagedHub.updateVisibility
                             (shared=True, recommended=True) — visibility() verified in plexapi
                             source 2026-07-12
7. PRIVACY SYNC              merge-aware exclude-label sync (§6) — usually a no-op after day 1
8. ACQUIRE (optional)        LLM's "wish I could include" titles → Radarr/Sonarr/Seerr, capped
9. RECORD                    picks, reasons, diffs, hit-rate attribution → SQLite
```

**Cold start:** users with <10 history items get a "Popular on <server name>" row (top-rated
unwatched in library, LLM-themed if available) and a dashboard badge; auto-upgrades to personal mode
once history crosses the threshold.

**LLM contract:** structured output (JSON schema enforced), inputs are titles+year+genres only (no
PII), output validated against the candidate set — any title not in the input list is dropped and
logged. Temperature low. One call per user per run; ~40 users ≈ pennies/night on a cheap-tier model.

---

## 6. Privacy system (the load-bearing wall)

**Label scheme:** collections get `rowarr_<user-slug>`; items get **no labels at all** (unlike
Curatarr's item-labels — collections-only is sufficient since exclusions target the collection
label, and it keeps user metadata pristine).

**Restriction sync algorithm** (improves on Curatarr's overwrite loop):

```
for each enabled user U:
  desired_excludes = { rowarr_<slug(V)> for V in enabled_users if V != U }
  current = GET plex.tv/api/users → parse U's filterMovies/filterTelevision
            (pipe-separated conditions; label!= is one comma-separated value)
  if first sync: snapshot current → RestrictionSnapshot(U, before=current)
  merged = current with label!= := (existing label!= values ∪ desired_excludes)   # MERGE, never clobber
  if merged == current: skip (steady-state nights are zero PUTs)
  PUT plex.tv/api/users/{U.id}?filterMovies=…&filterTelevision=…   # throttle 1 req/s, 429 backoff
  read back; assert; log diff
```

- Preserves any pre-existing owner-set filters (content ratings, other labels).
- Snapshots make uninstall/rollback provable.
- Steady state = 0 plex.tv calls; adding/removing a user touches every user's filter once (40 PUTs,
  ~40s throttled — fine).
- Admin/owner is skipped (Plex cannot restrict the owner) and surfaced in UI.

**Continuous verification:** weekly scheduled re-run of Privacy Check T1 (filter read-back for all
users) + T2 when a managed canary exists; dashboard badge flips red with a notification if drift is
detected (e.g. owner manually edited a share).

---

## 7. Architecture

**One container.** Python 3.12 backend serving a static SPA. `/config` volume (SQLite DB, logs,
snapshots, posters). Multi-arch (amd64/arm64) on GHCR + Docker Hub.

| Layer       | Choice                                                                             | Why                                                                        |
| ----------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| API/backend | **FastAPI**                                                                        | Python shop; async; OpenAPI for free (public API = community integrations) |
| Jobs        | **APScheduler** (in-process) + job rows in SQLite                                  | one container, no broker; runs are resumable/idempotent                    |
| DB          | **SQLite + SQLAlchemy**                                                            | homelab-right; `/config/rowarr.db`; Alembic migrations                     |
| Frontend    | **React + Vite + Tailwind**                                                        | SPA served by FastAPI; SSE for live run progress                           |
| Plex        | **plexapi** + thin raw client for plex.tv (pins, users, filters, home-user switch) | plexapi lacks some plex.tv surfaces                                        |
| LLM         | provider interface: `curate(profile, candidates) → ranked picks`                   | Anthropic/OpenAI/Google SDKs + Ollama HTTP + Null provider                 |
| Packaging   | Dockerfile (multi-stage), GH Actions: lint (ruff) → pytest → build → GHCR          | *arr-standard distribution                                                 |

**Code layout (monorepo, MIT):**

```
rowarr/
  engine/        # pure library: pipeline stages, providers, plex/tautulli/tmdb clients — no FastAPI imports
  server/        # FastAPI app, auth, SSE, scheduler wiring, DB models
  web/           # React SPA
  cli.py         # `rowarr run --user X --dry-run` — same engine; this is what Steve's cron uses
```

The engine/app split is contractual: **Steve's server runs `cli.py` from week 1** (Phase 1), the app
wraps the identical engine later. No throwaway code.

**Data model (core tables):** `settings` · `plex_server` · `users` (plex_id, slug, enabled,
prefs JSON, label) · `runs` · `picks` (run_id, user, tmdb_id, rank, reason, seed, watched_at →
hit-rate) · `restriction_snapshots` (user, before, after, ts) · `caches` (tmdb, library index).

**App auth:** "Login with Plex" only, and only the **server-owner account** is authorized (account
id must match the linked server's owner). No local passwords to leak. Session cookie, CSRF on
mutations. Docs firmly recommend not exposing Rowarr publicly; subpath + reverse-proxy documented.

**Secrets:** in SQLite, encrypted at rest with a key derived from an instance secret in `/config`
(sufficient for homelab threat model; documented honestly).

---

## 8. Compatibility gates & edge cases

| Condition                               | Detection                     | Behavior                                                                                                                                                                                                      |
| --------------------------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PMS < 1.43.2.10687                      | `/identity` at Step 1         | **Gate:** explain the leak (with the actual Plex changelog refs), offer **Shared Mode** or blocked-until-upgrade                                                                                              |
| No Plex Pass on admin                   | plex.tv subscription probe    | Gate → Shared Mode offer (restrictions are a Pass feature)                                                                                                                                                    |
| Shared Mode (fallback)                  | user choice                   | One global "Fresh Picks on <server>" collection, blended from opted-in users' tastes; no privacy claims anywhere in UI                                                                                        |
| Managed user w/ restriction profile set | plex.tv users probe           | Badge + docs; never auto-modified (it's parental controls)                                                                                                                                                    |
| Owner/admin account                     | always                        | Excluded from restrictions (Plex limit); banner + "use a viewing account" tip                                                                                                                                 |
| Thin history (<10)                      | Step 4 + nightly              | Cold-start row (§5), auto-upgrade                                                                                                                                                                             |
| User removed from server                | nightly diff                  | Their collection deleted, their label dropped from everyone's excludes, snapshot kept                                                                                                                         |
| plex.tv 429s                            | response codes                | 1 req/s throttle + exponential backoff + resume; runs never half-apply (per-user transaction)                                                                                                                 |
| TMDB down / LLM down                    | health probes per run         | degrade gracefully: reuse last candidates / heuristic mode; warn, never fail the whole run                                                                                                                    |
| Very large libraries                    | index once/run + SQLite cache | O(library) once, O(user-history) per user; 10k-item library ≈ seconds                                                                                                                                         |
| ~40 collections on owner's Home         | —                             | only the owner sees all (verified); collection mode "hide" keeps them out of everyone's Collections tab; incremental rollout guidance in docs; no evidence of Home-render degradation (researched July 2026)  |
| Row POSITION on a user's Home           | —                             | not server-controllable per user: promoted rows land in Plex's hub order; each user can pin/reorder it in their own client ("Manage Home Screen"). Documented honestly — the row appears, its position varies |

---

## 9. What's verified vs. what Phase 0 must prove

**Verified (don't re-litigate):** the fix timeline + versions (PM-617/PM-5174, public 2026-05-19);
Steve's server on 1.43.3.10793; exclude-labels are the correct mechanism (allow-labels whitelist the
library); plexapi `updateVisibility` exists; Curatarr's restriction PUT pattern works in the field;
Plex history API works per-invited-user with owner token; label restrictions need Plex Pass;
managed-user profile constraint; no competing maintained OSS tool (July 2026 sweep).

**Phase 0 must prove (the one assumption left):** on a real 1.43.3 server, a promoted labeled
collection is invisible on Home/Recommended/**Related** for an excluded _invited_ user and visible
for others. 30 minutes, reversible, on Steve's server with a test account. **This is the go/no-go
for everything above** — and it doubles as the manual dry-run of the Step-5 Privacy Check design.

---

## 10. Execution plan

| Phase                  | Scope                                                                                                                                                                           | Exit criteria                                                                                         | Effort         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------- |
| **0 — Validate**       | Manual privacy test on Steve's server (probe collection + one invited test account + a non-owner viewing account). Scaffold repo (engine/server/web skeleton, CI, ruff/pytest). | Privacy test passes on Home/Recommended/Related; repo builds                                          | ~1 day         |
| **1 — Engine + pilot** | `rowarr/engine` + `cli.py` complete (history→TMDB→LLM→collections→privacy sync w/ snapshots). Cron on plex host via `error_checker.sh`. Roll out 5 → 15 → 40 of Steve's users.  | 40 users have private rows nightly for 1–2 weeks; zero privacy incidents; hit-rate baseline collected | ~1 week + soak |
| **2 — App core**       | FastAPI + DB + scheduler + API; React shell; Dashboard, Users, Runs, Settings on the live engine                                                                                | Steve administers his own server through the UI instead of cron                                       | ~2 weeks       |
| **3 — Onboarding**     | PIN auth, capability probes, wizard steps 0–7, automated Privacy Check (T1/T2/T3), uninstall/rollback flow                                                                      | Fresh `docker run` on a clean test server → rows, no docs needed                                      | ~1 week        |
| **4 — Ship-ready**     | GHCR/Docker Hub images, README + docs site, screenshots/GIF, issue templates, 3–5 external beta testers recruited from r/PleX                                                   | Beta testers onboard unassisted; blockers fixed                                                       | ~1 week        |
| **5 — Launch**         | r/selfhosted + r/PleX posts (lead with the Privacy Check + "works without AI"), Awesome-Selfhosted PR                                                                           | Public v1.0                                                                                           | —              |

Total: **~5–6 weeks** to public v1, with real user value landing at end of Phase 1. Phases 2–4 can
flex around life; the engine keeps running regardless.

**Risks & honest mitigations:** Plex re-breaks label restrictions in an update (weekly verification
catches it; version-pin advisories in README) · LLM cost anxiety (None-mode + spend estimates
up-front) · community AI-skepticism (hallucination-proof design + heuristic mode, messaged loudly) ·
solo-maintainer burnout (small surface, engine/app split keeps core ~1.5k lines, CI does the
drudgery) · a competitor ships first (Phase 1 gives us a working private beta within a week; launch
post can follow in ~a month).

---

## 11. Open decisions (deliberately few)

1. **Name:** working title **Rowarr** (GitHub-free as of 2026-07-12; "Pickarr" is taken by an
   adjacent Claude/Radarr project). Rename is trivial until Phase 4.
2. **Steve's instance cadence:** nightly (recommended) vs weekly — pick during Phase 1.
3. **Acquisition default for Steve's server:** suggest-only vs auto-add capped — pick during Phase 1.
