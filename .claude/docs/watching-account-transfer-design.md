# Watching-account transfer — replicating a watch history exactly

The plan of record for making "move my watching to a second account" produce an account whose watch
state **matches the original**, rather than one that claims everything is finished.

Companion to [watch-signals-design.md](watch-signals-design.md) (what each Plex read returns) and
[watch-tracking-build.md](watch-tracking-build.md) (how plays are captured). This document is about
the **write** direction, which neither of those covers.

Status: **built, tested, and proven on a live account.** `shortlist/engine/watch_replica.py` (the plan), `PlexClient.read_watch_state`/`apply_watch_op` (the PMS half), `services/watching_account.py` (snapshot, apply, verify, undo), migration 0082, and the `/transfer` + `/undo` endpoints. Everything below is implemented, including the durable job in §3.5. See §9 for what is and is not covered, and §10/§11 for what live running and review actually caught.

Every number and every API claim below was probed against SFLIX
(PMS 1.43.3.10896) on 2026-08-25 — read probes with the admin token, write probes on a purpose-made
Home user (`Tester`, account 841506001) with every write undone afterwards.

---

## 1. What is wrong today

`transfer_watch_history` copies `watched_titles` rows and, when asked, calls
`plex.scrobble_as(row.rating_key, ...)` for each one (`watching_account.py:181`).

For a show, `row.rating_key` is the **show's** key. `/:/scrobble` on a show key marks every episode
watched. That is the whole bug, and it is not an edge case.

Measured on the owner's own account, which is the account this feature exists to move:

|                                                |         |
| ---------------------------------------------- | ------- |
| watched shows                                  | 535     |
| of those, **fully** watched                    | 193     |
| of those, **partially** watched                | **342** |
| partial shows with **zero** completed episodes | 63      |
| watched episodes                               | 9,582   |
| episodes in progress (`viewOffset > 0`)        | 267     |
| watched movies                                 | 1,073   |
| movies started and never finished              | 72      |
| movies with `viewCount > 1`                    | 169     |

So today's transfer would tell Plex that 342 shows are complete when they are not — including 63 the
person has not finished a single episode of — and would silently drop 267 part-watched episodes, 72
part-watched films and 169 rewatch counts. The reported One Piece complaint is the **majority case**,
not a corner.

It then corrupts Shortlist's own copy. The next watch sync reads the target back as `1100/1100` and
overwrites the correct counts the transfer wrote, so the engine also stops offering to continue the
show.

**The source account is never written to.** The copy reads the source and only ever scrobbles with
the _target's_ server token. That is true today and stays true here. It is worth saying in the UI,
because fear of losing an existing history is the reason people decline the feature.

### Why the cache cannot be fixed in place

`watched_titles` is built from `?unwatched=0`, which is completions-only and show-level. It stores
`viewed_leaf_count` / `leaf_count` — **how many** episodes, never **which**. No change to the write
side can recover information the read never had. The source of truth has to change.

---

## 2. What the server actually does

Probed 2026-08-25 against SFLIX 1.43.3.10896. Each of these gets a recorded fixture (rule 11).

### Writes

| Call                                                   | Result                                                                                                                   |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `/:/scrobble?key=<episode>`                            | show goes to **1/35**, not 35/35. Per-episode replication works.                                                         |
| `/:/scrobble` twice on one movie                       | `viewCount` **1 → 2**. Rewatch counts are replicable.                                                                    |
| `/:/progress?key=&identifier=&time=<ms>&state=stopped` | sets `viewOffset` **exactly**, with the target's own server token, leaving `viewCount` unset. Partial replication works. |
| `/:/unscrobble?key=`                                   | clears it. Undo works.                                                                                                   |
| any of the above                                       | `lastViewedAt` is stamped **now**. No API accepts a date.                                                                |

Every cell of the write plan in §3.2 was exercised against `Tester` with `Tester`'s own server token,
minted through the same plex.tv switch-then-exchange path `canary_server_token` uses
(`plextv.py:330`). Specifically: show key → **35/35** (which is the One Piece bug, reproduced
deliberately); episode key → **1/35**; `/:/progress` on a movie key and on an episode key → exact
`viewOffset`; three back-to-back scrobbles in 28 ms → **`viewCount` 3**, so the rewatch loop needs no
pacing; a ratingKey the account cannot see → **HTTP 404**, which `scrobble_as` already treats as skip
rather than raise. Undo was exercised for all of them and `Tester` verified clean afterwards.

