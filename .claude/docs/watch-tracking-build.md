# Watch tracking — build spec

The plan of record for capturing **starts** and **partial watches**, and for crediting a pick to the
moment someone pressed play rather than to the state of their row now.

Companion to [watch-signals-design.md](watch-signals-design.md), which records what each Plex API
actually returns (probed, not assumed). **Read that first** — this spec does not repeat the field
lists, the pagination trap, or the identity findings.

Status: **spec. Nothing built.** Written before implementation deliberately, so the phases stay
separable and nothing is rediscovered halfway through.

## 1. What we are trying to be true

1. **A start counts.** If someone begins a title that was in one of their rows _at that moment_ —
   their own row or a shared one they can see — the pick is credited. Finishing it four days later,
   after the row moved on, does not un-credit it.
2. **A drop is its own outcome.** Started-and-abandoned is not a miss; it means the row got them to
   press play and the title lost them. That is a signal about the pick that a title nobody opened
   cannot give.
3. **Nothing here writes to Plex.** Every source is a read. Refilling a row the moment someone
   finishes something is a real idea and is deliberately OUT of scope — it is a Plex write on a live
   event, which needs the writer lock and the leak-safe ordering, and belongs in its own change.

## 2. Outcome model

Per (person, title), exactly one of:

| outcome    | means                                |
| ---------- | ------------------------------------ |
| not opened | delivered, never played              |
| bounced    | started, got under 5% in             |
| dropped    | started, got past 5%, never finished |
| finished   | met the completion bar               |

`bounced` and `dropped` are split because they say different things: opening and closing something
inside a couple of minutes is "wrong pick entirely", while abandoning at 40% is "fair go, didn't
hold me". If the data shows the split is noise, collapse it — but it costs nothing to record both
and cannot be recovered later if we throw the offset away.

Completion keeps its existing definition and its existing source: `WatchedItem.is_finished` off the
library read (a series needs every episode; a film is Plex's flag). Position data does not change
what "finished" means in this change.

## 3. The attribution rule

> Credit the pick if there is a watch event at time **T** where the title was in a row visible to
> that person at **T**.

This replaces "is it in their row now", and with it the pre-run snapshot in `RunService.start_run`.
The snapshot only ever existed because we had one timestamp per title and no history: being watched
is what makes the engine drop a title, so by reconcile time the row no longer lists it. Asking about
the past removes the ordering problem entirely, and a late event — a backfill, or a catch-up after
downtime — is attributed correctly instead of being judged against today's rows.

### 3.1 Per-person rows

A row's contents at T come from `picks` + `runs`: within a `(user_id, collection_slug, section_key)`
group, take the newest delivery whose run started at or before T, and ask whether the title was in
it. This is the query already used to measure the last change's impact before shipping it, so it is
known to work on real data.

Detached picks (`run_id IS NULL`, from `DELETE /api/runs` or the retention prune) cannot be placed in
time and are not creditable. Unchanged from today, and documented in `live_pick_ids`.

### 3.2 Shared rows — and why they are NOT fanned out

A shared row is one collection for the whole server, built from pooled history. It has no per-user
pick rows, and `RunSharedRow`'s own docstring records why: `PickRow.user_id` is non-nullable and
RESTRICT-keyed to a real account, and "inventing nullable-user pick rows would put rows nobody
watched into every per-user hit-rate and history query". Fanning 15 picks out across 46 accounts
would also add ~690 rows per shared row per run to the largest table in the schema, and would claim a
personally-curated pick where none exists.

So shared rows are credited **at row level, not per person**: a shared row's job is server-wide, and
its honest metric is "how many people started something from it", not a per-person hit rate.

Membership at T for a shared row:

- the newest `run_shared_rows` entry for that `collection_slug` whose run started at or before T
  contained the title (its `picks` JSON — no new table, this is a handful of rows per window); **and**
- the row was visible to that person: `audience = 'everyone'`, or they were in `collection_audience`.

**Blocker, found on the real database: that JSON has no id to match on.** `_pick_dicts`
(`run_persistence.py:610`) stores `rank`, `title`, `reason`, `seed_title`, `sources`, `affinity`,
`year`, `rating` — and neither `tmdb_id` nor `rating_key`. Verified against SFLIX, whose live shared
row `popular_library_name_on_sflix` has 11 run records, every pick reading
`{"rank": 40, "title": "The Devil Wears Prada", ... "year": 2006}`. A watch event arrives carrying a
`rating_key`, so there is nothing to join on, and title+year matching is the kind of guess this
codebase has been bitten by before.

Fix: add `tmdb_id` and `rating_key` to `_pick_dicts`. It is a JSON column, so no migration — and it
improves `RunUser.picks` in the same stroke. Forward-only: existing rows stay unmatchable, which
costs nothing because shared-row crediting does not exist yet.

Verify at build time rather than assume: that `Pick.rating_key` is actually populated on a shared
row's picks. `_previous_picks` deliberately carries `rating_key=0` for per-person picks and remaps at
delivery, so a shared row may do the same. `tmdb_id` is the reliable key either way — match on
`rating_key` when it is non-zero, else map through `tmdb_id`.

Audience is current state with no history, so a person added to a subset row today would wrongly
credit their older watches. Fixed by snapshotting the audience onto the run: a nullable
`run_shared_rows.audience` JSON column, written at persist time. NULL (every pre-migration row) falls
back to the current audience, which is exactly today's approximation and no worse.

### 3.3 Joining a watch to a pick — measured

A watch event carries a `ratingKey`; `picks.rating_key` is what we stored at delivery. Both sides
join, but **not on the episode key alone**. Run over 30 days of SFLIX history against the 15,679
distinct `(user, rating_key)` pairs in `picks`:

- 32 events matched directly on the movie/episode `ratingKey`
- **46 matched only via the show's `grandparentKey`** — a pick for a series stores the SHOW's key,
  while history reports the EPISODE played
- 1,977 were titles we never recommended to that person (normal — people watch plenty besides their
  row), and 1 was an account that is not a Shortlist user

So the grandparent mapping carries 59% of all matches and is not optional. Parse the show's key out
of `grandparentKey`'s path (`/library/metadata/592373` → `592373`; there is no `grandparentRatingKey`
attribute on history entries) and try the direct key first, the show key second.

