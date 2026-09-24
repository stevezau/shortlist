# Full-repo review backlog

Findings from the nine-reviewer pre-`beta.8` sweep (July 2026). **Everything is closed** — sections
1–8 from the sweep itself, plus the one item the sweep missed and a later pass found. See the `dev`
history for 2026-07-31.

Kept as the record of what was fixed, so a future reviewer who rediscovers one of these checks the
history before "fixing" it again. Everything from that sweep is closed; the OPEN section immediately
below is later work.

---

## OPEN — left over from issue #133, "Because you watched X" stuck (2026-09-25)

The bug itself is fixed in be9dcd95: `_seed_moved` compared pick #1 as it stood while the title skips
unseeded picks, so a row led by a discover/web-search pick never saw its seed move. These three were
found on the way and are not fixed.

- **LOW — the run trace says "refreshed" for a row rebuilt because its seed moved.** `decision` is
  settled at `shortlist/engine/rows.py:2754-2762`, before the branch at `:2803` asks `_seed_moved`, so a
  forced rebuild is recorded as `refreshed`. It hid the #133 mirror cells (a nightly full rebuild) from
  anyone reading the trace, and it makes `decision` useless as a test assertion — the #133 tests assert
  on which picks survived instead. Fix: a `seed_moved` decision, set where the bootstrap branch is taken
  for that reason.
- **DECISION (owner) — a TV row can carry a film's name.** When a library has no seeded pick,
  `seed_source` (`shortlist/engine/delivery.py:380-397`) borrows the other library's title (#84, pinned
  by `tests/unit/test_delivery.py:145`). The #133 reporter's TV row read "Because you watched Passenger"
  (a film) over 18 shows with no seed at all: their TV seed's 40 look-alikes were not in a 309-show
  library. Option: when the row has its own `fallback_name`, prefer it to a borrowed seed; default
  unchanged when it is blank.
- **LOW, reasoned not reproduced — above one seed per library, a title can name a dead seed for a
  night.** `_seed_moved` now checks the prior NAMED pick; the refresh then re-ranks survivors against
  the pool (`rows.py:2821`) and the title renders from whichever seeded survivor ranks best — which can
  be one whose seed has since left the seed set. The next night's check sees that name and rebuilds.
  One seed per library (the recommended Movies+TV budget of 2) cannot reach it.

Not a bug, so it isn't re-found: on SFLIX, 28 of 91 `{top_seed}` titles named an older watch than the
person's newest (2026-09-24). The row's budget is 3, and above one seed per library the title names the
best pick's seed, not the newest watch — the trade-off `docs/guides/rows.md` already describes.

## OPEN — v1.9.2 release review, one MED and three LOW (2026-09-24)

The release-PR Architecture Review over `v1.9.1..dev` (PR #131) found no HIGH, so 1.9.2 shipped with
these open. All four are in the shared-row duplicate cleanup (`_remove_shared_row_duplicates`, 92d690c7)
and fire only on a server that still carries a duplicate from a rename before 1.9.1.

- **MED — the run page calls a removed duplicate a deleted row.** The removed titles go into the
  library's `CollectionDiff.deleted` (`shortlist/engine/delivery.py:1805`, `:916`), which
  `web/src/components/runs/user-panel.tsx:232-236` renders in red as "Row deleted (this person no longer
  gets this row)", beside that same row's live picks. Before 92d690c7 nothing in `_deliver_one` filled a
  per-library `deleted`, so this text is new to delivery. Fix: a separate field, or wording for this case
  ("Removed a duplicate copy of this row").
- **LOW — a failed duplicate delete is still reported as deleted.** `delivery.py:1548` appends the title
  before `delete_owned_collection` runs, so a raise still lands it in `diff.deleted` and the events row —
  with the MED, "Row deleted" every night while the duplicate stays on Plex. Contradicts the docstring and
  `models.py:1323` ("rows destroyed this run"); `test_a_failed_delete_never_costs_the_audience_their_row`
  pins the current behaviour. Fix: append only after a successful delete (or in a dry run), keep the
  failure in the warning, update that test.
- **LOW — a duplicate delete goes unaudited if the row's own write then fails.** The cleanup at
  `delivery.py:1741` runs before the row's membership write; if that raises, `_deliver_one` never returns
  its diff (shared rows have no retry wrapper), so no events row records a delete that happened. Only the
  WARNING line, now in the run log, survives (rule 10). Fix: run the cleanup after the row's own writes, or
  return the removed titles on failure too.
- **LOW — a comment gets rule 9 wrong.** `delivery.py:1553-1557` names `_rebuild_under_twin_name` (the
  function is `_rebuild_under_name`), claims plexapi error text carries the token in the URL (on 4.18.2 it
  does not — `X-Plex-Token` is added only with `includeToken`/show_secrets, and the text is built from
  `response.url`), and calls the exception logged at `delivery.py:260` "one of ours" when
  `_create_labelled_collection` also raises plexapi's own. No leak today; comment only.

---

## OPEN — #115 leaves an allow-list account's own row VISIBLE BUT EMPTY (measured 2026-09-18)

`privacy.admit_own_rows` was shipped to make an allow-list account "see its own rows, filled with titles
it can watch". Measured against a real managed account, it does not do the second half.

**First two attempts were void — recorded so nobody repeats them.** They planted `label=Overlay` as the
allow list. `Overlay` is a Kometa label on **9,980 of 9,991** movies on that server, so the allow list
admitted virtually the whole library and proved nothing. Always confirm the allow VALUE actually
restricts before reading anything into the result.

**The real measurement.** `Tester` (the one managed Home account, no parental profile) was given a real
row for this, then `label=recommended` was planted on its `filterMovies` alone — 11 of 9,991 movies, none
of them in the row — with 45s to propagate and the value restored byte-identical afterwards:

| state | own row collections visible | items inside the movie row |
| --- | --- | --- |
| baseline | both | 30 / 30 |
| allow list planted | both | **0 / 30** |
| after `plan_share_filter` admitted `Shortlist_tester` | both | **0 / 30** |

Two conclusions, both new:

1. **The allow list works on ITEMS and never hid the row object at all.** The collection stayed visible in
   all three states, so the premise "an allow-list account cannot see its own row" did not reproduce on
   PMS 1.43.3.10793 — for a managed account, which is the type the fixture recorded.
2. **Admitting the row's own label does not bring its contents back.** A share filter matches ITEMS by
   label, and the row's member titles carry no `shortlist_*` label — only the collection does. So adding
   `Shortlist_tester` as an allow value can admit the collection (already visible) and none of its 30
   members. The account gets an empty row.

The fixture's conclusion 3 is literally consistent with this — "inside it exactly the items the allow list
admits" is 0 when none of the row's titles carry the allowed label. What overstates is
`.claude/rules/plex-safety.md`'s "filled with titles it can watch", now corrected there.

`admit_own_rows` is still harmless and still additive, so there is nothing to revert. What is open is
whether the feature is worth anything: to fill the row, the row's TITLES would have to carry the label,
which means labelling other people's media — a much larger decision than #115 made.

Residue note: `CanaryAllow_DELETE_ME` now appears in the movie library's label list with 0 items, from the
first void attempt. Inert, and that list already carries several empty names (`Kometa`, `Based`, `Decade`,
`Genre`, `RequestNeeded` are all 0).

---

## CLOSED — v1.9.1 release review, two LOW (2026-09-16, both fixed 2026-09-18)

