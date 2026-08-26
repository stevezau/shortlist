# Watch signals — what Plex will tell us, and what to build on it

Status: **design, nothing built.** Everything in "What the server actually offers" was probed against
SFLIX (PMS 1.43.3.10793) on 2026-08-23 with the admin token. Numbers are that server's, not
illustrative.

Written because the membership rule shipped in `82e1bfd` ("only credit a watch if the title was in
their row at the time") is limited by the one watch signal Shortlist reads, and that signal turns out
to be the weakest of the three the server offers.

## 1. What we read today, and what it cannot say

`ShareTokenWatchSource` reads `/library/sections/{key}/all?unwatched=0` per user per section, as that
user, via their own share token (`plex_pms.py:1417`). It yields Plex's binary watched flag plus
`lastViewedAt`, and for shows `viewedLeafCount`/`leafCount`.

Four limits, all load-bearing for attribution:

- **Completions only.** `unwatched=0` filters to `viewCount>0`, and a film's `viewCount` does not move
  until roughly 90% played. Someone who watches 20% of a film is invisible to us.
- **One timestamp per title, and it is the LATEST.** A rewatch overwrites `lastViewedAt`, so the
  original watch date is gone. We cannot ask "when did they first watch this".
- **Show-level only.** We learn how many episodes are watched, never which or when.
- **No incremental filter.** `lastViewedAt>=` is silently ignored by this PMS (live-probed 2026-07-30,
  see `watched_titles`), so the read is done by sorting newest-first and stopping early.

Cost: one paged read per (user, section) — 46 users x 2 sections.

## 2. What the server actually offers

### A. `/status/sessions/history/all` — a durable play log we have never read

- **101,604 entries reaching back to 2020-10-26.** No pruning observed in nearly six years.
- Per entry: `historyKey`, `ratingKey`, `key`, `librarySectionID`, `type`, `title`, `viewedAt` (unix),
  `accountID`, `deviceID`. Episodes add `grandparentKey`, `parentKey`, `index`, `parentIndex`,
  `grandparentTitle`.
- **No `duration`, no `viewOffset`, no `viewCount`.** It says _that_ a play happened and _when_.
- Episodes carry `grandparentKey="/library/metadata/592373"` but **no `grandparentRatingKey`** — the
  show's key has to be parsed out of that path.
- `accountID=` filtering works. `sort=viewedAt:asc|desc` works — both directions verified.
  **`viewedAt>` works**, which the library read does not honour.
- **Pagination needs BOTH `X-Plex-Container-Start` and `X-Plex-Container-Size`.** Size alone is
  ignored and the server returns all 101,604 rows. This is easy to get wrong and expensive.
- Volume for the whole 46-user server: **102 entries in 24h, 474 in 7d, 2,049 in 30d.** An incremental
  read is one small call.
- **Identity is solved here.** 44 of the 45 accounts appearing in the last 30 days map directly to
  `users.plex_account_id`; the 45th has a single entry. This is the plex.tv account id, not a display
  name — which is exactly what disqualified Tautulli as a source.
- The admin token sees every user's history. One call covers the server, replacing 92 paged per-user
  reads.
- **It records completions, not starts.** Verified directly: ratingKey 654993 was being played at the
  moment of the probe, `viewOffset=1988015` of `duration=2717172` (73%), no `viewCount`, and there was
  no history entry for it.
- **Near-duplicates occur**: the same ratingKey for the same account seconds apart (139s observed;
  also two identical rows at the same second on one device). `historyKey` is unique per row and is the
  natural dedupe key.
- Of 133 distinct ratingKeys sampled from the last 30 days, **0** had lost their library item.

### B. `/status/sessions` — live state, and the only place partials exist

- Per session: full metadata, `viewOffset`, `duration`, `sessionKey`, `<User id title>`,
  `<Player state machineIdentifier ...>`.
- **`<User id>` is the plex.tv account id** — verified, `14136324` is `gemnath` in our users table.
- Ephemeral. 13 sessions at probe time. A play that starts and stops between two reads leaves no trace
  here or anywhere else on the server.

### C. The PMS notification websocket — live playback, pushed, no Plex Pass

This is what Tautulli actually uses (`monitoring_use_websocket = 1` in its config on this host, with a
dedicated `plex_websocket.log` showing open/reconnect cycles). Polling `/status/sessions` is only its
fallback (`monitoring_interval = 60`).

Probed directly with a dependency-free client (`ws_probe2.py`): `GET
/:/websockets/notifications?X-Plex-Token=...` returns `101 Switching Protocols` and then pushes
events. In 20 seconds on a normal evening: 19 `playing`, 13 `activity`, 13 `transcodeSession.update`,
2 `transcodeSession.start`, 2 `transcodeSession.end`, 1 `progress`.

A `playing` event (`PlaySessionStateNotification`) carries exactly:

```json
{"sessionKey": "556", "clientIdentifier": "otrlnpwf8nzwmpwzrmglitzs", "ratingKey": "456294",
 "key": "/library/metadata/456294", "viewOffset": 1294833, "state": "paused",
 "playQueueID": 126025, "playQueueItemID": 2670220, "guid": ""}
```

- `state` is `playing` / `paused` / `stopped` / `buffering`, so starts and stops are both visible.
- `viewOffset` is the live position — **this is the only place a partial play exists.**
- **There is no user in the event.** Identity comes from correlating `sessionKey` against
  `/status/sessions`, whose `<User id>` is the verified plex.tv account id. That correlation is the
  whole reason Tautulli keeps its own database.
- No Plex Pass needed, no plex.tv round trip, same admin token we already hold. Strictly better than
  either polling or Plex Pass webhooks, both of which were considered and dropped:
  polling adds an always-on loop *and* still misses short plays between reads; webhooks depend on a
  subscription and on an `Account.id` whose semantics are documented inconsistently.
- Like every live source, it is lossy while we are down. Source 2 remains the backstop.

Scale note: Tautulli's own `session_history` on this host holds 136,571 rows back to 2019-12-22,
against the PMS history log's 101,604. The gap is roughly the partial plays Plex itself never records.

## 3. Why the history log is not "the session poller, polled less often"

This is the distinction the whole design rests on.

- **The history log is a ledger.** Reading it less often costs latency and nothing else — the rows are
  still there, six years deep. Catch-up is idempotent, and our own downtime costs us nothing.
- **Sessions are a window.** The read interval _is_ the data-loss rate. A 20-minute play that starts
  and ends between two polls never existed as far as we are concerned.

They also carry different facts, so no polling rate reconciles them: the history log will never report
a 20% abandon, however often it is read. Tautulli exists precisely because there is no server-side
endpoint for partial plays — it holds the websocket open (section 2C) and persists what it is pushed.
Which is why a Tautulli outage loses plays in exactly the way ours would.

## 4. Architecture

Three sources, one reconcile. Each answers something the others cannot.

### Source 1 — the library sweep (keep, unchanged)

Truth for "has this person watched X". Self-healing and complete: it catches bulk marks, manual
"mark as watched", and anything that happened while every other source was down. Stays the backstop.

### Source 2 — the history log (add; this is the cheap, high-value one)

One admin-token call per sweep: `viewedAt>` the stored cursor, `sort=viewedAt:desc`, paginated with
both container params. Rows land in a new `watch_events` table, deduped on `historyKey`.

What it buys:

- an exact timestamp per play, instead of a `lastViewedAt` a rewatch has overwritten;
- per-episode granularity for shows;
- immunity to our own downtime — the backlog is on the server;
- one call for the server instead of 92 per-user reads.

### Source 3 — live starts, via the websocket (later, only if the data justifies it)

Hold `/:/websockets/notifications` open. On a `playing` event, look up `sessionKey` in
`/status/sessions` to get the account id, and write a `watch_events` row with `source='websocket'`
carrying `viewOffset`.

This is the only thing that can see a 20% start. Not polling, and not Plex Pass webhooks — see
section 2C for why both lose to the websocket.

Costs to weigh before building it: a long-lived connection with reconnect handling, and storing what
people are watching live — a real step up in what a privacy-focused tool knows about its users.

## 5. What this does to the reconcile — the actual prize

Today's rule needs a snapshot of each person's live rows taken _before_ the run rebuilds them
(`RunService.start_run`), because by reconcile time the row has already dropped the title — dropped
_because_ it was watched. That snapshot is a workaround for having only one timestamp and no history.

With real event times the question becomes historical and the workaround disappears:

> credit the pick if there is a watch event at time T where the title was in one of their rows at T.

"In their row at T" is answerable from `picks` + `runs` at any later date — the pick's group had a
delivery from a run started at or before T, and that delivery contained the title. That is exactly the
SQL used to measure this change's impact before shipping it, so it is known to work on real data.

Consequences: the pre-run snapshot goes away; ordering between the reconcile and the rebuild stops
mattering; and a late-arriving event (from a backfill, or after downtime) is attributed correctly
rather than being judged against today's rows.

## 6. Data model

```
watch_events
  id                integer pk
  plex_account_id   integer  indexed        -- joins users.plex_account_id
  rating_key        integer
  show_rating_key   integer null            -- episodes: parsed from grandparentKey
  media_type        text                    -- movie | episode
  viewed_at         datetime indexed
  source            text                    -- history | webhook
  history_key       text null unique        -- dedupe; null for webhook rows
  created_at        datetime
```

Cursor in `settings` as `sync.history_cursor`. Retention: prune alongside `picks`, since nothing older
than the pick history can be attributed anyway.

## 7. Rollout

1. Add the table and the cursor. Backfill from the history log, bounded to the oldest pick we still
   hold rather than all six years.
2. Run event-based attribution **alongside** the current rule, logging the delta. Change no reported
   number yet.
3. Once the delta is understood, switch the reconcile over and delete the snapshot path.
4. Webhooks last — and only if step 2 shows that partial plays are a material share of what we miss.

## 8. Open questions, to settle with a probe rather than a guess

- Whether a `playing` event ever arrives for a session that `/status/sessions` no longer lists
  (a `stopped` state racing the session teardown), which would cost us the identity lookup.
- Does the history log get an entry when a title is marked watched **without** playback?
- Do managed/Home users appear in the history log under their own `accountID`? **Partly answered
  (SFLIX, 2026-08-24, 18,756 events across 51 distinct ids).** Managed users do — one appears under
  its own id and credits normally. But two ids in the log match no user row: `1`, with 3 events from
  December 2025 and March 2026, and `725647550` with 37, almost certainly a share that has since
  been removed. `1` is conventionally the server owner in Plex's history endpoint, and the owner here
  really does have a different id (`5245144`), so an owner watching on the ADMIN account would very
  likely not be credited.

  Deliberately NOT mapped `1` → owner. Three stale events are not enough evidence to attribute
  watches by, and mis-attributing them credits the wrong person — a worse failure than the current
  one, which is that an unknown id is skipped and credits nobody. It does not bite this server: the
  owner is `enabled=False` and the maintainer watches on a non-admin account by policy. To settle it,
  watch one title on the admin account and re-run
  `scratchpad/id1.py` — if the new event appears under `1`, map it and record a fixture (rule 11).
- Is history pruning configurable, or version-dependent? Six years unpruned here is one data point.
