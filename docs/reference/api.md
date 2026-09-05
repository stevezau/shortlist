---
title: "Reference: the REST API"
description: Every Shortlist REST endpoint — sign-in, users, rows, runs, requests, events, reports, the support checks and their responses.
heading: API reference
---

The interactive API docs are off by default (they'd disclose the whole surface unauthenticated);
set `SHORTLIST_ENABLE_DOCS=1` to expose `/api/docs` and `/api/openapi.json` for local development
(also required if you regenerate the frontend API types with `pnpm -C web gen:api` against a live
server). Highlights:

## Sign-in and setup

```
POST /api/auth/pin · GET /api/auth/pin/{id} · GET /api/auth/session · POST /api/auth/logout
     Sign-in is owner-only in BOTH states. Once claimed, the Plex account must match the linked server's owner (403 otherwise). Before it is claimed there is no owner to compare against, so plex.tv is asked directly: an account that owns no Plex server is refused (403) rather than handed a session. Someone who merely has a share on your server can never sign in. If plex.tv can't be reached, or answers with something that isn't a resources list, the sign-in fails closed with 503, never open. **Known gap:** on an unclaimed instance the test is "owns *a* Plex server", not "owns *this* one", so someone running their own PMS can still claim an instance that has credentials seeded from the environment but no server linked yet. Link your server as step 1 and the window closes
POST /api/setup/probe · POST /api/setup/link · GET/PUT /api/setup/state
     `/link` verifies with plex.tv that `machine_id` is a server the caller's account OWNS (403 otherwise). The body's `owner_account_id` is only the caller vouching for themselves — `/servers` lists shared servers alongside owned ones, so without the plex.tv check a friend with a share could link your PMS and become this instance's owner. The wizard shows shared servers but won't let you select one
```

## Users

```
GET  /api/users · PATCH /api/users/{id} {enabled?, manage_sharing?, request_tag?, prefs?} — a `prefs` key sent as `null` CLEARS that preference. Only the keys actually present are touched: an omitted key keeps its stored value, so a partial write cannot wipe the rest. · DELETE /api/users/{id} (only for someone plex.tv no longer lists: drops their picks and run history and hides them from the list; keeps the users row and their pre-Shortlist share-filter snapshot, which uninstall restores from — 409 for anyone still on the share) · POST /api/users/sync (shared + Home users from plex.tv, plus the server owner, whom that list never returns)
POST /api/users/set-enabled {enabled} (bulk enable/disable every user at once)
```

`manage_sharing` (default true) is a SEPARATE axis from `enabled`, not another value of it. `enabled`
decides whether someone gets a row; `manage_sharing` decides whether Shortlist may edit that
account's Plex share filters. Set it false and Shortlist adds no `label!=shortlist_*` exclusions for
them and removes the ones it already added — so that account can see other people's rows unless its
own Plex restrictions stop it. That is the point: an account with its own "allow only" label list is
already kept away from Shortlist's rows by that list, and Shortlist's exclusions were fighting it. Every
OTHER account still excludes this person's own row label, so leaving one account alone never exposes
their row to the rest of the server. Changing it either way rewrites the filters straight away rather
than waiting for a run. Turning someone OFF (`enabled=false`) still writes their filters — it means
"no row for them", not "let them see everyone else's" — unless you have also left their sharing
alone, in which case that wins and nothing is written for them at all.

A **restricted shared row**'s exclusion is the exception that is NOT removed. A shared row limited to
an audience is hidden from everyone outside it by that exclusion alone, so dropping it would hand the
account a row you explicitly restricted; "leave their sharing alone" clears the per-person exclusions
it accumulated, not that. The consequence to know: while an account is left alone, later changes to a
shared row's audience never reach it, so its exclusion can go stale in the direction of seeing less.
Switching management back on resyncs it.

```
GET  /api/users/{id}/rows · PUT /api/users/{id}/rows/{collection_id} {muted?, row_size?, recent_count?} (per-person, per-row: `recent_count` (1–25) overrides how many recent watches the `llm_web` source searches for this person on this row; null on any field clears it back to the row's own setting)
GET  /api/users/{id}/runs (this person's outcome per run — `status`, `reason` for a non-failing skip, `duration_ms`, their diff and picks) · GET /api/users/{id}/runs/summary -> {included, total} (a run is server-wide, so "6 runs" on a person's page only reads honestly next to "of 148")
GET  /api/users/{id}/outcomes -> [{tmdb_id, media_type, title, row, outcome, percent, watched_at, finished_at}] (what became of each pick delivered to this person — `outcome` is `finished|dropped|bounced|watching`, resolved by the same `resolve_outcomes` the dashboard uses, so the two can never disagree)
GET  /api/users/search/titles?q=&media_type=movie|show -> [{tmdb_id, title, media_type, year}] (TMDB's best guess, for the block-a-seed picker)
POST /api/users/{id}/blocked-seeds {tmdb_id, title?, media_type?, year?} · DELETE /api/users/{id}/blocked-seeds/{tmdb_id} -> {blocked_seeds: [{tmdb_id, title, media_type, year}]}
     Titles that must never SEED this person's recommendations. The watch stays in their history, it just stops shaping their picks.
     Stored on `users.prefs`; an install that predates the richer shape holds bare TMDB ids and keeps working unchanged.
GET  /api/users/{id}/history (recent watches read LIVE from Plex; each item carries `title`, `media_type`, `year`, plus `season`/`episode`/`episode_title` for TV)
GET  /api/users/{id}/watched?q=&media_type=movie|show&library=&limit=&offset= -> {items, total, libraries, last_full_sync_at, synced_titles, dislike_threshold, ratings_trusted, rated_count}
     Search this person's CACHED watched set — the same set recommendations are filtered against, so
     it can answer "I watched that, why was it recommended?". Unlike `/history` it never touches Plex,
     so it searches the whole set rather than the newest page, and each item carries `watch_count`
     plus `viewed_leaf_count`/`leaf_count` (null for movies) for the "3 of 8 episodes" progress.
     ONE ITEM PER TITLE. A film or show held in more than one Plex library is cached once per library,
     and those copies are merged into one item whose `libraries` field names the ones it was found in (empty for a watch cached before v1.x, until that person's next
     sync records it). The merge takes the newest watch date, the SUMMED play count, the progress of
     whichever copy they got furthest through — as a pair, so both numbers come from one real copy —
     and the lowest rating any copy carries THAT A PERSON COULD HAVE TYPED (a whole number), because
     a single low rating is what stops a title seeding and `disliked_seed_keys` ignores fractional,
     tool-written values. A fractional one is shown only when no copy carries a typed rating.
     `total` counts titles; `synced_titles` counts stored library copies, so on a server
     holding anything twice they differ on purpose.
     `library` filters to titles held in that library, by its Plex display name. It SELECTS which
     titles appear — each one still names every library it lives in, so filtering to "4K Movies" can
     return a row marked "Movies · 4K Movies", which is the duplicate you were looking for. The
     page-level `libraries` lists every library this person has a watch in as `{name, media_type}`,
     never narrowed by the filter (narrowing it would empty the control that did the narrowing).
     The media type is there so the UI can decide whether to offer the filter at all: on a server
     with one movie library and one TV library, a library dropdown repeats the Movies/Shows buttons
     beside it, so it is only shown once one TYPE holds more than one library.
     `last_full_sync_at` is null while ANY library has never had a full read — the set is incomplete.
     Each item also carries `user_rating` — what THIS person rated it in Plex, 0–10, or null if they
     never did (nearly always). The three page-level rating fields say whether that rating is acting:
     `dislike_threshold` is the cutoff in force (null = `recommendations.use_plex_ratings` is off),
     and `ratings_trusted` is false when this account's ratings look tool-written, in which case none
     of them are used whatever the threshold says. `rated_count` counts the whole set, not the page.
```

## Watching account (the owner's escape from seeing everyone's rows)

```
GET  /api/watching-account/candidates -> [{plex_account_id, title, protected, already_a_shortlist_user}]
     Plex Home users the owner could move their watching to. The admin account is never a candidate.
POST /api/watching-account/transfer {to_user_id, from_user_id?, dry_run?} -> {planned, applied, unreachable, failed, marks,
     unmarks, offsets_set, offsets_cleared, removals_preview, verify_mismatched, verify_checked,
     shows_cleared, target_unreadable, events_copied, titles_cached, snapshot_id, dry_run,
     source_empty, errors}
     Replicates one account's watch state onto the watching account: the exact episodes, the exact
     rewatch counts, and the exact position in anything part-watched.
     `from_user_id` defaults to the OWNER, which is the case the guide walks through. Name a
     different one when the history to copy lives on an account you already moved to — it is read
     with THAT account's own server token, never the admin's, so one person's history can never be
     copied under another's name. The web page offers this as "Copy the history from" once more than
     one candidate exists.
     Repairing an account an older Shortlist over-marked needs no special action: mirroring un-marks
     the spurious watches on the next run, and the preview says so when the removal count is large.
     MIRRORS — `unmarks`/`offsets_cleared` count what it REMOVES from the target because the SOURCE
     account has not watched it. That is what makes the result a replica, and what repairs an account an
     older Shortlist over-marked. Always dry-run first: `removals_preview` names up to 50 of them.
     A snapshot of the target is taken before the first write; `snapshot_id` is what `/undo` needs.
     `unreachable` counts titles in libraries that account cannot see (the PMS 404s) — normal, not a
     failure. `failed` counts writes that RAISED (a timeout, a 500) and is the opposite claim: not
     "that title isn't there for them" but "the write never got an answer". Those stay in
     `verify_mismatched` rather than being excused out of it. `verify_mismatched` comes from re-reading the target afterwards, so it reports what
     actually landed rather than what was sent.
     `source_empty` means the SOURCE ACCOUNT has nothing watched — told apart from `planned: 0`,
     which means the two already match.
     `shows_cleared` counts show rows un-scrobbled because every episode of them was removed — an
     episode un-scrobble does not clear its show, and a show left flagged at 0/N goes invisible to
     the watch cache. `target_unreadable` lists libraries the TARGET cannot see: not a failure, but
     it makes the snapshot partial, so undo is refused for it.
GET  /api/watching-account/snapshots -> [{id, user_id, username, taken_at, entries, complete}]
     Transfers that can still be undone, newest first. Needed because the undo is otherwise reachable
     only from the response of the transfer that created it — and the queue exists precisely so the
     work survives a request timing out, which is the case where that response never arrives.
     Snapshots an UNDO took are excluded: restoring one re-applies the transfer it reversed, and
     without the copied play events it would arrive undated. `complete: false` means a library was
     unreadable when it was taken, so restoring from it could remove watches it never recorded.
POST /api/watching-account/undo {snapshot_id, dry_run?} -> (same shape as /transfer)
     Restores the watching account exactly as the transfer found it, from that snapshot — counts and
     positions included, not just watched/unwatched. Refuses a second time rather than replaying.
     It is a mirror too, so it REMOVES anything watched on that account since the copy; dry-run it
     first and read `removals_preview`. It takes its own snapshot, so an undo is itself undoable.
```

## Privacy status

```
GET  /api/privacy/status -> {read_at, summary, accounts[], rows_on_plex, rows_error, error, enforcement}
     What can be VERIFIED about row hiding right now, for an ordinary shared account — the same
     computation the support page ran, out from behind support mode and rendered at /sharing.
     It reports three things, each a live read: that plex.tv is storing each exclude now, that the rows
     exist on the PMS now, and that Plex was applying them ON HOME for the accounts a run spot-checked
     (naming the run and when).
     It deliberately REFUSES to claim: anything outside Home (Plex's Collections tab has no recorded
     answer, and plex-safety rule 11 forbids guessing one), anything for a parental-profile account
     (plex.tv refuses the write), anything about the owner (no share exists — a Plex limitation, not a
     fault), and "all clear" from a check that did not run or a read that failed.
     Verdicts rank: unreadable > rows_unknown > missing > not_enforced > unhideable > clean. A
     MISSING rule outranks a measured exposure on purpose — it is the one the next run fixes, where
     an exposure needs the owner to change something in Plex. The page never derives "hidden" from
     what Shortlist WROTE — every cell is a live read or the words "not checked".
```

## Rows

```
PATCH /api/collections/{id} {dry_run: true} · DELETE /api/collections/{id}?dry_run=true
     Preview what an edit would do to Plex before it does it. PATCH is the one that matters: narrowing a
     row's media or libraries DELETES its collections in the libraries it no longer covers, and that had
     no preview anywhere. The response carries the projected plan plus `preview_incomplete`, which is
     the difference between "this edit removes nothing" and "the libraries could not be read, so it is
     unknown" — two answers that otherwise arrive as the same empty plan.
     `dry_run` is rejected with 422 on POST: creating a row has nothing to preview, and silently
     ignoring the flag would mean a documented preview parameter that writes.
GET  /api/picks/{rating_key}/poster -> image bytes
     A pick's artwork, proxied from the PMS rather than fetched from TMDB. Every `Pick` carries a
     `rating_key` and only one of the four construction sites carries a `poster_path`, so the PMS is the
     only source that covers all of them — no new column, no migration, no backfill gap. Owner-gated,
     and it refuses any thumb path that is not on this server.
GET/POST /api/collections · PATCH/DELETE /api/collections/{id} (incl. `request_tag`, `candidate_sources`, `library_keys`, `max_seeds` — how many watched titles the row is built from (1–100; null inherits the engine default of 30), `recency` — how much a title's release date counts when ranking it for this row (0.0–1.0; null inherits the global `recommendations.recency`), `cold_start` — what the row does for someone below `recommendations.min_history` (`popular` | `skip`; null inherits the global `recommendations.cold_start`), `fallback_name` — what to call this row for someone whose name cannot be filled in, i.e. a `{top_seed}` row for a person with nothing watched. `""` (the default) means there is no such name and the row is simply not built for them — Shortlist never invents one, and a value containing `{top_seed}` is refused because it could not be filled in either, `seed_window` — how many recent watches a one-title row cycles between, one per run (1–20, default 1 = always their most recent; no global to inherit), `pick_order` — how the delivered collection is ordered (`best` | `rating` | `newest` | `shuffle` | `new_first` — titles that arrived this run lead | `rotate` — the front advances by one title a day, default `best`), `show_days` — which days the row appears, as ISO weekdays 1=Mon..7=Sun (`[]` = every day; values outside 1-7 are refused, and the list is stored sorted and de-duplicated). The response also carries read-only `shown_today`, resolved on the SERVER's clock so a UI badge cannot disagree with what Plex is showing, `hub_anchor` — per-row shelf-placement override, and `poster` — custom row artwork {mode: ""|upload|generate, title, subtitle, style})
GET  /api/collections/{id}/effectiveness -> {delivered, watched, finished, first_delivered_at, matured_days, matured, per_library} (has this row actually landed? `matured` is null until picks are old enough to judge — a pick counts as a hit only if watched while the row was still showing it, so a newer row is reported as "too early" rather than scored 0%)
     `finished` accompanies every `watched` here too, including per library. A row spanning Movies and TV can land the same share in
     both and finish almost none of the TV — that gap is the panel's most useful line, and it is invisible in `watched` alone.
     `rewatch` (bool, default false) makes a REWATCH row: already-finished titles are ordered FIRST and unwatched ones only fill what is left.
     `watched_pct` cannot express this — it is a ceiling, so the ranking shows unwatched titles first and merely PERMITS finished ones; even at 1.0 a
     library with plenty of unwatched candidates yields a mostly-unwatched row. Setting `rewatch` also keeps finished titles in the row's candidate
     POOL regardless of `watched_pct`, so the two rows do not share one pool.
     `unstarted_only` (bool, default false; accepted on any row that can hold shows) drops every series the person has started, however little of it.
     It only changes anything on a row whose `watched_pct` is ABOVE 0: such a row caps FINISHED titles and so still admits a series someone is three
     episodes into, and this is what makes "a series to start" literally true there. At `watched_pct` 0 the row already excludes started series (see
     "What 'already watched' means for a show"), so the flag is a no-op. Refused for `media: "movie"`, where any view is already a finish.
     The finished bar itself is not configurable — it is `EngineConfig.watched_show_pct`, fixed at 0.8. Earlier revisions of this document cited a
     `recommendations.watched_show_pct` setting; no such key has ever existed.
     Both are refused (422) in combinations that cannot work: `rewatch` + `unstarted_only` together (they ask for opposite things — the row would fill
     with titles nobody has seen, under a "you've already seen" name), and `unstarted_only` on a `media: "movie"` row. PATCH validates the MERGED row,
     not just the fields sent, so neither invalid pair can be reached one field at a time.
POST /api/collections/{id}/cleanup {dry_run?} (remove this row's Plex collections for everyone; dry-run previews)
POST /api/collections/{id}/poster/upload (multipart image) · GET/DELETE /api/collections/{id}/poster/image (serve/remove uploaded artwork) · POST /api/collections/{id}/poster/preview {title,subtitle,style} -> generated sample image
```

## System, jobs and libraries

```
GET  /api/system/image-provider -> {capable, provider, reason} (can the AI provider generate poster images — drives the row editor's Generate gate)
GET  /api/system/logs?level=&q=&limit= (parsed + redacted log lines) · GET /api/system/logs/download (all log files as a zip; credentials, addresses and this server's machine id removed — the live view above strips credentials only, since it renders on the owner's own screen where the address is what makes a line readable)
GET  /api/system/libraries -> [{key, title, type}] (the server's Plex libraries, for the row editor)
GET  /api/system/jobs?kind=&limit=&before_id=&status=&exclude_routine= -> [{id, kind, payload, result, status, attempts, max_attempts, detail, error, created_at, started_at, finished_at}] (background maintenance history, newest first; `kind` narrows it to one job type, which is how the Jobs page shows a single job's own history; `status` narrows it to one of `queued`/`running`/`done`/`failed` and anything else is refused with 422 rather than ignored — the Jobs page's "N failed" badge counts every failed row in the table, so its list has to be able to reach past the newest page; `exclude_routine=true` drops the high-volume automatic kinds unless they FAILED — `watch.reconcile` alone is one job per playback stop, measured at 165 of the 197 jobs a 46-user server queued in a day, so it is what the header's activity feed passes and the Jobs page does not; runs have their own page)
GET  /api/schedule -> {jobs[{kind, label, setting, cron, using_default, default_cron, optional, writes_plex, next_run}], rows[{cron, rows[], next_run}]} (everything on a timer, rows grouped by shared cron exactly as the scheduler groups them. One trigger builds all of them). `cron` is the EFFECTIVE one with defaults resolved; `default_cron` is the built-in it falls back to when nothing is stored, and `using_default` says whether that is what it is running on. Read-only: crons are still edited through PUT /api/settings and PATCH /api/collections, so each one is validated in exactly one place
GET  /api/system/jobs/catalog -> [{kind, label, description, manual, trigger, scheduled, next_run, last, total, queued, running, failed}] (every job Shortlist can run, with its schedule, its tallies and its most recent run — the Jobs page renders straight from this, so labels can't drift from the handler registry)
POST /api/system/jobs {kind, payload?, background?} -> the job after an inline drain, or as soon as it is queued when `background` is set (the Jobs page uses that and polls, so a slow job can't end in a proxy timeout that reads as a failure). Only `sync.users`, `sync.history`, `sync.check`, `privacy.sync`, `backup.take` and `maintenance.prune` (retention trim of old runs, picks, log lines and expired caches — touches Plex not at all) may be triggered by hand. All of them passes that bring things back to how they should be that take no target. The rest are queued by the mutation that knows their target and are rejected here with 422: `user.cleanup` (someone turned off), `user.hide`/`user.restore` (paused/un-paused), `row.reconcile` (a row deleted, switched off, narrowed to fewer libraries, or with someone dropped from its audience). Queued work waits for any run in progress, is retried with backoff, survives a restart, and raises a notification if it gives up
GET  /api/system/libraries/{key}/collections -> [{title, on_shelf}] (a library's FOREIGN managed collections — the title-anchor choices for row placement; Shortlist's own are excluded, since a row is anchored to another row by slug via `hub_anchor[library].row`. `on_shelf` is false only for a collection Plex reports as promoted nowhere: it has no position to sit next to, so it cannot anchor anything and the editor offers it greyed out. Plex's own built-in hubs are always true)
GET  /api/system/owned-collections -> {collections:[{library,title,label,rating_key,kind,slug,orphan}], total, orphans} (cleanup audit: every shortlist-labelled collection ON PLEX, drift-flagged, DB-independent)
```

## Runs

```
GET  /api/runs?limit=&collection=&before_id= (newest first; `before_id` pages backwards) · GET /api/runs/summary · GET /api/runs/{id} (each user carries `status`, `error`, `reason` — why a `skipped` user built nothing — `has_trace`, and `cost` — that person's timing and token spend for this run, `null` on a run recorded before this was measured, which must render as "not recorded" and never as `0s`; when present, `{setup_ms, rows: {row_slug: {duration_ms, blocked_ms}}, pools: [{label, tokens, exa_searches, duration_ms, rows}]}` — `setup_ms`/`pools` are the person's shared spend (history fetch + candidate gather), repeated across every row because it belongs to none of them; each row's own `duration_ms` is wall-clock INCLUDING `blocked_ms` (time spent waiting on the shared Plex write lock at concurrency above 1), so that row's own work time is `duration_ms - blocked_ms`; tokens are reported per POOL, never per row — all AI spend happens in the pool-scoped candidate gather, and pools are shared between rows, so a per-row token figure would be an allocation invented by the API rather than a measurement; `pools[].rows` names every row slug that drew on a given pool) · GET /api/runs/{id}/users/{user_id}/trace -> {username, display_name, status, error, reason, trace, breakdown} (the full per-user pipeline trace. History (with true distinct-title watched totals per library, split by media type) / seeds with each seed's weight ingredients, each source's queries+returns tagged with their fate (kept / already_watched / not_in_your_libraries / excluded_genre / lost_ranking_cutoff), the web-search/RAG prompts, resolved vs. hallucinated titles (the AI's resolved proposals carry the same fate so the UI marks each kept vs. dropped), plus `error`/`reason` for a failed or skipped person and `breakdown` (the delivered picks per library); a cold-start user carries a trace too (their thin history + a synthetic `cold_start` source), so `has_trace` is set and the "How we picked" page renders for them; fetched on demand, `trace: {}` on runs predating the feature) · GET /api/runs/{id}/log?after_seq=&format=json|text (the run's activity feed, kept in `run_log_lines` so an older run still has one; `after_seq` returns only what is new, `format=text` is the download) · POST /api/runs {user_ids?, collection_ids?, dry_run?} · POST /api/runs/{id}/cancel · DELETE /api/runs (clear all run history; changes nothing on Plex, and no longer disarms the row reconciles — they address collections by label + rendered title, not by run history)
```

## Requests

```
GET  /api/requests?wanted_by=&wanted_by= (the inbox, pending first then sent then rejected, capped at 500 rows; `wanted_by` repeats one `wanters` username per value and keeps a title any of them wanted — applied BEFORE the cap, so picking a name searches the whole history rather than the 500 the page loaded; omitted = everyone) · GET /api/requests/status -> {statuses: {request_id: "downloaded"|"downloading"|"queued"|"unmonitored"|null}, radarr: "ok"|"unreachable"|"off", sonarr: same} (live Sonarr/Radarr status for WAITING and SENT items — rejected are skipped; null = the app is fine and doesn't track it, which is why `radarr`/`sonarr` report reachability separately: an app that never answered would otherwise be indistinguishable from one with nothing to say. Fetched separately so the list itself makes no Arr calls, and read from whole-library maps so the cost doesn't scale with inbox size — which is what makes the inbox's poll cheap — it runs every 10s only while a title is actually downloading, and every 30s while an app is unreachable so the badge clears itself when it comes back; a settled inbox does not poll at all) · POST /api/requests/send {ids, dry_run?} · POST /api/requests/reject {ids} (permanent) · POST /api/requests/restore {ids} (un-reject → back to Waiting) · POST /api/requests/delete {ids} (removable; can re-surface) · POST /api/requests/clear {ids} (hide SENT items from the log without un-sending — the tombstone stays so the title isn't re-requested)
```

## Events and notifications

```
GET  /api/events (SSE) · GET /api/events/log?scope=&limit=&before_id= (audit feed; `before_id` pages backwards — a cursor rather than an offset, since events are appended while you read)
GET  /api/notifications -> {items[]} · POST /api/notifications/dismiss {id} (dismiss one alert)
     One of them is "Playback tracking is offline", raised when the PMS notification socket has been unreachable for 45+ minutes.
     Not dismissable (like "Runs are paused"): silencing it would leave you believing a feature is running that isn't. It clears
     itself on reconnect and raises again on a new outage — being undismissable, it is never hidden in the first place.
     The threshold is deliberately long — a container restart takes seconds and a Plex restart a minute or two, and alerting on
     those trains you to ignore the bell. A dropped socket is normal: the listener retries for ever, backing off 5s -> 120s.
     A socket that CONNECTS and immediately drops does not reset the clock: the handshake succeeding is not the same as frames
     arriving, and since the backoff caps at 120s, every flap cycle is shorter than the threshold — so a server flapping all
     night would otherwise have looked healthy. A connection has to survive 60s to count as recovery.
     An unreachable-but-configured Plex counts as an outage too; an install that has not finished setup does not.
     Worth alerting at all because the failure is SILENT: the nightly play-log sweep still credits finished watches, so every
     number keeps looking plausible. What stops is the partial-watch signal, which only the live socket can see — and
     "nobody abandoned anything this week" looks exactly like a healthy week.
```

## Outgoing notifications

```
Settings -> System -> Notifications, or `notify.webhook.enabled` / `notify.webhook.url`.
     A whole run failing POSTs generic JSON {source, version, id, severity, title, message, path, sent_at}
     to one webhook — the gap being closed is that a run failing overnight was visible only to someone
     who opened the app. Delivery reuses the existing job queue, so retry, backoff and the dead-letter
     state are the ones already tested rather than a second mechanism.
     `notify.webhook.url` is a SECRET (a Discord or Slack webhook URL is a bearer token in a URL): Fernet
     at rest, redacted from `GET /api/settings`, and stripped of its path and query before any exception
     text reaches a log, a `Job.error`, the audit trail or the support bundle.
     The "Send a test" button travels the exact same code path as a real 3am failure — same settings
     read, same body builder, same HTTP call — so a passing test cannot mean a broken channel.
```

## Settings and connections

```
GET/PUT /api/settings · POST /api/settings/test/{plex|tautulli|tmdb|llm|radarr|sonarr|overseerr|mdblist|trakt|exa} (a PUT that changes anything also writes a `settings.change` audit event carrying `{changed: {key: {from, to}}, actor: {via, account_id, client}}` — `changed` covers the changed keys only (secrets record `<redacted>` on both sides, long object values are summarised), and `actor` says WHO: `via` is `browser` or `api_token`, `client` is a truncated User-Agent. No client IP is recorded, deliberately: these rows are immutable and the support bundle exports them. Read it back with `/api/events/log?scope=settings.change` to see which thresholds a past run actually used, and what changed them)
GET  /api/settings/arr/{radarr|sonarr}/options -> {quality_profiles, root_folders}
GET  /api/settings/overseerr/options -> {users[], default_user_id} (the instance's accounts, for the "request as" dropdown, each with whether it auto-approves films/shows and whether it belongs to a real person; `default_user_id` is the account the API key itself is, so the UI can resolve "Server default". No profiles or folders — those are Overseerr's own)
POST /api/settings/curator/models {provider?, api_key?, ollama_url?} -> {provider, models[]} (models the provider offers; the body lets the picker list the provider being edited before it is saved — blank fields fall back to saved settings, a redacted key means "use the saved key"; [] = free-text fallback)
```

## Reports and the dashboard

```
GET  /api/report?window=7|30|90|all -> {window, since, first_pick, overall, trend[], per_user[], per_row[], recent[], watch_sync, coverage, runs, requests, top_titles} (what got watched, from picks.watched_at)
     Windowed, default 30 days, with each headline figure carried alongside its previous equal period so the UI can show a change.
     `requests.watched_after_sent` compares a watch against `request_candidates.sent_at`, stamped once when the status flips
     to "sent" (rows predating that column fall back to `updated_at`).
     `first_pick` is the oldest pick on record (null when there are none). On a young install every window already covers all the data, so 7/30/90/all
     return identical numbers and the selector looks broken; the UI compares `first_pick` against `since` to say why rather than leaving it a mystery.
     `first_pick` also gates the comparison itself: `watched_prev`, `watchers_prev` and every `*_delta` are **null** unless the previous period is one
     Shortlist was installed for its whole length. A previous window that reaches back before the first pick would be counting a month the app did not
     run in, which reads as growth — a real server showed "53 watched, +53 vs previous" the day after its first-ever pick fell inside the comparison.
     Partial coverage is excluded for the same reason: it undercounts the earlier period, so every delta would lean toward good news. The UI says
     "no earlier period yet" rather than printing a comparison with no number in it.
     `overall.bounced` / `overall.dropped` split the picks that were STARTED and abandoned, by how far they got (under 5%, and past it).
     They come from live playback (`watch_sessions`), not from Plex's watched flag, which cannot see a partial play at all — so both are 0
     until the playback listener has observed some, and a title nobody has played since tracking began is in neither.
     SHARED rows count here too. They write no pick rows, so their credits live in `shared_row_watches` and are folded into the same
     per-(person, title) outcome — a title on both a personal and a shared row is one thing that person watched, counted once.
     A shared-row credit needs a play the delivery ledger and the run's own audience snapshot both agree that person could see at the time;
     Plex's watched flag alone never credits one, because everyone sees a shared row and a popular title would otherwise credit for everyone.
     Un-watching in Plex WITHDRAWS a credit, but only one that Plex's flag was the sole evidence for. Every sync
     withdraws — un-mark something tonight and the credit is gone on the next sync, not up to a week later. A credit
     backed by observed playback — a live session or a play-log entry — is kept: it is a fact
     about a moment, not a mirror of a checkbox, and a partial watch never sets the flag at all, so withdrawing on absence
     would delete the exact signal this feature exists to capture. Credits older than 30 days are left alone as settled
     history. Nothing is withdrawn for someone whose read came back empty, or whose read could not be proven complete —
     if one of their libraries was unreadable that night, absence from it is not evidence and their credits are untouched.
     Resuming later needs nothing special: progress is the furthest across ALL sittings and only ever moves up, so a second
     sitting extends the percentage, while the credit stays pinned to the first time they pressed play.
     Every figure that counts a WATCH includes them: `overall.watched`/`finished`/`watchers`, `bounced`/`dropped`, `trend`, `per_user`, `per_row`,
     `top_titles`, `recent` and `requests.watched_after_sent`. The ones that do not are `delivered`, `landing` (a ratio of delivered to watched) and
     `avg_days_to_watch` (an interval that starts at a per-person delivery): a shared row is ONE
     collection for the whole server, so there is no per-person delivery to count and inventing one would be a number with no referent. A shared
     row's `per_user`/`per_row` line therefore shows watched and finished with no "delivered" clause, which the UI already omits when it is zero.
     `overall.landing` is the one RATIO, and it is computed over a MATURED cohort: picks delivered in the window AND at least 30 days
     old. That matters — a pick stops being creditable once its row drops it, so counting a pick delivered
     yesterday in the denominator drags the rate toward zero for no reason. `per_user`/`per_row` return COUNTS, not rates, sorted by
     what was actually watched: at these sample sizes a percentage is noise, and sorting by one put `1/31` above `3/103`.
     Every count comes in a WATCHED/FINISHED pair (`overall.finished`, `landing.finished`/`finished_rate`, and a `finished` on each
     `per_user`, `per_row` and `trend` entry). `overall.finished` carries NO `_prev`/`_delta`, unlike its neighbours: this window's
     finishes are counted as of now while the previous window's have had an extra period to complete, so a server behaving perfectly
     steadily would report a permanent decline. The level is honest; a shifted-window change is not. `watched` is Plex's own flag, which for a SERIES flips on the FIRST finished episode —
     so one episode of a 60-episode show scores exactly like a whole film, and a TV row therefore out-scores a movie row for a
     structural reason rather than a real one (measured on a 47-user server: only 21 of 158 credited show picks had been finished).
     `finished` is the stricter count — a film played, or a series with every episode watched. See
     [Watched vs finished](concepts.md#watched-vs-finished) for why Shortlist gets to choose the threshold. Sorting still uses `watched`, deliberately:
     ranking by `finished` would bury every TV row under every movie row.
POST /api/report/sync -> 202 (kick off a watch-history sync — re-reads every user's watched set from Plex so hit rates and "N titles watched" stay fresh between runs; writes nothing to Plex)
GET  /api/report/engagement?window=7|30|90|all -> {window, people[], losing[], stop_points[]} (what people DID with their picks: per person with how far
     each got, the titles several people start and few finish, and where abandons cluster). Outcomes are per (person, title): finished | dropped |
     bounced (under 5% in) | watching (credited, but no live session ever said how far). `percent` is null where no session observed the play —
     which is not 0%, and is the normal state for anything watched before playback tracking was running.
GET  /api/report/deleted-rows -> [{slug, picks, first_seen, last_seen}] (history left behind by rows that no longer exist, biggest first; NOT windowed — "what can I clear" is a question about all of it)
DELETE /api/report/deleted-rows?slug= -> {cleared, picks, slugs[]} (permanently delete that history; omit `slug` to clear every deleted row)
     Eligibility is recomputed server-side from `collections` vs the slugs in `picks` AND `shared_row_watches`, so naming a
     live row's slug deletes nothing and returns `cleared: 0` rather than erroring. The DELETE re-checks it in the same
     statement (`NOT EXISTS`), which closes the window in which a row re-created between the two would be treated as an orphan.
     `picks` rows AND `shared_row_watches` rows are removed — a SHARED row writes no picks at all, so its credits are the only
     history it has. The `picks` FIELD in both responses counts both (the name is kept for wire compatibility); one slug can
     carry both kinds, because a row's `build` can be switched from per-person to shared, and the GET's number is always what
     the DELETE will remove. `deliveries` is deliberately untouched — it is the ledger of which Plex collection is
     which row, and clearing it would strand a real collection with nothing left to clean it up. Audited as
     `report.clear_deleted_rows`, splitting the total into `pick_rows` + `shared_watches` with a per-slug count.
```

## Health, tokens and onboarding

```
GET  /api/system/health -> {status} (the ONE unauthenticated endpoint — liveness only, for Docker's HEALTHCHECK; the version lives on the owner-gated /system/version)
GET  /api/system/api-token -> {enabled, created_at, token} (owner-gated; token revealable) · POST /api/system/api-token -> {token, created_at} (generate/replace) · DELETE /api/system/api-token (revoke)
GET  /api/setup/servers (Plex server picker during onboarding) · GET /api/setup/state
POST /api/system/uninstall {confirm, dry_run?} -> {filters_restored, filters_skipped[], filters_unreachable[], filters_failed[], collections_deleted[], rows_disabled, dry_run, message} (the trust feature: switches every row off and clears its schedules, deletes every Shortlist collection, then restores each account's share filters from its pre-Shortlist snapshot — in that order, so the excludes hiding a row are never removed while the row still exists. `dry_run` previews; the real thing needs the literal string UNINSTALL, and 409s while an engine run is in flight. No single account can stop it: one that has left this server is reported in `filters_skipped`, one plex.tv's roster omits that Shortlist's own records say is here in `filters_unreachable` (worth retrying — that is what a partial roster read looks like), and one plex.tv refuses in `filters_failed`. A write plex.tv accepted but that could not be verified is still audited, with what was sent)
```

## Support checks ("Have an issue?")

Read-only diagnostics behind `/issue` in the UI, listed below. **Nothing here writes** — not to Plex, not
to plex.tv, not to the settings a run reads. The only mutations are the mode's own switch and the
audit rows it leaves.

Two gates, not one. Every tool needs the owner session AND support mode switched on; the mode lapses
by itself after 24 hours. Being off by default matters because this surface reads share filters and
per-user tokens, and an install that never files a bug report should never expose it.

```
GET  /api/support/status -> {enabled, expires_at, seconds_remaining}   (owner only; usable while the mode is off)
POST /api/support/enable -> switches the checks on for 24h (audited: events scope `support.enable`)
POST /api/support/disable -> switches them off now
```

Everything below additionally requires the mode to be on, and returns **403** when it is not. Each
response carries its own fields plus `text`: a fixed-width block, ≤76 columns, that the UI's "Copy
for support" button puts on the clipboard verbatim. The block is rendered server-side so the format
a maintainer reads is decided (and tested) in one place. Credentials are stripped centrally before
anything reaches it, including from quoted exception messages.

```
GET  /api/support/health -> {checks[{name, ok, detail}], text} (Plex, libraries, tokens, TMDB, curator, database, clocks, last run — each probed independently so one failure is content, not a 500)
GET  /api/support/title?q= -> {rows[{user, watched_record, viewed_leaf_count, leaf_count, counts_as_watched, cap_pct, delivered[], problem}], flagged[], text}
GET  /api/support/person/{slug} -> {user_type, watched_movies, watched_shows, libraries[{section_key, library, titles_known, ever_read}], never_read[], text}
GET  /api/support/rows -> {rows[{slug, watched_pct, watched_pct_source, refresh_days, refresh_days_source, idle_hold_days, idle_hold_source, rewatch, unstarted_only}], global_watched_pct, text}
GET  /api/support/row-schedule -> {rows[{slug, refresh_days_source, rebuild_every_days, idle_hold_days, idle_hold_source, last_built_at, days_since_built, due}], text}
GET  /api/support/libraries -> {libraries[{key, title, type, items}], error, text}
GET  /api/support/connection -> {users[{user, has_token, libraries_read[], never_read[]}], problems[], text}
GET  /api/support/read-as?user=&endpoint=&section= -> {status_code, total_size, body, sections[], choices[], text}
GET  /api/support/sharing -> {accounts[{user, shortlist_excludes[], other_conditions[], should_hide[], missing[]}], rows_on_plex[], rows_error, missing_excludes_for[], error, text} (measured against the rows that EXIST on Plex, not the enabled-user list; reports `rows_error` rather than health when that read fails or comes back empty while marked rows exist)
GET  /api/support/surfaces -> {rows[{library, title, label, marked, rating_key, recommended, own_home, shared_home}], owner_label, on_owner_home[], on_owner_shelf[], unlabelled[], error, text} (the live Plex promotion flags per row: `own_home` on someone else's row is always a bug, since no share filter applies to the owner; `recommended` on one is the documented Plex limitation and a settings change)
GET  /api/support/drift -> {ledger_count, plex_count, marked_count, missing_on_plex[], orphans_on_plex[], error, text} (`marked_count` counts rows by invisible title marker, so "the rows were deleted" and "their labels are unreadable" stop looking identical)
GET  /api/support/pick?user=&title= -> {picks[{row, rank, seed_title, sources, affinity, reason}], text}
GET  /api/support/missing?user=&title= -> {verdict, hits[], run_id, text}
GET  /api/support/funnel?user= -> {stages[{pool, pooled, disposition{}}], delivered, run_id, text}
GET  /api/support/ai?user= -> {provider, model, llm_tokens, by_step{}, error, text}
GET  /api/support/timeline?user= -> {entries[{at_utc, at_local, kind, what}], text}
GET  /api/support/settings-history -> {changes[], last_build_at, change_after_last_build, text}
GET  /api/support/jobs -> {jobs[], counts{}, failed, text}
GET  /api/support/clocks -> {tz, local_now, utc_now, offset_hours, scheduled[], text}
GET  /api/support/database -> {head, tables_present, tables_expected, missing_tables[], indexes, size_mb, text}
GET  /api/support/config -> {settings[{env, key, env_set, secret, value, has_value}], text}
GET  /api/support/bundle.txt -> text/plain; every server-wide block in one downloadable file
GET  /api/support/report.zip -> the bundle plus every log file, redacted, as one attachment
GET  /api/support/suggestions -> {users[], titles[], libraries[]} (type-ahead for the inputs above; owner-only, never part of a report)
```

**What a report masks.** Credentials (rule 9), IP addresses, and this server's machine id — in the
report body, in every quoted exception, and in every log file inside `report.zip`. A URL keeps only
its scheme and port (`https://<host>:32400`), which are the parts with diagnostic value. The machine
id is additionally removed by exact match rather than by pattern alone, because it reaches a log line
URL-encoded (`uri=server%3A%2F%2F<id>%2F…`) where a word-boundary pattern cannot see it.

This is a filter for the shapes Shortlist knows about, not a proof of absence, and the docs should not claim
otherwise — a promise the code cannot keep is what gets a report pasted unread. Log files are the
weak spot: they carry whatever a dependency chose to print. Every leak found so far arrived in an
escaping nobody had thought of (`%2F`, then `%252F`, then a `plex.direct` hostname with the id
embedded), so assume the next one will too. `tests/unit/test_redaction.py` is where a newly found
shape gets pinned.

**People are named, deliberately.** The report prints each account's Plex username and slug — a
maintainer cannot follow one person through a report otherwise, and the slug is what appears on Plex
as `shortlist_<slug>`. Anyone who would rather not publish them can replace them before posting. The
report BODY does not print nicknames or friendly names, which are free text and routinely someone's
real name; that is a property of the renderers, pinned by `TestNoDisplayNameReachesTheReport`.

That guarantee stops at the report body. The LOG FILES bundled in `report.zip` are not filtered for
names and nothing masks them — `redaction.py` knows about this server's own host and machine id, not
about people. A row template containing `{user}` renders as the nickname (`UserProfile.display_name`),
and the rendered title is logged verbatim on every delivery, so the nickname is in the logs. Say so
in the UI and the guide rather than implying the whole artifact is covered: the previous wording put
nicknames in a "Masked" column, which is exactly the overstatement that gets a zip pasted unread.

(An `anonymise=true` mode existed briefly in 1.2 and was removed before release: it governed only
these two endpoints, not the per-check `text` blocks beside them, so a tickbox reading "hide
everyone's names" covered less than it appeared to.)

**`read-as` runs against an allowlist, never a URL you supply.** `endpoint` is one of `libraries`,
`watched-movies`, `watched-shows`, `home-rows`, and `section` is validated against the keys the PMS
itself just reported. The container sits on a home network, so an arbitrary-URL fetcher behind owner
auth would be a port scanner with extra steps. It refuses (409) rather than falling back to the
owner's token when a person has no share token — reading as the owner would answer a different
question while looking like it worked.

**`drift` reports, it never repairs.** It also refuses to call anything missing when the Plex read
itself failed: an unread server is not an empty one, and treating it as one would mark every
delivered row as missing.