The release-PR Architecture Review over `v1.9.0..dev` (PR #129) found no HIGH or MED. A third LOW, the
CHANGELOG saying a seasonal row returns "without being rebuilt", was fixed before the tag. The two below
are now fixed too: the catalogue text scopes "without being built again" to days off, and the seedless
season reason is the always-true "Right for the season", pinned by
`test_picker.py::TestReasonFor::test_a_season_pick_does_not_claim_a_genre_match`.

- **The Jobs catalogue overstates seasonal rows.** `rows.visibility`'s description
  (`shortlist/server/services/jobs.py:326-328`) says a hidden row "comes straight back without being built
  again". True for a day schedule, not for a season: the recipe carries `season=<slug>@<anchor date>` and
  `_reusable_prior` drops out-of-season titles, so each new season rebuilds the row the night before it
  opens (`docs/guides/rows.md:148-150` already says so). Fix: scope "without being built again" to days off.
- **Every seedless seasonal pick explains itself as "in genres you watch".** `reason_for`
  (`shortlist/engine/picker.py:23`) uses that line for any seedless `season` candidate, but
  `candidates.py` admits season titles with no genre overlap (fit is only a 0.5–1.0 weight), and a failed
  genre lookup leaves the claim empty for every title. Fix: an always-true line ("Right for the season"),
  or the genre wording only above the fit floor.

---

## CLOSED — LOW: unmarked duplicates of a shared row (found 2026-09-15, fixed 2026-09-18 on the 2nd attempt)

`_remove_shared_row_duplicates`, called from `_deliver_one` once the row has been resolved. It deletes
collections under this row's SHARED label in this library that are not the resolved row, are not marked
with `row_marker(0)`, are not name-freeing helpers, and are the right type for the library. It never
raises: a failed delete of a duplicate must not cost the audience their row.

**The first attempt put this in `sweep_broken_rows` and Architecture Review blocked it**, reproducing the
failure twice against the real sweep. Keep both reasons — they are why the code is where it is:

- In the sweep the only evidence is a title suffix. A leftover name-freeing helper's title is
  `FREED_NAME_PREFIX + hex + row_marker(0)` and it carries the shared label, so it counted as the
  "marked sibling" that authorised the delete — and the live unmarked row was deleted with it, with the
  run reporting success. A marked copy of the wrong subtype, itself being swept as `unhidable`, did the
  same on its way out.
- The sweep is also the wrong PLACE even with guards. Its purpose is rows Plex CANNOT hide, and it runs
  before anything that can fail because leaks cannot wait. This duplicate is not a leak — both copies
  carry the same shared label, so every exclude hides them identically. And an unmarked target makes
  `delete_owned_collection` fall back to a label re-read whose empty answer raises, which `_sweep_phase`
  (`pipeline.py:443`) turns into a whole-run abort. A cosmetic duplicate must not be able to do that.

By `_deliver_one`, `_find_this_rows_collection` has already answered which collection IS the row from an
exact title match or the ledger's ratingKey, so `keep` is an identity rather than a guess.

**Which guard actually protects the live row:** the `endswith(row_marker(0))` one. Every resolution path
requires the marker, so a resolved shared row is always marked and can never be selected for deletion.
The ratingKey comparison is redundancy — verified by mutation: removing it changes no test, while removing
the marker check fails `test_another_MARKED_copy_is_left_alone`. Do not drop the marker check on the
grounds that the ratingKey check looks sufficient.

**What it does NOT cover** (second review, 2026-09-18 — read this before trusting "CLOSED"). The cleanup
is opportunistic: it runs only on a run that actually delivers this row to THIS library, so a duplicate
survives a run where the row got no picks for the library, was scoped out, was dormant or out of season,
or had no audience. It is also skipped when the row cannot be RESOLVED, and a duplicate guarantees
`len(owned) != 1` so the `sole_row` fallback cannot fire either — meaning a shared row whose title has
since moved on (renamed library, new season) resolves to None and a third collection is built. Neither is
a regression; both are the pre-existing shape. Fixing the second means either passing the shared row's
ledger keys through `_shared_row`'s `deliver_rows` call, or relaxing the `sole_row` fallback for a shared
label to "exactly one MARKED collection" — true for shared rows by construction, not for per-person ones.

The same review raised a HIGH that is fixed: the delete reached no audit trail. It now returns the removed
titles and `_deliver_one` puts them in `CollectionDiff.deleted`, so it flows through `combined.deleted`
into the per-library breakdown and the run page. A delete on someone's server must be answerable from the
UI, not from a container log (rule 10).

Nine tests in `test_delivery.py::TestSharedRowDuplicates`, five of which assert nothing is deleted:
helper, wrong-typed, unresolved row, another marked copy, and a per-person row's unmarked sibling. Plus
the audit-trail assertion, and a failed delete that must not cost the audience their row — that last one
guards the "never raises" promise, which is the entire reason this is not in the sweep.

## CLOSED — v1.9.0 release review, three LOW (2026-09-14)

The release-PR Architecture Review over `v1.8.0..dev` found no HIGH or MED. All three LOWs fixed the
same day:

1. **Stale comment** in `api/collections.py` about the visibility handler keeping state — reworded.
2. **`user.restore` omitted `skip_unmatched`**, so un-pausing someone on a row's day off could put an
   unidentifiable `{top_seed}` row back on their Home. It now asks `pipeline.any_row_hidden_today`,
   the same answer `_promote_phase` uses. Pinned by `TestRestoreAfterUnpause::test_an_unidentifiable_row_*`.
3. **0088's downgrade left `collections.shown_state`** behind after 0089's downgrade re-created it.
   It drops it again; recorded as a downgrade-only `amended:` line in `frozen_migrations.txt`.

---

## CLOSED — a person's first row was in the Collections tab until the merge (2026-09-13)

Found auditing #119. A person with no row yet has no `label!=shortlist_<slug>` in anyone's share
filter until a merge writes one, and the merge ran after EVERY person's delivery — about an hour on
SFLIX, longer on a refresh night now that rows update in place.

**Measured, read-only, as a shared account** (`tests/fixtures/pms_collections_tab_filter_visibility.json`):
collection mode "hide" does not cover the Collections tab or collection search — the public shared rows,
mode 0 and excluded by nobody, were listed 2 of 2 — while every row the account's filter excluded was
absent, 0 of 180. So the gap was real: title and contents, for every shared account, for the rest of
delivery.

**Fixed:** when a person whose slug was not in the run-start `stored_labels` gets a stored label,
`delivery.deliver_rows` calls `_exclude_first_rows` (via `rows._deliver_row`) as soon as that library's write
returns — created, labelled, browse-hidden and postered — inside the same hold of the write lock, so no other
collection is written in between, at any concurrency, not even the row's next library — merging excludes
into every other account
(additive only: no enumeration, no departure evidence, so nothing is removed but a person's own label from
their own filter). A delivery that raises in a later library has already triggered it. The first plex.tv failure that is not a per-account 422 stops early merges for
the rest of the run, since each failing write backs off for a minute or more while every delivery waits on
the lock. The end-of-run `_privacy_sync_phase` still runs, reads every filter fresh — which catches an early
write plex.tv did not keep, and confirms or withdraws any #116 "restriction working again" notice the early
merge recorded — and still gates promotion. Cost: one merge per new person; on a first rollout, where
everyone is new, about (people × accounts) plex.tv writes instead of one round.

The early hide from browse at creation (`PlexClient.hide_from_browse`) stays, for library browse.

**Not covered, deliberately (architecture review 2026-09-13):** "already on the server" is taken as
"already excluded", which is not proof. Three cases fall outside the fix; the first two are older than #119:
1. An account newly shared with the server sees every row until the next merge — the end of the next run
   or the daily privacy sync (05:15). A merge before delivery would narrow that to the start of the next
   run, at one extra plex.tv roster read per run; not done, since the daily sync is the gap that matters.
2. A run killed (a `dev` redeploy recreates the container) between someone's first row being created and
   their `_exclude_first_rows` leaves that row unexcluded until the next merge, because the next run treats
   the person as existing. The window is that person's own delivery: minutes for a multi-library person.
3. The early writes are audited only when the run finishes: `report.filter_writes` becomes `run.privacy_sync`
   events in `_persist_report`. A run that crashes or is redeployed after an early merge leaves filter writes
   with no events row, and the next run finds those filters already right and audits nothing. The end-of-run
   writes always had this gap, for the short phases after the merge; it now spans delivery. Accepted: every
   early write only narrows what an account can see. The fix, if wanted, is a live `ctx.on_filter_write` hook
   like `ctx.on_user_done`.

---

## PARTLY CLOSED — every pick vanishing before a create fails the person (2026-09-13, 2026-09-18)

**The race is unchanged and still self-heals; what changed is that it now says so.** 2026-09-18:
`_create_labelled_collection` raises a message naming the person, the row, the library and the pick
count when `picks and not items and vanished`, instead of letting plexapi answer
`BadRequest('Must include items to add when creating new collection')` — which named none of them and
cost a full investigation to place. Behaviour is deliberately identical: that person's row is not built
tonight and the next run rebuilds it.

The gate includes `vanished` on purpose. An earlier attempt at this fired on an empty `items` alone and
broke 25 tests: "no items and nothing vanished" is a different situation that must keep its behaviour.

Still NOT done, and deliberately: making the delivery actually succeed. That needs the picks resolved
BEFORE the repair's delete, per the reverted attempt below, and the race has never been observed —
see the evidence in the original note.

Found auditing #119, LOW, pre-existing. **Never observed on SFLIX:** 0 in 7 days of logs, and 0 vanished
picks in run history back to 2026-07-24 (checked 2026-09-13). Leave it unless it is ever seen. If every pick for a library is deleted from Plex in the seconds
between curation and `_create_labelled_collection`'s `fetch_items`, `create_collection(section, title,
[])` reaches plexapi's `Collection._create`, which raises `BadRequest('Must include items…')` before
any request. It is not transient, so the person's delivery fails that night and heals the next.

A fix was tried and reverted in the same session: returning "nothing created" made the
refuses-every-add repair delete the broken row, create nothing, and report success with a ledger entry
still naming the deleted ratingKey, and made the run page show a row that was never created. A correct
fix has to resolve the picks BEFORE the repair's delete, and keep the breakdown out of the run record.

---

## CLOSED — issue #108 watch-status follow-ups (2026-09-02; the REPORTER closed it 2026-09-05)

**This section was stale and cost a wasted investigation on 2026-09-18. Read this first.** The notes
below were written 2026-09-02 and say three of nine watch-status paths fail. On **2026-09-05** the
reporter retested against `dev` (fd6259d, PMS 1.43.3.10896) and said of the one that mattered — marking a
SEASON watched — "I cannot say what has changed but I can no longer reproduce the error. I've tried with
3 new shows and it's now working just fine", then confirmed every other point. The issue was closed that
day. Nothing here was ever left to build.

Manual marks DO sync, and always did on these paths: `unwatched=0` for movies is Plex's own watched flag
and includes a mark-as-watched, and shows are read with `viewedLeafCount!=0` precisely BECAUSE marking a
series or a season does not set the show's own watch-state row while the episode counts stay correct
(`plex_pms.watched_titles`).

**Proved end to end 2026-09-18** on the managed test account, which has NO watch history at all — the
exact case the stale note called broken. `The 'Burbs`, 8 episodes, `viewedLeafCount` 0: scrobbled ONE
season, the show went to 8/8 and `watched_titles` returned it as `(8, 8)`; unscrobbled, back to 0 and
absent again. So a marked season counts with no prior history, and the 52 shows the abandoned rollup
flagged cannot have been marked at all — every level read zero watched episodes, which marking would have
changed. Do not reopen this without a fresh reproduction from a real server.