**Unscrobbling an episode does not clear its show.** Found while cleaning up after the write probe:
with the one scrobbled episode of `【OSHI NO KO】` unscrobbled, the show still appeared in
`?type=2&unwatched=0` reading **0/35**. The show row keeps its own `viewCount`/`lastViewedAt` until
the show key is unscrobbled too.

Two consequences, both load-bearing:

- **Undo must clear the show key as well as the episodes**, or a rolled-back transfer leaves every
  touched show flagged watched-with-nothing-watched.
- **A show reading `unwatched=0` does not mean any episode was completed.** This is exactly the shape
  of the 63 shows on the owner's account reading `0/leafCount`, Bob's Burgers among them at 0/313.
  Today's transfer scrobbles the show key for those and marks all 313 episodes watched for a show the
  person has finished none of. The write plan must treat `viewedLeafCount == 0` as **write nothing at
  show level**, falling through to the per-episode and in-progress passes.

**Scrobbles write nothing to the play-history log.** Tester's `/status/sessions/history/all` held 0
rows before and 0 rows after 31 scrobbles, and **still 0 when re-read several minutes later** — so
this is not a flush that had yet to happen. This was the single largest risk in the design — that a
transfer would inject thousands of fake plays into `watch_events` and inflate every report — and it
is **not real**. No suppression window, no history deletion, no cursor games are needed.

It is also the assumption most likely to break under a PMS upgrade, and nothing else in the system
would notice if it did. It gets a recorded fixture and an explicit re-check on any Plex version bump:

    PLEX_PREFS=... PMS_URL=... TEST_ACCOUNT_ID=... python scripts/recheck_pms_assumptions.py

That script re-measures all six behaviours below against a throwaway Home account and exits non-zero
if any has changed, so the re-check is a command rather than a paragraph. Plex on the maintainer's
host auto-updates (`linuxserver/plex` with `VERSION=latest`, recreated by watchtower every ~4h), so
the version can move with no deploy at all — it went 1.43.3.10793 → .10896 between two readings of
this document.

(The log does contain human bulk-marks: 392 distinct titles in one minute for one account in 2023.
Those come from Plex's own UI, not from this endpoint. Unrelated to us, but it is why the risk looked
live before it was probed.)

Throughput: **~2 ms per scrobble** on-host. The whole ~11,000-write transfer is a couple of minutes of
PMS time at most. Volume is not the constraint it looked like.

### Reads

| Call                                            | Result                                                                                                    |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `/library/sections/{k}/all?type=4&unwatched=0`  | every watched **episode** in one paged read — 9,582 rows, and **all 9,582 carry `grandparentRatingKey`**. |
| `/library/sections/{k}/all?type=4&viewOffset>0` | in-progress episodes. The `viewOffset>` filter **is** honoured (unlike `lastViewedAt>=`).                 |
| `/library/sections/{k}/all?type=1&viewOffset>0` | in-progress movies.                                                                                       |
| `/status/sessions/history/all?accountID=`       | exact per-account totals (11,587 / 10,902 on two real accounts).                                          |

`grandparentRatingKey` being present matters: the history log has only `grandparentKey` as a path and
has to be parsed (`plex_pms.py:1394`). The section read does not.

### What is still impossible

Backdating. There is no date parameter on `/:/scrobble`, `/:/progress` or `/:/timeline`. The only
mechanism is writing Plex's own SQLite directly, which Shortlist cannot reach (separate container, we
do not mount Plex's database) and which is unsafe against a running server. **Out of scope, and the
UI must say so rather than imply otherwise.**

---

## 3. Design

### 3.1 Two reads, two consumers

**Read A — the source's current state.** Four paged reads with the source's token (the admin token,
since the source is always the owner):

- movies, `type=1&unwatched=0` → `ratingKey`, `viewCount`, `lastViewedAt`
- movies, `type=1&viewOffset>0` → `ratingKey`, `viewOffset`, `duration`
- episodes, `type=4&unwatched=0` → `ratingKey`, `grandparentRatingKey`, `viewCount`, `lastViewedAt`
- episodes, `type=4&viewOffset>0` → `ratingKey`, `viewOffset`, `duration`

This is what gets written to Plex. It is authoritative for _what_ is watched, and it is the only
source that knows about partials.