## 4. Storage

### Phase 1 — `watch_events` (from the history log)

One row per completed play Plex recorded.

```
watch_events
  id                integer pk
  plex_account_id   integer  indexed     -- joins users.plex_account_id
  rating_key        integer              -- the movie, or the episode
  show_rating_key   integer null         -- episodes: parsed out of grandparentKey's path
  media_type        text                 -- movie | episode
  viewed_at         datetime indexed     -- UTC; Plex sends unix epoch
  source            text                 -- 'history'
  history_key       text unique null     -- Plex's own row id; the dedupe key
  created_at        datetime
```

`history_key` carries the dedupe for free — the log emits near-duplicates (same item, same account,
seconds apart; two identical rows at one second observed). Cursor lives in `settings` as
`sync.history_cursor`.

### Phase 3 — `watch_sessions` (from the websocket)

One row per playback session, opened live and closed when it ends. Separate from `watch_events`
because it is a **span** with progress, not a point, and because conflating "we watched them watch
it" with "Plex says it completed" muddies both.

```
watch_sessions
  id                integer pk
  plex_account_id   integer  indexed
  session_key       text                 -- Plex's; unique only while the session is live
  rating_key        integer
  show_rating_key   integer null
  media_type        text
  started_at        datetime indexed
  last_seen_at      datetime
  ended_at          datetime null        -- null while live
  max_offset_ms     integer              -- furthest they got
  duration_ms       integer null         -- runtime, from metadata (cached per rating_key)
  end_reason        text                 -- 'stopped' | 'timeout' | 'replaced'
```

### Both phases — one new column on `picks`

```
picks.max_percent   integer null         -- furthest they got, 0-100
```

`watched_at` keeps its column but changes meaning: **when they started it**, taken from a session
start where we have one, else from the history-log completion. `finished_at` is unchanged. The
outcome model in §2 derives from the three together, so no outcome column is stored.

## 5. Phases

Each phase is separately shippable and separately revertible.

**Phase 1 — events, in parallel, changing no reported number.**
Migration for `watch_events` + `picks.max_percent` + `run_shared_rows.audience`. History-log feed on
the existing sweep. Backfill bounded to the oldest pick we hold (~2,000 rows covers the lot).
Implement the §3 attribution **alongside** the current rule and log where they disagree. Nothing
user-visible changes.

**Phase 2 — switch over.**
Make §3 the reported number. Delete the pre-run snapshot and `live_pick_ids`. Stretch the library
read to 12–24h — safe only now, because a late catch-up is attributed correctly.

**Phase 3 — the websocket.**
`watch_sessions`, the listener, start-based credit, partial metrics, and the UI in §7.