All three resolved: 2 and 3 were closed on the dates noted below, and 1 was investigated on
2026-09-18 and ruled out — the sweep it proposed would have marked 52 unwatched shows as watched.
See the measurement under item 1. Nothing here is actionable without a fresh report from a server
that can produce a season with `viewedLeafCount > 0` under a show reading 0.

Six commits landed for #108 (`dd2614a`, `a829724`, `1c61a9c`, `ac0a165`, `545a340`, `83cf07a`), and
the reporter then tested all nine watch-status paths against `2bf1d90`. **Six pass**: mark a show
watched, unmark it, mark a show that already had some episodes, mark an episode, unmark an episode,
unmark a season. Three do not. None is a regression from those commits — 2 and 3 are deliberate
trade-offs made in them, and 1 predates them.

**1. A season marked watched on a NEVER-watched show does not appear.** The reporter's own
characterisation: mark the whole show watched, unmark it, THEN mark a season, and it works — so Plex
only propagates a season mark once some watch record already exists for that show. It could not be
reproduced on the maintainer's server (a season mark there DID set the episodes and the show
appeared, `The Night Agent`, verified 2026-09-02), so the behaviour differs by server or by how the
show got into that state.

The signal exists and is measured: `?type=3&unwatched=0` returned 1,045 seasons rolling up to 522
shows, of which **52 are invisible to the show-level read**. Those season rows carry `viewCount` and
`lastViewedAt` but NO `leafCount`/`viewedLeafCount`, so they say a season was watched and nothing
about how much — the same shape as the show-level bug one level down. Every one on that server is old
(2017–2024), so they are historical residue there rather than fresh marks.

*INVESTIGATED 2026-09-18 — DO NOT BUILD THIS. The detection query is wrong and would mark 52 shows on
SFLIX as watched that nobody has watched.*

The plan was `?type=3&unwatched=0`, roll up by `parentRatingKey`, count the episodes. Measured, in this
order, and each step moved the conclusion:

- `type=2&viewedLeafCount!=0` (today's show-level read) returns 494 shows. `type=3&unwatched=0` returns
  1,050 seasons over 525 parents. **52 parents are absent from the show-level read** — the figure in the
  original note, still exact.
- Those 52 are **false positives, every one**. Show `viewedLeafCount=0`, and via
  `/library/metadata/{show}/children` **every season also reads `viewedLeafCount=0`**, and every episode
  reads `viewCount=0`. The only non-zero field is the SEASON's own `viewCount` (1-5). Nobody watched or
  marked anything: something established a play record on the season object. `Bob's Burgers` is in the
  52 with 0 of ~300 episodes watched, and `Below Deck Mediterranean` with 0 of ~189.
- **`unwatched=0` on seasons has exactly the flaw `watched_titles` already documents for shows.** Its
  docstring says `unwatched=0` "filters on the show's own watch-state row, which marking a series or a
  season does not establish". The inverse bites here: a season's own watch-state row can exist with no
  episode watched at all, so `unwatched=0` returns it.
- Control, to prove the reads themselves were sound: on shows the show-level read DOES return, season
  `viewedLeafCount` matches the episodes exactly (8 of 8, and 4 of 10), and season `viewCount` is NOT the
  watched-episode count (5 against a true 4). So `viewCount` could not stand in for it either.

**The correct signal is a season with `viewedLeafCount > 0` under a show with `viewedLeafCount = 0`, and
it cannot be had cheaply.** The section-level `type=3` response carries NO `viewedLeafCount` under any
param tried (`unwatched=0`, `includeUserState=1`, bare, and `viewedLeafCount!=0`), and
`type=3&viewedLeafCount!=0` is SILENTLY IGNORED — 1,732 rows against 1,050 for `unwatched=0` and 10,631
for the whole library, so it is filtering on something else entirely. The only place the number appears is
`/library/metadata/{show}/children`: one read per show, 525 per person per library, ~5s each and ~4
minutes across 46 users, every sync.

**On this server that cost buys zero findings** — there are no genuine cases, which is consistent with the
original note's "could not be reproduced on the maintainer's server". So: do not build the sweep. If the
reporter hits it again, read `/library/metadata/{show}/children` for THAT show and check whether a season
has `viewedLeafCount > 0` while the show reads 0. That is one request, and it settles it. Only if that
comes back positive is there a bug here at all, and then the question of what `viewed_leaf_count` becomes
is answered by the season's own count rather than needing a decision.

**2. The "Finished" date does not move when a partly-watched show is marked fully watched.** CLOSED.
Plex does not update a show's own `lastViewedAt` when its episodes are MARKED, so a series finished
today still read as finished months ago. Not cosmetic: that date is the recency half of a seed's
weight and halves every ~45 days, so a series marked watched today but dated two years ago never
seeds — you finish a show and get nothing like it.

The cost objection recorded here (an episode read for every library, every sync) was answered by
detecting WHICH shows need it instead of reading for all of them: the cache already holds last
night's `viewed_leaf_count`, and a count that went UP while the show's date stood still has exactly
one cause. A quiet night makes no request at all; marking a few shows costs one small
`/library/metadata/{key}/allLeaves` each, and past a dozen it falls back to the single library-wide
read. Two traps found by probing the live server rather than reasoning: that endpoint SILENTLY
IGNORES `unwatched=0`, and a part-watched episode carries a `lastViewedAt` with no `viewCount` —
on the first real show tried, that abandoned episode's stamp was NEWER than the only episode
actually watched. Both recorded in `pms_all_leaves.xml.txt`.

**3. A pick stays "finished" on the dashboard after being unmarked in Plex.** CLOSED by `ea33454`.
Withdrawal was gated on the `sync.watch_full_days` pass to protect against INCREMENTAL reads, which
could not tell "they un-watched it" from "this pass did not look". Since #108 no read is
incremental, so the gate bought nothing but the seven-day lag. Completeness is now a property of
each person's own read (`UserProfile.history_complete`, stamped by `refresh_watched`) rather than a
roster-wide claim — the Architecture Review on the first attempt found that a roster-wide `True`
would erase credit for every pick in a library that failed to read, because `ShareTokenWatchSource.
fetch` fail-softs past an unreadable section and returns a non-empty answer that looks complete.

**Also outstanding, unrelated to the reporter:**

* **The watch sync now takes ~87s** (was ~27s on the broken read). The cost is 141 serial PMS calls —
  users x their libraries — not any single query. Two levers, neither tried: fetch the static half of
  a library's metadata once instead of once per user, and run users in parallel (`run.concurrency`
  already exists for other work).
* ~~**`545a340` and `83cf07a` never had an Architecture Review.**~~ DONE — reviewed, no HIGH
  findings; its five MED and four LOW are fixed in `5d75496`. Original note: both touch watch history —
  `545a340` changes which rows are DELETED — which the risk list in `.claude/CLAUDE.md` says is not
  optional. Do this before the next release tag.

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
   `row-sources-field.tsx:143-145`. The `row-shelf-placement.tsx` case ("Use the default (Settings)")
   is MOOT as of 2026-09-12 — the global per-library placement default was retired, so the control no
   longer offers it and there is no Settings path left to name.
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

Not audited: nothing on the main SPA is now unaudited. The Dashboard, Users, Runs, Jobs, Logs and
Settings pages were audited on 2026-08-02, and **Requests** (page + its Settings card) on
2026-08-03 — see the two sections below. Still never audited: the job-catalogue copy in
`shortlist/server/services/jobs.py`, which lives in the backend and no `web/` pass will ever reach.

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
- **Both seed knobs are renamed, in every place at once.** "Watches to build from" → **"Watches
  every source builds from"**, "Watches the AI searches from" → **"Watches the AI web search looks
  up"** (the name the row editor already used, so this also ends a setting that went by two names).
  Each label is now a single exported constant (`MAX_SEEDS_LABEL`, `RECENT_COUNT_LABEL`) that
  Settings, the row editor and the rename page import rather than retype. Both number boxes gained a
  "watches" suffix — under a "use the global default" toggle they rendered as a bare unitless digit,
  where `RowSizeField` shows "15 titles". Their `aria-label` is now applied only when the visible
  caption is suppressed: it was unconditional, and an `aria-label` beats a `<label>`, so it was
  overriding the per-person caption the user page passes ("Recent watches for this person").

### Open — not fixed, with the reason

1. ~~**`maintenance.prune` is invisible on the Jobs page.**~~ CLOSED 2026-08-03 — it has its own
   `JobRow` in "Run now" (`pages/jobs.tsx`, "Clear now") with a `SchedulePanel`, a run mutation, and
   error/queued/done states. Verified during the v1 sweep; this entry was stale.
2. ~~**`advanced-section.tsx:38` falls back to `runs.retention ?? 100`.**~~ CLOSED 2026-08-03 — now
   `?? 3`, matching `settings_store.DEFAULTS`. Fixed alongside the `events.retention` control below.
3. **The Plex card's "Plex token" field has no "where do I get this" link**, unlike TMDB/MDBList/Exa.
   Normally filled by the wizard's PIN flow, so it only bites someone re-entering it by hand. Left
   alone rather than assert a support URL I could not verify.
4. ~~**The read-only Plex audit sits at the top of the Danger zone.**~~ CLOSED 2026-08-03 —
   `CleanupAuditCard` moved to `advanced-section.tsx`, above the log-level card.
5. **`JobDetail` renders raw result keys** ("Asked to" + a JSON blob, then `fixed`/`orphans`/
   `demoted` verbatim). Diagnostic rather than everyday, so left as is.
6. ~~**The Jobs "Run now" group mixes reads and writes.**~~ CLOSED 2026-08-03 — `EFFECT_TAGS`
   (`pages/jobs.tsx`) puts a "Changes Plex" / "Can delete" tag on the LINE, rendered by
   `EffectTag` in `job-row.tsx`. A job with no tag changes nothing outside Shortlist's own records.
   Verified during the v1 sweep; this entry was stale.
7. **Backend job-catalogue copy is in `services/jobs.py`**, not the SPA — it reads well, but it is
   the one place a copy pass over `web/` will always miss. Worth noting for the next audit.

---

## Copy audit — Requests (2026-08-03)

The one area the two passes above never reached: `web/src/pages/requests.tsx`,
`web/src/components/requests-settings.tsx` and `web/src/components/settings/requests-section.tsx`
(the last is four lines and needed nothing). Read as a non-technical Plex owner. Shipped alongside
issue #61's "Wanted by" filter, in the same two files.

### Fixed — factual errors (traced to the handler/engine)

1. **"Number of people whose picks it appears in" was impossible.** `requests.min_demand`'s help
   text described `demand` as a count of people whose _picks_ held the title. A requestable title is
   by definition absent from the library, and `filter_candidates` drops every non-library candidate
   before picks exist — so it can never be in anyone's picks. `demand` counts the people whose
   **candidate pool** surfaced it (`rows.py:_record_demand` → `requests.py:accumulate`). Now: "How
   many different people it has to be a good match for before Shortlist asks."
2. **The vote floor is silently ignored for Rotten Tomatoes and Metacritic.** `VOTE_SOURCES`
   (`clients/mdblist.py:29`) is `{imdb, trakt, tmdb}`, and `_gate_by_source` only enforces
   `min_votes` when the chosen source is in it — MDBList reports no vote count for the two critic
   scores. The field claimed it "keeps out obscure titles with a high {source} score from very few
   votes" regardless. Now says plainly that the number is ignored while a critics' score is chosen.
   (The field stays editable: it still matters if the owner switches source.)
3. **"per night" is not what `max_per_run` counts.** Two strings ("Most to auto-request per night",
   "requested for you each night", "so a night can never flood your downloads") assumed one run a
   night. The cap is applied once per run (`request_missing`, `cap = cfg.max_per_run`) and rows
   carry their own schedules. Reworded to "in one run". The claim it _doesn't_ cover — "titles you
   approve by hand aren't capped" — is true: `request_titles` skips every floor and the cap.