**Read B — the source's play log**, `accountID=<source>`, no `since`, paged. This is what gets copied
into Shortlist as the target's `watch_events`. It is the only source of **true dates** and of
per-episode play history, and since scrobbles write no history rows, it is the only dated history the
new account will ever have.

### 3.2 What gets written to Plex

**Never a show key or a season key. Only leaves — episodes and movies.**

| Source state                      | Write                                                   |
| --------------------------------- | ------------------------------------------------------- |
| episode watched                   | one `/:/scrobble` per its own `viewCount`               |
| episode in progress               | `/:/progress` with its `viewOffset`                     |
| movie watched                     | one `/:/scrobble` per its own `viewCount`               |
| movie in progress, never finished | `/:/progress` with its `viewOffset`                     |
| movie watched **and** in progress | scrobble to `viewCount`, **then** `/:/progress`         |
| show, at any level                | nothing — the show row is a consequence of its episodes |

The last two rows are measured, not assumed. `10 Cloverfield Lane` (`viewCount 1`, `viewOffset
490509`) replicated to `{'viewCount': 1, 'viewOffset': 490509}` exactly: neither write clobbers the
other, in that order.

#### Why no show key — the shortcut is silently lossy

Scrobbling a show key does mark every episode: show 592000 (`12 Monkeys`, 47 episodes) went to
**47/47** for Tester within a second. But it leaves the **show's own `viewCount` unset**, and
`?type=2&unwatched=0` filters on exactly that. So the show was 47/47 and **absent from the section
read** — the read Shortlist's own watch cache is built from (`plex_pms.watched_titles`). A transfer
using the shortcut would produce shows Plex thinks are complete and Shortlist cannot see.

Scrobbling all 47 episodes instead reproduces the source: `viewedLeafCount` 47/47, show `viewCount`
set, present in the section read. Cost: 47 writes in **0.46 s**. Across the owner's whole account the
shortcut would have saved 4,167 writes — about eight seconds — in exchange for a class of silent
mismatch. Not worth it.

Season-key collapsing is out for a separate reason: modelled against the real account it came out
_worse_ (820 writes vs 625 on a 40-show sample), because partial shows are scattered viewing — 18 of
802 Simpsons episodes — so complete seasons are rare.

**Per-episode `viewCount` matters too.** The source show row reads `viewCount 56` against
`viewedLeafCount 47` — the owner rewatched nine episodes. A single scrobble per episode gives 47.
Replicating each episode's own `viewCount` reproduces 56. The episode read already returns it.

Estimated total for the owner's account: **about 11,000 writes** — 9,582 watched episodes plus their
rewatches, 267 episode offsets, 1,073 movies plus ~200 rewatches, 72 movie offsets. At the measured
~10 ms per write that is roughly two minutes. Today's code writes 1,608 and gets most of them wrong.

#### What this says about the existing product

A show marked watched at show level is invisible to `watched_titles`. That is not only a transfer
concern: **any account the current transfer has already scrobbled has shows Plex reports as complete
and Shortlist cannot see**, so their picks still offer titles they have "finished". No transfer has
ever run on the maintainer's server, but `:dev` and release users have had this feature since it
shipped. Worth a separate look; out of scope here.

### 3.2a Mirror, not merge — and the snapshot that makes it safe

The transfer **mirrors**: the target ends up matching the source, which means clearing state the
source does not have, not only adding state it does.

Add-only cannot meet the goal. An account carrying prior state — most importantly one the CURRENT
transfer has already scrobbled — keeps everything wrongly marked. One Piece is the worked example:
the old transfer marks all 1,100 episodes, the new one scrobbles the 400 that were really watched,
those 400 are already marked, and **the other 700 stay marked for ever**. Re-running never repairs
it, and the result is not a replica.

So the write plan gains a fourth category: anything watched or in-progress on the TARGET that is not
watched or in-progress on the SOURCE is `/:/unscrobble`d, and any offset the source does not have is
cleared. Owner decision, 2026-08-25.

**This is the only destructive path in the feature, so it takes a snapshot first** — the same rule 2
that governs restriction writes, for the same reason. Before the first write, the target's complete
state (every watched movie and episode with its `viewCount`, every `viewOffset`) is persisted. Undo
restores from it exactly.

That makes this version strictly safer than the one shipping today, which writes thousands of
changes to someone's account with no snapshot and no undo at all.

Two guards on top:

- **Dry run lists what would be un-marked, by title**, not just a count. "This will remove 412 watches
  from that account" is the sentence someone needs before they agree, and a bare number is not it.
