# #173 — shows whose watched episodes were deleted read as never watched

Status: implemented 2026-11-01 (uncommitted), two Architecture Review passes, proven read-only on production.

## Verdict

A bug. A 0% row and an "only series they have not started" row both promise to leave out anything
the person has touched (`RowPolicy.zero_pct_exclusions` docstring). A show whose watched episodes are
no longer in the library breaks that promise.

## Mechanism (measured, not inferred)

Throwaway PMS 1.43.5.11029 (the reporter runs 1.43.5.11079), a Personal Media TV library, 4 shows of 4
episodes each:

| Action | show `viewCount` | `viewedLeafCount` | `lastViewedAt` | in `unwatched=0` | in `viewedLeafCount!=0` | On Deck |
| --- | --- | --- | --- | --- | --- | --- |
| Watch ep1+ep2 | 2 | 2 | set | yes | yes | yes |
| ...then delete ep1+ep2 files, scan, empty trash | **2** | **0** | **kept** | **yes** | **no** | **yes** |
| ...then "mark series unwatched" | cleared | 0 | cleared | no | no | yes (stale) |
| Watch ep1+ep2, then unmark both episodes one by one | **2** | 0 | **kept** | **yes** | no | no |
| Watch ep1+ep2, then "mark series unwatched" | cleared | 0 | cleared | no | no | no |
| Mark whole series watched (no plays) | absent | 4 | absent | no | yes | no |

So:

- Deleting watched episodes reproduces the reporter's state exactly, Continue Watching included.
- A series-level "Mark as unwatched" clears the show counters. An explicit un-watch is respected.
- Unmarking each episode individually leaves the show counters behind. This is the one false
  positive of the fix below. It needs a deliberate per-episode un-watch of every watched episode, and
  the documented way out is "mark the series unwatched".
- Marking a series watched sets no show `viewCount` (the #108 finding). So neither filter alone is
  complete: the right set is the union.

Production server, owner token, read-only, TV section 2 (5,012 shows):

- `viewedLeafCount!=0` = 485. `unwatched=0` = 528. Union = 547.
- 62 shows have show `viewCount>0` and `viewedLeafCount=0`. That is the #173 state.
- In 54 of those 62, every episode now in the library was added after the show's `lastViewedAt`.
  The watched episodes were removed and others added later. 61 of 62 were last viewed over a year ago.
- An allLeaves read of 40 of them found no episode with `viewCount>0`.

Code path that drops them:

- `PlexClient._read_watched_page` (`shortlist/engine/clients/plex_pms.py:2879`) asks for
  `viewedLeafCount!=0`, so the server never returns them.
- `PlexClient.watched_titles` (`plex_pms.py:2477`) discards any show with `viewedLeafCount<=0`
  client-side as well.
- `started_shows` (`shortlist/engine/rows.py:639`) needs `viewed>0`, and `watched_titles` (`rows.py:338`)
  needs a fraction. Even if the item arrived, neither would exclude it.

## One read covers both sets (measured)

Plex's grouped filter syntax ORs two conditions in one request:

```
type=2&push=1&viewedLeafCount!=0&or=1&unwatched=0&pop=1
```

On the production server this returned exactly the 547-show union. With `sort=lastViewedAt:desc` the
order stayed newest-first and `totalSize` was 547. So the fix costs no extra request. The ungrouped
form `or=1&a&b` returned 5,953 rows, more than the library holds. The `push`/`pop` brackets are
required.

## Design (as built)

- **Read** (`plex_pms.py`): the show filter is `push=1&viewedLeafCount!=0&or=1&unwatched=0&pop=1`. The
  client-side guard `_show_was_watched` keeps a show with `viewedLeafCount>0` OR `viewCount>0`. Such a
  show arrives with `viewed_leaf_count=0` and `watch_count=1`. No episode count is invented.
- **Cache:** no migration. `viewed_leaf_count=0` round-trips, and a falling count is never re-dated.
- **Model** (`models.py`):
  - `WatchedItem.only_played_earlier` is a SHOW with `viewed_leaf_count == 0`.
  - `UserProfile.take_history(items)` keeps those out of `history` and puts them in
    `UserProfile.played_earlier`.
  - It is called wherever history comes from a read: `rows._run_user`, the watch_sync prefill and
    `context_builder.profile_with_history`.
- **What that keeps them out of:** taste, seeds, the cold-start count, AI prompts, the shared-row
  watcher count, crediting, the requests row and finished titles.
- **Why they must stay out:** these shows prove a start and nothing more. An Agregarr trailer play
  leaves the same state once the trailer is cleaned up. Passed through as `(1, leaf)`, a 1-episode
  show would read as finished, and a recent trailer play would be the person's freshest seed.
  This was the first review's MED.
- **Engine** (`rows.py`):
  - `RowPolicy.played_earlier` is filled by `load_watched_breakdown`, from history too, so a direct
    `history=` assignment still routes correctly.
  - `RowPolicy.started()` is `started_shows(watched_shows) | played_earlier`. It feeds
    `zero_pct_exclusions`, the unstarted-only `pool_exclusions` and `_reusable_prior`.
  - `ratings_policy` reads both lists, so the user page and the run agree on rating trust.
- **Diagnostic** (`support.py`): a 0-episode record counts as started at cap 0 and is never finished.
  Copies of a title keep the furthest-watched one.
- **Report:** the "titles watched" figure skips 0-episode shows.
- **Replay eval:** filters them out of histories, as a run does.
- **UI:** the user page's watch history shows such a show as "watched before", not "0 of N episodes".

## Live proof (read-only, 2026-11-01)

Ran the whole branch package inside the production container, beside production's own code, with
direct share-token reads and no cache writes. Across 52 people:

- `history`, `watched_titles` and `watched_shows` were identical to production's.
- 173 shows were newly set aside as played earlier, and exactly 173 were added to 0% exclusions. None
  were removed.
- 13 shows picked for someone in the last 60 days are now recognised as already started.

## Tests (write first)

- Read: a show with `viewCount>0` and `viewedLeafCount=0` is returned with `viewed_leaf_count=0` and lands in `played_earlier`.
  A show with neither is dropped. A server that ignores the filter returns the whole library and the
  answer is still right.
- Engine: at 0% and with unstarted-only, a played-earlier show is excluded. A truly untouched
  show stays eligible. At 30% it is not counted as finished. It never becomes a seed.
- Cache: `viewed_leaf_count=0` round-trips through `_upsert`/`_to_item`. An incremental read does not lose it.
- Series un-watch: the show leaves both filters and is eligible again.
- Owner/shared isolation: user A's flag never appears in user B's exclusions.

## Risk and review

This touches watch history and user identity: per-token reads and the cache. That is an Architecture
Review trigger. It writes nothing to Plex.

## Not in scope

79 more owner shows carry `lastViewedAt` with no `viewCount` and no watched episode. 14 of the 40
sampled have a part-played episode. Whether a part-played first episode counts as "started" is a
separate question. #173's examples have no offset, and the fix above does not change that question.