## 6. The websocket listener (phase 3)

Shape borrowed from Tautulli's `web_socket.py` / `activity_handler.py`, which solves the same
problems and has run against real servers for years:

- Connect `ws://<pms>/:/websockets/notifications`, token in the header, not the URL.
- Reconnect with a bounded retry and a fixed delay; treat a closed socket as a normal event, not an
  error. Every gap is covered by the history log on the next sweep.
- A session STARTS at the first event carrying a `sessionKey` we do not know.
- **Do not trust a `stopped` event to arrive.** Tautulli schedules a force-stop callback rather than
  waiting for one, because sessions vanish. We do the same: a session with no event for N minutes is
  closed with `end_reason='timeout'`. See §8 for what the capture measured.
- Throttle progress writes — Tautulli only persists a playing update every 60s. Same here: the row
  keeps `max_offset_ms` and `last_seen_at`, it does not need every tick.
- **Identity is not in the event.** Resolve `sessionKey` against `/status/sessions`, whose
  `<User id>` is the plex.tv account id. Cache it for the life of the session; a session we cannot
  resolve is dropped rather than guessed at.
- **Runtime is not in the event either.** Read `duration` from `/status/sessions` at session start,
  or from `/library/metadata/{ratingKey}`, and cache per rating key.
- An ignore floor: a session shorter than ~2 minutes with almost no progress is a misfire, not a
  start. Tautulli has the same idea as its configurable ignore interval.

## 7. What it surfaces

Mocked at the canvas linked from the session that produced this spec. Three surfaces:

- **Dashboard** — Delivered / Started / Finished / Dropped, and per row a three-part bar (finished,
  started-and-dropped, never-opened) plus "held" = finished as a share of started. Held is the number
  that separates a good pick from a merely visible one.
- **Per person** — every pick in their row with how far it got, and which of the four outcomes.
- **Picks that lose people** — titles several people start and nobody finishes, with where they
  typically stop, and the distribution of where abandons happen.

## 8. Measured behaviour

A 240-second capture of the live socket on SFLIX on a normal evening: **234 `playing` events across
10 sessions** — roughly one event per second server-wide.

**A `stopped` state does arrive on a clean end.** Two sessions ended during the window; both emitted
`stopped` and both had left `/status/sessions` by the time the capture finished. So the stop signal
is real — but keep the timeout fallback anyway, because a client that crashes or drops off the
network never sends one, which is why Tautulli schedules a force-stop rather than waiting.

**Identity must be resolved on the first PLAYING event, never on a stop.** 8 of 10 sessions resolved
against `/status/sessions` immediately; 2 did not. One of those (session 587) had `stopped` as the
only state we ever saw — we joined mid-session, and by the time its stop arrived the session was
already gone from `/status/sessions`, so there was nothing left to resolve it against. Resolve at
start, cache for the life of the session, and drop a session first seen as `stopped`.

**The socket carries every user.** 8 distinct accounts in four minutes, not just the token owner.

**Cadence: median 10s between events for a session, max 15s.** Which means a DB write per event is
about one write per second, forever, for nothing — `viewOffset` moves 10 seconds every 10 seconds.
Hold session state in memory and persist on a 60s throttle plus on close, exactly as Tautulli does.

**`viewOffset` advances 1:1 with wall clock** (+231,978 ms over 232 s; +230,102 over 230; +235,569
over 236). It is a trustworthy progress measure, not an estimate.

## 9. Rules this change lives under

- **No Plex writes.** Reads only. If that ever stops being true, `.claude/rules/plex-safety.md`
  applies in full and the writer lock is not optional.
- **Architecture Review before any commit here** — this reads watch history and maps it onto user
  identity, which is on the risk list in `.claude/CLAUDE.md`.
- **An Alembic migration per schema change**, which also puts this on the review list.
- Timestamps: our DB is UTC, Plex sends unix epoch. Convert at the boundary, once.
- `historyKey` is the dedupe key. The log repeats itself.
- History pagination needs **both** `X-Plex-Container-Start` and `X-Plex-Container-Size` — Size alone
  is ignored and you get all 101k rows back.
- **Do not push to `dev` while this is in progress.** Commit locally; deploy to SFLIX as
  `shortlist:local` with `--label com.centurylinklabs.watchtower.enable=false`, or watchtower
  replaces the test build with `:dev` inside four hours and the behaviour appears to revert on its
  own. Push only on the owner's say-so.