- The confirmation names the account and the number of removals. It is the one screen in this feature
  where the copy has to say plainly that something gets deleted.

### 3.3 Order

Writes are sorted **ascending by the source's original timestamp** — oldest play first.

Plex stamps everything `now`, so absolute dates are lost either way. But Continue Watching and
"recently watched" sort by `lastViewedAt`, so writing in the source's chronological order makes the
target's **relative** order match the source's exactly. It costs a sort and it fixes the shelf the
person actually looks at.

### 3.4 What gets written to Shortlist

- `watched_titles` for the target, as today, with correct per-show counts — plus `source_viewed_at`
  from the source's `lastViewedAt`, unchanged.
- `watch_events` for the target: the source's play-log rows, remapped to the target's
  `plex_account_id`, with `history_key = f"transfer:{target_account}:{original_key}"` (the column is
  unique and nullable; a namespaced key keeps the copy idempotent on re-run) and `source='transfer'`.

**Copied events are excluded from pick attribution.** They are real watches for recommendation
purposes and must feed recency and seeds, but they are not this person pressing play on a Shortlist
row, and back-dated credits are a bug shape this codebase has already shipped twice. `event_credits()`
and `shared_credits()` filter `source != 'transfer'`.

### 3.5 It becomes a job

The transfer moves onto the durable queue (`jobs.py` catalog) instead of running inside the HTTP
request (`api/watching_account.py:150`).

Not primarily for time — at 2 ms a write it finishes in under a minute, and the earlier claim that it
could not finish in a request was wrong. It becomes a job because that is where progress reporting,
resumability (rule 6), the audit record and the undo payload all belong, and because the read side is
the slow half.

Resumability: the job records the keys it has written. A replay skips them. Idempotent by
construction, except `viewCount` — repeated scrobbles increment, so the rewatch loop records its
per-title progress rather than re-deriving it.

### 3.6 Verify, and undo