4. **"it'll drop off the list on the next run" is false on Sonarr v3.** The arr-presence prune keys
   shows off `report.arr_present`, which is built from Sonarr's own `tmdbId` — a v4-only field, so
   `show_present_tmdb` is empty on v3 (`_apply_arr_state`) and the pending row survives for ever.
   The badge itself still appears, because `/requests/status` falls back to a TMDB→TVDB lookup.
   Weakened to a claim that holds either way: "you don't need to send it again."
5. **"remove it there first, or approving won't add it"** (the exclusion-list warning) asserts
   something no code here proves. What IS proven: `request_missing` never auto-sends an excluded
   title (`elif m.excluded`), and a manual send goes straight to `add_movie`/`add_series` with no
   exclusion check at all. Reworded to the provable half, keeping the Arr's own term ("import
   exclusion") so the owner can find the setting there.
6. **"Never suggest or request these again" / "no run suggests or requests them"** over-claimed on
   the "suggest" half. A rejection only feeds `_handled_requests` (`context_builder.py:278`), which
   is the engine's _request_ skip set; nothing stops a rejected title being picked into a row if it
   later lands in the library. Now: "no run will ask Radarr or Sonarr for them again."
7. **The Sent tab said "Nothing sent yet" while sent titles were merely filtered out**, and the
   Rejected tab rendered a blank list with an "Allow all again (0)" button. Only Waiting had a
   filtered-to-nothing message. All three now share one, and it names the control that undoes it.
8. **"Strong picks are sent automatically" was unconditional** in the "Nothing waiting" empty state,
   but `requests.auto_send` can be off — in which case `request_missing` queues everything with the
   reason "auto-send is off". The empty state now reads the setting.
9. **The off-state banner claimed only that nothing can be sent.** It can also say the stronger true
   thing: `_request_phase` skips the whole pass when requests are off and
   `persist_request_queue` returns early with no `report.requests`, so nothing new is added either.

### Fixed — jargon, wayfinding, states

- **Radarr/Sonarr were never explained on this page.** Both are glossed at first use on the page
  header and in the Settings card ("the apps that fetch films and TV"), matching what the
  Connections cards already say.
- **Both "Enable in Settings" buttons went to `/settings`**, a nine-section page, without naming
  what to look for. Now `/settings#requests` (`use-hash-scroll.ts` handles the cold load), labelled
  "Go to Settings → Requests".
- **MDBList, in both directions.** The connected note said "manage or test the key in Connections";
  the warning said "add its free API key in Connections". Neither named the card. Both now say "the
  MDBList card in Connections" — the third place this exact miss has been fixed.