**Verify** runs at the end: re-read the target's state with the same four reads and diff against the
source. Report matched / missing / unreachable, with reasons ("in a library this account is not
shared"). Today the transfer reports counts it never checked.

**Verify compares leaves, never show rows.** A show row is derived state — §3.2 showed it can read
47/47 and still be missing from the show-level query. The diff is over episodes and movies, with show
totals aggregated from the episode read by `grandparentRatingKey` (present on 9,850 of 9,850 episodes
across both show libraries, so the aggregation is total). This is also what makes the verify pass
independent of the write path rather than agreeing with it by construction.

A miniature end-to-end of exactly this — 22 writes replicating one complete show, two partial shows,
a zero-completed show, seven movies including a rewatched one and one both watched and in progress,
and three in-progress episodes — matched the source on every item once the show key was dropped.

**Undo** is a second job kind, and with mirroring it restores from the §3.2a snapshot rather than
walking back a list of writes. That is the difference between "un-do what I added" and "put the
account back how it was", and only the second is correct once the transfer can also remove things —
a rollback that re-ticked what we un-ticked but forgot the `viewCount` and offsets behind them would
leave a third state that never existed.

Undo must also unscrobble the **show** key for every show it touched. Unscrobbling an episode leaves
its show flagged (§2), so an episodes-only rollback leaves each show reading watched-with-nothing-
watched — the same 0/N shape as the 63 shows on the owner's account.

### 3.7 Dry run and audit

`dry_run` logs the full would-be diff per title (rule 8). The audit event records intent and outcome
including the per-category write counts (rule 10), so "what did the transfer do to this account" is
answerable afterwards, which it currently is not.

---

## 4. Deliberately not doing

- **Backdating Plex.** Impossible without writing Plex's database. Section 2.
- **Season-key collapsing.** Measured, worse. Section 3.2.
- **Keeping the two accounts in step afterwards** — re-copying plays the source makes later, for
  people who keep forgetting to switch. A real idea, out of scope here, worth its own change once
  this one is proven. (Not to be confused with "mirror" in §3.2a, which is about one run making the
  target match the source, not about an ongoing sync.)
- **Transferring ratings.** `userRating` is per-token and readable, but nobody asked, and it is a
  separate promise.
- **Any write to the source account.** Ever.

---

## 5. Testing

- Unit: the write-plan builder is pure — source state **and target state** in, ordered write list
  out. Every cell of the matrix in 3.2 gets a case, including the three that bit us (show with zero
  completed episodes; movie both watched and in progress; a target already scrobbled by the OLD
  transfer, whose 700 spurious episodes must appear as removals).
- Property: plan → apply → re-read → plan again must be a fixed point, and the second plan empty.
- Undo: snapshot → mirror → undo → re-read must equal the snapshot exactly, including `viewCount`
  and offsets, not merely watched/unwatched.
- A target with watches of its OWN that the source lacks: they appear as removals, they are in the
  dry-run listing by title, and undo brings every one of them back.
- Fixtures (rule 11): recorded responses for each probed shape in section 2, including the
  **empty** history log after a scrobble, which is the assumption most likely to change under a PMS
  upgrade and the one nothing would otherwise catch.
- `fake_plex` gains per-episode watch state and `viewOffset`, so e2e exercises the real shapes. Per
  the testing rules, the fake must be no easier than the real server.
- e2e: transfer against the fake, then assert a partially-watched show is still partial afterwards.

---

## 6. Rollout

1. Land the read + plan + write path behind the existing `scrobble` flag, dry-run first.
2. Run it against `Tester` on the live server, verify pass green, then undo and confirm Tester is
   clean.
3. Only then offer it in the wizard.

---

## 7. Open questions

- Managed users are restricted (`Tester` reads `restricted=1`). Library visibility is per-account, so
  a title in a library the target cannot see must resolve to `unreachable` in the verify pass, not to
  a failure. Confirmed reachable for all three sections on this server; not confirmed for a target
  with narrower sharing — though an unreachable key returns a clean 404 (§2), which is exactly what
  `scrobble_as` already treats as "skip, don't raise".
- Whether the 90-day `watch_events` retention should be relaxed for `source='transfer'` rows, which
  describe watches far older than the window.
- Accounts already scrobbled by the PRE-1.x transfer carry shows Plex reports complete and
  `watched_titles` cannot see (§3.2). Mirroring (§3.2a) repairs them on the next run, so no separate
  migration is needed. Nothing PROMPTS those users, but the preview now names the situation when it
  sees it: a removal count over 20 is almost never someone's own viewing, so the confirmation says
  the count is old damage rather than leaving a wall of removals to read as destruction. Actively
  detecting affected accounts and offering the repair unprompted is still undecided — it would mean
  reading every watching account's show rows looking for the 0/N shape, on a schedule.

## 8. What was verified, and what was not

Everything in §2 and §3.2 was exercised against `Tester` (841506001) on SFLIX with `Tester`'s own
server token, on 2026-08-25, and undone afterwards with the account re-read to confirm it was clean.
The read-shape claims in §1 and §3.1 come from the live server with the admin token.

Not verified, and knowingly so:

- **Scale.** The largest run was 47 writes. The real transfer is ~11,000. Expected to be slower, not
  different, but it has not been observed.
- **A target with narrower library sharing than the source.** All three sections are shared with
  `Tester`. An unreachable key returns a clean 404, so the failure mode is understood; the end-to-end
  is not.
- **The implementation.** None of it exists. The API layer is proven; the plan builder, the ordering,
  the verify diff and the job wiring are not.

---

## 9. Built vs. not built

Implemented and covered by tests: the write plan and its ordering (`watch_replica.py`), the four
reads and the three write verbs, mirroring with removals, the snapshot and undo, the verify pass, the
copied play events and their date fallback, `source_viewed_at` stamping, the two durable job kinds,
the pending-undo listing, `fake_plex`'s per-episode state, and the UI's preview gate.

**Source picker — built.** The page offers "Copy the history from" whenever there is more than one
candidate, defaulting to the owner and sending no `from_user_id` at all in that case (the server owns
who the owner is; the UI should not be a second place that knows). The TARGET stays restricted to
Home users at both layers. This exists because the owner of the reference server watches on a SHARED
account, so copying from the admin account would have replicated an empty history over a real one.

**Not built — keeping the two accounts in step afterwards.** Out of scope, see §4.

## 10. The live run — what it proved, and the two bugs it caught

Run against SFLIX on 2026-08-25, owner (account 5245144, 10,948 watched leaves) → `Tester`
(841506001), through the real HTTP API with the owner's API token. Verified by reading Plex
DIRECTLY afterwards, never by trusting the report the feature produced.

Final result, second run:

| | |
| --- | --- |
| planned / applied | 10,713 / 10,713 |
| unreachable | 0 |
| `verify_mismatched` | 0 |
| dates carried (`events_copied`) | 10,948 |
| owner leaves vs Tester leaves | 10,948 / 10,948 |
| on owner only / on Tester only | 0 / 0 |
| differing view counts / positions | 0 / 0 |
| owner's own account changed | no |
| undo restored Tester exactly | yes |

The FIRST run is the more useful record, because it failed in two ways nothing else had caught.

**1. `/:/progress?time=0` does not clear a view offset.** The undo planned `CLEAR_OFFSET` for 349
items and reported 11,004 applied — and left **293 of them still part-watched**. Probed directly
afterwards: an offset of 1,139,347 was still 1,139,347 after `time=0`, and only `/:/unscrobble`
cleared it. Worse, the FAKE had modelled `time=0` as working, so the whole suite agreed with the
wrong answer — a fake easier than the server, which is the one thing a fake may never be.

The fix makes clearing an offset a **full reset**: `/:/unscrobble` zeroes the view count with the
offset, so any count still wanted is rebuilt from zero afterwards. Pinned by
`TestClearingAnOffsetIsAFullReset`, and the fake now ignores `time=0` exactly as the server does.

**2. Copying the play log carried no dates at all.** `events_copied` was **0**. The owner's account
has 10,948 watched leaves and effectively no play-log rows — the log does not reach back far enough,
and a bulk "mark as watched" writes none at all. So `source_viewed_at` stayed NULL and every
replicated title read as watched today, which is precisely the failure that column exists to prevent.

The fix dates anything the log cannot from the leaf read's own `lastViewedAt` — a weaker fact (the
latest view, not each play) recorded as one row per title rather than one per play. On the second run
that carried all 10,948. Pinned by `TestDatesWhenThereIsNoPlayLog`.

Both were invisible to 3,288 passing tests. They needed a real server.

---

## 11. What the review found that the live run did not

An Architecture Review pass over the finished diff found **three HIGH** issues, none of which the
live run had surfaced, plus several MED. Recorded because each is a shape worth recognising again.

**HIGH — the snapshot was never durable.** `_snapshot` only FLUSHED; the caller's transaction commits
after all ~11,000 Plex writes. Anything raising in between — the verify read hitting a 500, the
play-log copy, a stray `OperationalError` — rolled the snapshot back while the writes stayed on Plex.
The retry then re-read a half-mirrored target, found the plan already converged, and took **no
snapshot at all**: the un-marked watches were gone with no record anywhere. The comment above it
claimed the opposite of what the code did.

Fixed: `take_snapshot` commits in its own transaction before the first write, and reuses an existing
un-restored snapshot for the same job so a retry keeps pointing at the true "before".

**HIGH — the undo silently dropped view offsets.** `SET_OFFSET` was decided against the offset as
READ, before the plan ran — but the reset above it is `/:/unscrobble`, which zeroes the offset too. An
item being reset whose offsets already agreed got no reposition, and its position was lost; the undo
then stamped `restored_at`, so a second press refused. Every existing case in
`TestClearingAnOffsetIsAFullReset` had a source offset of 0, which is exactly why none caught it.

Fixed: the comparison uses the offset the target will have AFTER any reset.

**HIGH — copied events were never excluded from attribution.** §3.4 stated that `event_credits()` and
`shared_credits()` filter `source != 'transfer'`. Nothing did. Every copied play was creditable to
whatever row happened to contain that title at the copied timestamp, inflating `row_effectiveness` —
and through `_CreditInputs.observed`, those credits could never be withdrawn. The covering test
asserted only that the rows carried the marker, never that anything acted on it.

Fixed in `_scan_plays`, and the test now asserts the outcome.

**MED, all fixed:** a real mirror could run with no preview at all (the block keyed on
`removals > 0`, so no preview read as "nothing to remove"), and an acknowledgement survived a change
of target account; `_copy_play_events` built one unbounded `IN (…)` that would exceed SQLite's 32,766
variable limit on a large account, **after** every Plex write had landed; the service-level fake
modelled un-scrobble as clearing one field, which is what hid the offset bug; a show whose episodes
were all removed kept its own `viewCount` and went invisible to `watched_titles`; and the snapshot id
was reachable only from the in-flight response, so a timed-out request left a completed destructive
run with no way back.

The lesson worth keeping: the live run proved the feature WORKS, and the review found the ways it
fails. Neither substitutes for the other.