- **"tag" and "quality profile" are now spelled out** where they first appear ("this label … a
  'tag', in their words"; "choose how good a copy to grab and which folder to save it in").
  Verified against `_resolve_tag`, which does create the tag in the Arr if it is missing.
- **"Minimum reviews"** was the only place the app called votes reviews; it shows "(5,000 votes)" on
  every card and has a "Votes" filter. Now "Minimum votes".
- **Controls renamed for what they do**: "Auto-send the strongest picks" → "Send the strongest
  titles without asking"; "Auto-send when rated" → "Send without asking when rated"; "Auto-send vs.
  ask me" → "Send on its own, or ask me first". "Radarr — Handles movie requests" → "Fetches the
  films Shortlist asks for."
- **The Arr error state was a dead end** ("check its connection in the Connections section"). It now
  names the card and the Test button that actually exists on it (`connection-card.tsx:254`).
- **Capitalisation**: `wantedByLabel` returned "Wanted by Sarah" but "wanted by 3 people"; the
  Rejected row printed a bare "wanted by 3" with no noun. Both now use one label.
- **`Sonarr/Radarr` vs `Radarr/Sonarr`** was mixed within one page; standardised on films-first,
  matching the "Sent to Radarr & Sonarr" heading.

### Fixed in the follow-up (2026-08-03) — ordering + names

1. **The "Wanted by" names now read as people, resolved client-side.** No payload change was needed:
   `wanters` and `why[].user` store `UserProfile.username`, which is exactly `User.username`
   (`context_builder.py:518` → `rows.py:1266`), and `GET /api/users` already returns `username`
   **and** `display_name` for every user in the table (`serializers.py:user_dict`, no filtering).
   `lib/user-names.ts` builds the map; a username with no match resolves to itself, so a departed
   sharer never renders blank. The **filter is still keyed on the username** — only the chip's label
   changed — so two people sharing a display name can't be merged into one filter.
2. **The Waiting toolbar leads with Send**, then a rule, then Delete and Reject. The separator is
   what the earlier note said this wanted; the copy is unchanged.
3. **`Send on its own, or ask me first` now sits ABOVE `Guardrails`,** and Guardrails gained a line
   saying what it is ("the lowest bar a title must clear before Shortlist will ask for it at all").
   `Most to send automatically in one run` moved into the auto-send fieldset and is hidden when
   auto-send is off — `request_missing` only reaches the cap after the auto bars, so with the switch
   off it can never apply to anything. `docs/guides.md` steps 4–5 were re-ordered to match (they
   also still said "per night" and "minimum number of reviews").
4. **The Waiting tab now says what it is waiting for**, above the toolbar: "Titles Shortlist wanted
   for your people that your library doesn't have. Nothing here has been sent — send the ones you
   want, or reject the rest." Both halves are traceable: `persist_request_queue` deletes a pending
   row the library now holds, and no handler ever moves a row back from `sent` to `pending` (only
   `rejected` → `pending`, via restore), so a pending row has never been sent.
5. **The auto-bar warning was over-claiming and is now weaker.** It fires when _either_ auto bar is
   below its guardrail (an `||`), but claimed "everything that gets past those minimums will be sent
   without asking" — only true when both are. Now: a bar below its guardrail stops nothing.

### Open — not fixed, with the reason

1. ~~**The `MAX_INBOX = 500` cap is disclosed, not solved.**~~ CLOSED 2026-08-03 — the "Wanted by"
   filter is now the SERVER's. `GET /api/requests` takes a repeated `wanted_by` query parameter (one
   `wanters` username per value, union not intersection) and applies it BEFORE the sort and the cap,
   so picking a name searches the whole history rather than the 500 rows the page loaded. No
   parameter means exactly what it meant before. It filters in **Python, not SQL**, on purpose: the
   handler's read is already `.all()` over every non-hidden row (the cap bounds the payload, not the
   query), so filtering rows already in memory costs nothing extra — and it avoids depending on
   SQLite's JSON1 `json_each` to ask whether a JSON array column contains a value.

   Three things worth keeping:
   - **The cap note now tells one of two truths, and had to be able to tell either.** Unpicked it
     still says the page loads 500; once a name is picked and the server's answer has landed it says
     the limit no longer applies — and if that person's own titles fill the cap, it says _that_
     instead. While the answer is in flight, or if it failed, the old note stands, because the list
     is still the client-side narrowing of the loaded page (`applyPeople` is deliberately kept in
     `narrow()` for exactly that window, so a chip never blanks the list or leaks someone else's).
   - **The roster stays on the unfiltered read.** `peopleOptions` (and the tab counts) come from the
     unfiltered query, or picking a name would delete every other chip and strand the filter.
   - **A picked chip's count is re-read from the server's answer.** Otherwise the fix would have
     introduced a fresh lie — "Sarah (12)" above a list of forty of Sarah's titles.

2. **`ARR_STATUS_LABELS` uses "Not monitored"**, Sonarr/Radarr's own word for a state that means
   "it isn't even looking" (`_status_for`). Kept deliberately — matching the Arr's vocabulary is how
   the owner finds the toggle there — but it is jargon by the letter of the rule.
3. ~~**`docs/guides.md` still carries two claims the UI stopped making.**~~ CLOSED 2026-08-03 —
   both fixed when the guide was split into `docs/guides/`. `requests.md` now says films drop off on
   the next run while **shows only drop off on Sonarr v4** (v3 does not report the TMDB id), and the
   inbox reads "Send to Radarr/Sonarr", films-first. Verified during the v1 sweep.

## Row editor: delete/rename/tiles (2026-08-03) — ALL RESOLVED

The editor gained the destructive actions, an editable name, and dashboard-style stat tiles.
Architecture Review found five issues; all are fixed (`1b03ba5`, `d50e459`).

**CLOSED — the DEFAULT row no longer carries its own `name_template`.** Fixed in three places,
because one was not enough and the first two attempts each looked complete:

1. `PATCH /collections/{id}` skips the column for that row.
2. `POST /collections/{id}/rename` wrote it too, TWELVE LINES BELOW the new guard, in the same
   flow — the rename screen PATCHes and then immediately POSTs, so a column cleared by the guard
   came straight back one request later. The first test passed because it stopped at the PATCH.
3. `_serialize` still SHIPPED the column, and the SPA reads `name_template || name` in three
   places, so a database written before the guard showed a title Plex no longer used — and the
   rename sent it as `old_template`, matched nothing (`collection_reconcile.py:527`), reported
   "renamed 0 collections", and left the next run to build a second collection beside the old one.
   Neutralising it in the API is what makes a migration unnecessary for existing databases.

The lesson worth keeping: **a guard is only as good as the narrowest path that reaches the same
write.** Grep for every writer of a field before believing one guard covers it.

Two things worth keeping from this round, both invisible to a passing test suite:

1. **A delete-failure alert rendered OUTSIDE its dialog is invisible.** The dialog stays open on
   failure and Radix marks everything behind it `aria-hidden`, so the message was buried under the
   overlay and absent from the accessibility tree — a failed delete looked like a button that did
   nothing. The alert now lives inside the dialog. This shipped that way for as long as the rows
   list has existed; no test caught it because no test exercised a failing delete.
2. **Asserting the FIRST call is a dry run does not pin down the second.** Flipping the real removal
   to a second dry run — reporting success while removing nothing — left all nine tests green.
   `expect(cleanupCollection.mock.calls).toEqual([[id, true], [id, false]])` is the assertion that
   holds both ends. This is the `.claude/rules/testing.md` "call count right, arguments wrong" shape,
   on a Plex write path.

---

## Jobs schedule control + `events.retention` (2026-08-03) — both closed

Two copy-audit items that both turned out to be missing CONTROLS, not bad sentences.

**1. "clear the box and it stays off" described a box that did not exist — and an off switch that
did nothing until the next restart.** The claim lives in the backend job catalogue
(`services/jobs.py`, `sync.check`), which is the copy every `web/` pass misses (item 7 of the
2026-08-02 Jobs section). Three separate faults behind it:

- **The off control was unreachable on a default install.** `SchedulePanel` rendered its ghost
  "Turn this schedule off" button only when `settings[sync.check_cron] !== ""`. But the whole point
  of `sync.check_cron` is that a STORED blank means off while an ABSENT row means "nightly at
  05:45" (`scheduler._resolve_cron(blank_means_off=True)`), and `all_public()` folds the default in
  — so both states read as `""` in `GET /api/settings`, and the one job the scheduler goes out of
  its way to let you switch off had no off switch until some other frequency had been saved first.
  The panel now reads the EFFECTIVE cron from `GET /api/schedule`, which is the only place that
  distinction is resolved. (`scheduler.py:35` already said the UI must do this; the Jobs panel was
  the one place that didn't.)
- **`CronPicker`'s "Daily" chip WAS the off switch, mislabelled.** Its blank preset writes `""` —
  "use the built-in default" for five jobs, "off" for this one. A chip labelled Daily that silently
  switches off a job which writes corrections to Plex is the worst shape a control can have. The
  blank preset's label is now a prop; optional schedules get **Off**, everyone else keeps Daily.
- **`PUT /api/settings` only rebuilt the scheduler for a hardcoded four keys** —
  `sync.watch_cron`, `sync.users_cron`, `backup.cron`, `backup.max_keep`. So editing
  `sync.check_cron`, `privacy.sync_cron` or `maintenance.prune_cron` saved the setting and left the
  live APScheduler trigger alone until the container restarted: turning the drift check off did not
  turn it off that night. The trigger set is now derived from `DEFAULT_CRONS`, so a cron added there
  can never be the next one whose edits wait for a restart.

The copy moved too, to describe the control that now exists ("open this job and choose Off under
Frequency"), plus `docs/guides.md` and `docs/reference.md`.

Worth keeping: `test_every_schedulable_cron_takes_effect_on_save` asserts the LIVE trigger, not that
`next_run` is non-null — a job left on its boot-time cron still reports a next run, so the obvious
assertion passes whether or not the edit was applied. Both new tests were verified to fail against
the old four-key set.

**2. `events.retention` now has a control**, next to "Runs kept" in Settings → Advanced, named
**"Change log kept"** and wired exactly like its sibling (same 0–24 bound the API validates, same
Forever/3mo/6mo/12mo chips, 0 = for ever stated in the copy). It defaults to Forever, which is why
it had no control: nothing forced the question. The copy says what the log holds (Plex writes,
share-filter writes, settings changes) and admits there is no screen for it — it is read at
`GET /api/events/log`, which no SPA page consumes.

**3. CLOSED (2026-08-03) — the way back to the built-in 05:45.** Switching the drift check off used
to be one-way in practice: the only route back was typing a cron into Custom, because the picker had
no chip for "the built-in default" and the SPA is deliberately not allowed a second copy of
`DEFAULT_CRONS` (`settings_store.py:144` records what that cost last time). So the server now says
what the default is, and the SPA still holds no copy of it:

- `ScheduleJobOut` gained **`default_cron`** — the built-in cron for that job's setting, straight
  from `DEFAULT_CRONS`. `GET /api/schedule` was already the panel's source for the effective cron,
  so no new request. (OpenAPI snapshot + `api-schema.d.ts` regenerated.)
- `CronPicker` gained a **`Built-in (05:45)`** chip, labelled from that cron via `dailyCronTime`,
  which returns null for anything a bare clock time would misdescribe (weekday-only, stepped) so the
  chip falls back to plain "Built-in" rather than lying. It only renders where a `defaultCron` is
  passed — for the other five jobs the blank chip ("Daily") already IS restore-the-default, and two
  chips for one state would be worse than none.
- **On the wire, "use the built-in default" is `null`** — `PUT /api/settings
{"sync.check_cron": null}` DELETES the row. The two obvious alternatives are both wrong, and both
  are pinned by an assertion: `""` is the OFF switch for this job, and writing `45 5 * * *` pins a
  copy of today's default (`using_default` would go false, and a later change to the built-in time
  would never reach the install). `SettingsStore.unset` is the new counterpart to `has_row`.

Both breakages were verified: storing `null` in the row instead of deleting it leaves
`GET /api/settings` reporting `None` where every other unset cron reads `""`; writing the default
expression fails the `using_default` assertion. The live-trigger assertion is the one that matters
most — the job is REMOVED from APScheduler while off, so "restore" has to re-register it, and the
test reads the trigger string rather than `next_run is not None`.

**Still open here, small:** for the five jobs where blank means default, the chip is labelled
"Daily" rather than the time it actually runs at ("Built-in (03:00)"). Accurate but vague; now
cheap to fix, since `default_cron` is on every entry of `GET /api/schedule`.

## Requests: the people picker offers everyone (2026-08-03) — CLOSED

The "Wanted by" names were inferred from the titles on the page, and the page loads at most 500.
Anyone whose requests were all older than that never appeared as an option — and the server-side
filter would have found their titles perfectly well, if only they could be picked.

Fixed by building the list from `/api/users`, which the page already fetches for display names, so
no new endpoint was needed. Counts still describe the ACTIVE TAB, and a person with none on it shows
no count at all: "0" would read as "has never asked for anything" when it only means "nothing of
theirs is here".

The shape worth remembering: **a chooser must not be limited by the page you happen to be looking
at.** The filter was always able to reach the whole history; the list of things to choose from was
the part that could not.

## Pipeline order-of-operations audit (2026-08-11) — CLOSED

All seven findings fixed on 2026-08-11 (`f64795c`, `da31cda`, `950ddc6`, `005eb4a`), plus the trace
work they exposed (`cc302be`, `a1d46ab`). Each fix has a test that was verified to FAIL against the
reintroduced bug — twice that mattered: the first refresh-collapse test re-implemented the
composition rule instead of driving the branch and passed against the bug, and the first attempt at
that fix silently broke the "strongest two-thirds survive" guarantee while fixing the collapse.

The findings are kept below in full: the reproductions are the record of what these code paths do
wrong when they are got wrong, and two of them (shared-row parity, settings that decide contents)
are shapes this codebase keeps producing.

### Original findings

Architecture Review of the whole pick pipeline, prompted by the owner asking whether every setting
acts at the right point. Findings below in the order they should be fixed. Two HIGH, three MED, two
LOW, plus one gap the audit didn't cover that the owner found immediately after.

### 1. HIGH — a refresh night collapses a row to a single taste, permanently

`_rank_against_pool` (`rows.py:1703`) re-sorts survivors + newcomers by raw pool index, i.e. pure
score — exactly the ordering `diversify_by_seed` exists to counteract. `[:k]` then evicts every
weaker-seed pick. Measured on the real function:

    bootstrap night   {Breaking Bad: 7, Fargo: 6, Chernobyl: 2}
    refresh night     {Breaking Bad: 15}
    next refresh      {Breaking Bad: 15}

It never recovers — the collapsed row is what carries forward. This is the failure `ranking.py`
documents as ALREADY FIXED; it returns the moment a row has history, so every per-person row on the
server is subject to it. Fix: re-diversify after merging. `diversify_by_seed` reads `Candidate`;
Picks carry `seed_tmdb_id`, so it needs a Pick-aware sibling. Verified remedy restores the bootstrap
spread and keeps the pool's best pick leading, so `{top_seed}` naming is unaffected.

### 2. HIGH — the shared-row path ignores six settings the editor offers

`_shared_row` (`rows.py:2090`) never calls `_apply_order`, `_is_refresh_night`, `_reusable_prior`,
`_apply_watched_cap` or `_prefer_watched` — all of those live only in `_build_section_picks`.
Ignored on shared rows while the editor still renders the control: `pick_order`, `freshness`,
`watched_pct`, `rewatch`, `unstarted_only`, `cold_start`. Proven: all four pick orders give
byte-identical output on a shared row. Symptoms: a shared row set to "Shuffled" never shuffles; one
set to "never change" rebuilds and rewrites to Plex every run.

Same shape as the `recency` bug fixed on 2026-08-11. `request_tag` is the model to copy — the editor
hides it for shared rows (`row-editor.tsx:1055`). Decide per setting: honour (`pick_order`,
`freshness`) or hide (`watched_pct`, `rewatch`, `unstarted_only`, `cold_start` have no coherent
meaning for an aggregate row).

### 3. HIGH — changing a setting does not rebuild the row

Found by the owner, not the audit. `_is_refresh_night` is purely calendar-based, and picks store no
record of the settings they were built under, so a deliberate configuration change waits behind a
cadence designed to suppress accidental churn — up to a fortnight. The owner set "Recent releases"
and 36 of 42 rows redelivered byte-identically, which reads as the feature being broken.

Freshness should suppress churn when NOTHING changed, not when the owner changed the recipe.
Precedent exists: `_seed_moved` already forces a rebuild when the row's premise changes.

Fix: store a recipe fingerprint alongside the picks and force a rebuild on mismatch. Fingerprint the
settings that decide CONTENTS — recency, watched_pct, rewatch, unstarted_only, max_seeds,
seed_window, candidate_sources, media, library_keys, size, cold_start — and NOT the ones that decide
presentation or cadence (`pick_order`, `freshness`, name template, poster, placement). Needs a
migration for the new column.

### 4. MED — cold-start rows are half their configured size

`_cold_start_picks` (`rows.py:2346`) splits `k` across `sections_by_type()` regardless of the row's
media; `_build_section_picks` then takes only that library's share. Probed: asked for 20, each
library sees 10. On any server with both a movie and a TV library — nearly all — every cold-start
row is half size. Fix: size the cold pull per target library, or pass the row's media in.

### 5. MED — cold-start ignores `library_keys`

Cold picks come from `plex.top_rated` over the representative library, not the row's pinned ones;
delivery then drops every pick the target library lacks (`delivery.py:322`). A library-pinned row
comes back short or empty for a new user, reported as a green run.

### 6. MED — `unstarted_only` isn't re-checked on carry-forward

`_reusable_prior` only filters started shows when `pct <= 0`, but the editor tells the owner the
toggle "only changes anything if you've allowed already-watched titles above" (`pct > 0`) — where no
filter is applied. The row keeps showing a series the person has since started, for up to a
fortnight. Fix: pass `_started_shows` in when `spec.unstarted_only`, independent of `pct`.

### 7. LOW — two documentation items

* `rows.py:409` says a `{top_seed}` title renders from `picks[0].seed_title`; line 423 of the same
  docstring correctly says `render_row_name` takes the lowest `rank`. Line 409 is pre-fix wording,
  and it is the sentence someone will trust next time they touch ordering.
* `context_builder.py:687` — the `or` fallbacks for `min_history` / `recent_count` / `max_seeds` are
  safe only because the validators exclude the falsy value. Correct by coincidence of the bounds,
  not by construction: lower a validator floor to 0 and three settings break silently at once.

### Confirmed CORRECT (don't re-audit)

Rank vs display order is cleanly separated everywhere (`render_row_name` uses `min(rank)`,
`_seed_moved` reads rank-ordered `previous_picks`, delivery writes display order deliberately). The
watched cap never contradicts ranking and cannot exceed its cap. `_pad_picks` respects ranking.
Precedence is user -> row -> global throughout, with `is not None` wherever 0/0.0/"" is legitimate.
No setting is orphaned at the settings->engine seam. Leak-safe ordering intact.

## Agregarr removal review (2026-08-13)

No HIGH findings. The migration, the Plex-side ordering, leak-safe write ordering and rule 10 were
all cleared empirically. Everything found was fixed in the same commit EXCEPT one pre-existing item:

### RESOLVED — `pipeline.py`'s raw `{e}` logs of plexapi errors are redacted again

Removing the agregarr mirror took `pipeline.py`'s only `redact()` caller with it, and the comment
explaining why it was needed. Two raw `{e}` logs of plexapi exceptions were left unguarded at
`_apply_order` and `_collection_order_phase`.

**What the investigation established** (this is the part worth keeping — it settles a question the
codebase had two different beliefs about):

* plexapi raises `f'({status}) {codename}; {response.url} {errtext}'` — `server.py` `query()`.
* `query()` builds its URL with `self.url(key)` and **no** `includeToken`, so under default config
  the token travels as a HEADER and `response.url` is clean. The logs were safe by default.
* But `url()` appends `X-Plex-Token=` when `log.show_secrets` is on, and `PlexConfig.get()` checks
  the ENVIRONMENT first — so `PLEXAPI_LOG_SHOW_SECRETS=true` on the container is enough to put a live
  token into every plexapi exception message, with no change to our code.

So `plex_pms.py:150` was never wrong: it describes its own timing adapter dropping the query string,
which is correct regardless. It simply is not a guarantee about exception text, and nothing else was
guarding that. Both logs now pass through `redact()`, and
`test_hub_ordering.py::test_a_plexapi_failure_is_redacted_before_it_reaches_the_log` fails (printing
the token) if either is reverted.

**The general rule, since this is the second time it has bitten:** `redact()` being imported by only
one caller in a module makes it deletable-by-accident along with that caller. Anything derived from
an exception message goes through it — check for the import surviving whenever a module's last
`redact()` user is removed.

## CLOSED — delivery over-reported titles Plex silently dropped

**Fixed in `54299476` (2026-08-18), the same commit that wrote this entry; it was never marked closed.**
`fetch_items` returns `(items, missing)` and every delivery path drops the missing keys from
`diff.added` and `wanted_keys`. Coverage for each path, checked 2026-09-14 by removing the filters (both
tests fail without them): create `TestTheDiffReportsWhatLandedNotWhatWasAsked`, in-place update
`test_an_in_place_update_does_not_report_a_title_plex_dropped`, rebuild
`test_a_rebuilt_row_does_not_report_a_title_plex_dropped`. `titles_added` is `sum(len(diff.added))`
(`run_persistence.py`), so it follows. The original entry is kept below.


**Found:** architecture review, 2026-08-18, while fixing the run-#17 delivery failure.

A batch `fetchItems` returns 200 with only the titles that still exist (recorded:
`tests/fixtures/pms_metadata_batch_partial.json`); the deleted ones are simply absent. But
`_deliver_one` computes `diff.added` from `wanted_keys`, not from what came back — so a 25-pick row
that lost 2 titles mid-run reports "25 added" while Plex holds 23. `run_persistence` sums those diffs
into `titles_added`, so the run page and the events row both overstate it, and "why isn't X in my
row when the run says it delivered it" is unanswerable from the audit trail. That is the one thing
plex-safety rule 10 exists to guarantee.

**Pre-existing** — not introduced by the per-row requests work. `fetch_items` now at least logs the
dropped keys (`plex_pms.py`), so there is a record, but the diff is still wrong.

**Why it is not fixed yet:** the correct fix changes `fetch_items`'s return contract (items AND what
was missing) and every caller, then filters `diff.added`/`wanted_keys` on both the create and update
paths. That is meaningful churn in `delivery.py`, the highest-risk file in the repo, and it was
identified on release eve. Deliberately deferred rather than bundled into a release.

**When doing it:** cover all three delivery strategies (create / in-place update / rebuild) with a
dead key, and assert the persisted `titles_added` matches what Plex actually holds.

---

## FIXED 2026-08-24: shared-row watches are invisible to the hit rate (found 2026-08-23)

Fixed by giving shared rows their own credit table rather than fanning picks out. Migration 0078 adds
`shared_row_watches` — one row per (person, shared row, title) carrying the same
`watched_at`/`finished_at`/`max_percent` triple `picks` carries. `watch_events.shared_credits` is the
twin of `event_credits`, differing only in the title pool (every title a live shared row has carried,
from `RunSharedRow.picks`, because there are no pick rows to intersect against) and in asking
`RowMembership.visible_shared_rows`, which additionally tests the run's own audience snapshot.
`report_service.resolve_outcomes` folds them into the same person-title outcome, so a title on both a
personal and a shared row is still one thing they watched.

The two paths stay deliberately separate: letting a shared row satisfy PERSONAL membership would
stamp someone's own pick for a title their own row had already dropped, because a different row was
still showing it. Tests: `tests/unit/test_shared_row_watches.py`, including all four gates proven
by breaking them — timeline, audience snapshot, delivery ledger, and earliest-credit.

The decision the entry below asked for: NOT fanned out. The original text follows.

### Original entry

A shared row files its result under `shared_<slug>` (`engine/rows.py:2492`), which is nobody's user
slug, so `persist_report` routes it to `_persist_shared_row_report` — and that writes
`RunSharedRow.picks` as JSON and **no `PickRow` at all**. `reconcile_watched` only ever stamps
`PickRow`, so a title watched from a shared row has never counted toward anyone's hit rate, and never
appears in the dashboard's "Recently watched from Shortlist".

Predates the membership rule and is unrelated to it; found while auditing that change. It means every
hit-rate figure the app has ever shown measures per-person rows only.

Not fixed here because it is a schema question, not a bug fix: a shared row's picks are one set
delivered to N people, so crediting them needs a decision about whether to fan them out into one
`PickRow` per audience member (which is what the report's `(person, title)` unit assumes, and what
would make `per_row` work) or to track shared rows on their own axis. Worth deciding before the next
report change.

---

## Known limitation: shared rows delivered before the `media_type` fix can never be credited

**Status: accepted, not fixed. Verified on SFLIX 2026-08-24.**

`RunSharedRow.picks` is a JSON blob, and `_shared_key` refuses to guess a title's type when the blob
carries no `media_type` — correctly, because TMDB ids are namespaced per type and matching on the id
alone would credit the wrong title. `_pick_dicts` now writes `media_type` on every pick, but the rows
already in the database were written before it did.

On SFLIX every `run_shared_rows` row up to and including run 25 (2026-08-23) carries
`media_type: None` for every pick. The effect is measurable: `thats_no_moon` played The Bear at
2026-08-23 19:41, the play is in the scan and resolves to `(136315, "show")`, run 25's shared row
contains The Bear — and `shared_credits` returns **0**, because the row's key pool was built from
picks with no type.

Not backfilled, and mostly not backfillABLE. Measured on SFLIX across all twelve stored shared rows:
runs 1-23 carry neither `tmdb_id` nor `rating_key` on any pick — their blobs hold only
`title/year/rank/rating/reason/seed_title/sources/affinity`, so there is no id to join on by any
route, and no migration can invent one. Only run 25 carries a `rating_key` (all 80 picks), and its
type could in principle be recovered through `tmdb_by_rating_key`. That is one day of one row on one
server, against a data migration on a live database. From the first run on the fixed build onward the
rows key correctly, so this decays to nothing on its own.

What it means for anyone reading the dashboard in the meantime: shared-row watch counts start from
the first run after the upgrade, not from the row's whole history.

## FIXED 2026-09-13: last-run titles matched in every library on an on-demand removal

Found by the Architecture Review of the issue #121 fix. **Older than that fix; not a regression.**

`collection_reconcile._walk_row_collections` unions `_delivered_titles_by_user` (the last run's
recorded titles for this row) into `displays` and throws away the library each was recorded in. The
#121 guard (`titles_other_rows_build`) renders other rows WITHOUT picks, so for a `{top_seed}` row it
claims only its fallback name, never the "Because you watched X" it actually wore.

Precondition: one person has two `{top_seed}` rows, one Movies-only and one TV-only, and on the last
run both rendered the same title (both seeded by the same watch). Deleting, switching off or
resetting the poster of the Movies row then matches the TV row's collection by that recorded title.
The title-clash check never compared `{top_seed}` templates, so this pair was always allowable.

*Fixed in the same change:* `collection_reconcile._claimed_titles` adds, for every OTHER `{top_seed}`
row this person is in the audience of, the `(library_key, title)` pairs the delivery ledger last
recorded for it — per row, so it does not depend on which row the latest run happened to build. A
claim only ever blocks a title match; it never selects a collection. Static rows are left out because
a rename writes no ledger entry, so their recorded title can go stale. Pinned by
`test_collection_reconcile.py::TestATitleAnotherRowBuildsUnderIsNeverThisRows::
test_what_another_top_seed_row_was_delivered_as_is_claimed_in_that_library`.
