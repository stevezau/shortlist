---
title: "Reference: how Shortlist decides things"
description: What "already watched" means, watched vs finished, why the owner sees every row, how a pick is chosen, and how rows are kept private.
heading: How Shortlist decides things
---

## Watched titles (and why one can still be recommended)

Shortlist excludes what someone has already watched. Each run reads every user's **complete watched
set directly from your Plex server, as that user**, with no extra configuration, no database mount, and
it works for every account on the server.

The mechanism is the per-user server token Plex already mints for every share. When you share
libraries with someone, plex.tv issues a server-scoped `accessToken` for their account
(`GET /api/servers/{machine}/shared_servers`); reading `library/sections/{key}/all?unwatched=0` with
that token returns exactly the titles Plex considers watched **for them**, carrying their own
`viewCount` (movies) and `viewedLeafCount`/`leafCount` (shows). The owner isn't shared to their own
server, so their set is read with the admin token; a managed Home profile with no share of its own is
read by briefly switching to it and exchanging for a server token (the same path the privacy system
uses).

This is what stops watched titles reappearing. Plex has two notions of "watched": a **playback
session** (something was streamed) and a **mark-as-watched** (ticked off, or a whole season marked,
with no play). Plex's playback-history API reports only the first, and only the most recent ~200
plays, so a heavy watcher's older titles and everyone's marks are missing from it. `viewCount > 0`
(what `unwatched=0` filters on) counts **both**, at any depth — on one real server, all ~13k watched
titles rather than the ~1k the history API returns.

### What "already watched" means for a show

A movie is watched the moment it is played. A show has no such moment — Plex gives no show-level
`viewCount`, only `viewedLeafCount` and `leafCount` — so every yes/no answer is a threshold over
those two numbers, and **which threshold depends on where `recommendations.watched_pct` sits**:

| Cap                 | What it excludes                                                                                                                                                | A show they're 2 episodes into |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `0.0` (the default) | Everything they've **touched** — any show with at least one watched episode. This is Plex's own answer; `unwatched=0` returns a series from its first episode.  | Excluded                       |
| above `0.0`         | A ceiling on **finished** titles: at most that share of the row may be things they've completed. Unwatched titles still come first — it permits, never prefers. | Allowed (it isn't finished)    |

"Finished", used by the cap only, is `viewedLeafCount` against 80% of the episodes with a
length-scaled floor (`max(3, 15%)`), so a long-running series someone is genuinely deep into isn't
treated as fresh while three episodes of a 200-episode run isn't treated as finished.

> **Changed in 1.2.** A 0% row used to exclude only _finished_ shows, so one you were two episodes
> into was, to the row, a fresh discovery and could be recommended straight back. A live probe of a
> real server found `?type=2&unwatched=0` returning shows as little as 1.1% watched (2 of 176), and
> five of ten started shows there were still eligible. At 0%, started now means watched. Rows above
> 0% are unchanged. If you relied on the old behaviour, set the cap above 0.

A row that should LEAD with rewatches needs the per-row `rewatch` flag instead. `unstarted_only`
still exists and still matters — but only on a row whose cap is **above** 0%, since a 0% row now
drops started series anyway.

### Watched vs finished

The dashboard reports both, and they are different questions. **Watched** is Plex's own flag: a film
played, or a series with at least one finished episode. **Finished** is a film played, or a series
with **every** episode watched.

The gap is not a rounding error. On a real 47-user server, of the 158 show picks credited as watched,
only 21 had actually been finished — 31 were a single episode. A single "watched" count therefore
flatters television structurally, and a TV row will out-score a movie row on it without being any
better. Both numbers are shown side by side so that comparison stops being misleading; lists are
still SORTED by watched, because ranking on finished would bury every TV row under every movie row.

Three thresholds exist in Shortlist and they are deliberately not the same, because they answer
different questions:

| Where                 | Bar for a series                 | Question it answers                             |
| --------------------- | -------------------------------- | ----------------------------------------------- |
| Dashboard `watched`   | 1 episode (Plex's own)           | Did they start it?                              |
| Recommendation engine | `min(80%, max(3, 15%))` episodes | Are they engaged enough not to re-recommend it? |
| Dashboard `finished`  | every episode                    | Did they see it out?                            |

Plex publishes no show-level finished flag, so the last one is Shortlist's own threshold — the
strictest and least arguable of the options, and the same wording a person's page already uses per
title ("3 of 12 episodes" / "finished").

**Backfill.** `picks.finished_at` was added in migration 0072. Films were backfilled exactly, since
a film's watched flag IS completion. Series were deliberately left empty and fill in going forward:
which shows are past the bar today is knowable, but _when_ they crossed it is not, and inventing that
date would file old watches in the wrong week of the trend chart permanently. A series already
credited as watched is picked up on the first sync after it completes.

**Why a watched title can still appear:** the read is per-run, so a title marked watched _after_ the
last run stays eligible until the next run re-reads. Between runs, **Jobs → Sync history**
(`POST /api/report/sync`) re-reads every user's watched set on demand. It writes nothing to Plex,
only refreshes what Shortlist knows, so hit rates and the per-user "N titles watched" count stay
current without waiting for a scheduled run.

## Why you see everyone's rows (and the watching account)

If you own the server, the **Recommended shelf** inside each library shows you every person's row,
not just yours. This is a Plex limitation with no setting behind it: rows are hidden from other
people through the _share_ each of them has with your server, and you have no share with yourself,
so there is nothing for Plex to hide them behind. Your own **Home screen** is unaffected — Plex
tracks "on the owner's Home" separately from "on a friend's Home", so nobody else's row lands there.

**Users → You see everyone's rows** (`/watching-account`) lays out the three ways to deal with it:

1. **Take the rows off the library shelf.** Everyone still gets their row on their Home screen, and
   nobody — including you — sees anyone else's. You lose the row inside Movies and TV Shows. One
   click; it flips `placement_friends` on every per-person row and leaves the Home half alone.
2. **Leave it.** Some owners genuinely don't mind. Dismissing stops Shortlist mentioning it.
3. **Move your watching to a separate account.** Keep the library shelf _and_ stop seeing everyone
   else's rows. You create a Plex Home user (Plex → Settings → Home → Add user), share the same
   libraries with it, and Shortlist copies your watch history across so its picks are right from the
   first run.

The copy reads your account straight from Plex — per episode, including anything you are part-way
through — and writes the same state onto the new account. It does not read Shortlist's cache, so it
works during the setup wizard before anything has been synced.

### What "copies your watch history" means exactly

The new account ends up **matching** yours:

- **Per episode, not per show.** A series you are 400 episodes into arrives 400 episodes in, and no
  further. Marking the show itself would tell Plex "mark every episode", and part-watched series are
  the common case rather than an edge — on a real account, 342 of 535 watched shows.
- **Rewatch counts.** A film you have seen three times arrives at three.
- **Part-watched films and episodes.** They land at the same position and show up in Continue
  Watching.
- **It removes as well as adds.** Anything watched on the target that you have not watched is
  un-marked. That is what makes it a replica rather than a merge — and it is what repairs an account
  an older version over-marked. The web UI previews the removals **by title** and requires them to be
  acknowledged before the real run.
- **It is reversible** — unless that account cannot see all of your libraries. The target's complete
  state is snapshotted before the first write, and **Undo** restores it exactly, counts and positions
  included. If a library is unreadable for that account the snapshot cannot be complete, so the undo
  is refused rather than restoring from a partial picture; the preview says so before you agree.
- **Your own account is never written to.** The copy reads yours and writes only with the target
  account's own token.

Afterwards Shortlist re-reads the target and reports what did not land, rather than reporting the
writes it sent.

### The date problem, and `source_viewed_at`

Plex cannot backdate a watch. Every write — `/:/scrobble`, `/:/progress`, `/:/timeline` — is stamped
**now**, and no endpoint accepts a date. Copying two thousand titles onto a new account therefore
tells Plex they were all watched today, and the next watch sync would write exactly that into
`watched_titles.viewed_at`.

That matters more than it sounds: Shortlist picks seeds from the **most recent** watches, so a set
where every row shares one timestamp orders arbitrarily and the new account's recommendations become
noise. The migration that gave someone their history back would be the one that broke their picks.

Three things keep the dates:

- **`watched_titles.source_viewed_at`** records the true date per cached title. The watch sync never
  overwrites it and never deletes a row carrying one, and every "how recently?" read prefers it.
  `NULL` — the value on every row Plex reported directly — means `viewed_at` is the truth, so nothing
  changes for anyone who never runs a transfer.
- **Your play log is copied** onto the new account's `watch_events` with its original timestamps, per
  episode. Those rows carry `source='transfer'` and are deliberately excluded from pick attribution:
  they are real watches for recommendation purposes, but they are not that person pressing play on a
  Shortlist row. This is the only dated history the account gets — a scrobble writes no entry to
  Plex's own history log.
- **Writes go oldest first.** The dates cannot be replicated, but the ORDER can, and `lastViewedAt`
  order is what Continue Watching and "recently watched" sort on. So the shelves come out right even
  though every date reads as today.

## How a pick is chosen (and why a row can be short)

Every candidate carries an **affinity**: how strongly the source that produced it vouched for it,
0..1:

- **TMDB** sets it from which endpoint suggested the title and how near the top of that list it sat
  (`/recommendations` is worth more than `/similar`, and both decay down their list), multiplied by
  a **genre-coherence** factor: the share of the candidate's own genres that the seed does not have.
  TMDB tags a medical drama simply "Drama" and so is nearly everything it suggests, so overlap alone
  discriminates nothing, but a suggestion also tagged "Sci-Fi & Fantasy" is measurably further away.
- **Sources with no ranking of their own** — `tmdb_discover`, `trakt`, `llm_web` —
  report the neutral `1.0`. That means "no ranking information", not "perfect match"; they are
  deliberate picks rather than the tail of a list, and `pre_rank`'s per-source round-robin is what
  keeps them competing fairly.

Ranking is `(1 + seed_frequency) × rating × (1 + seed_weight) × affinity`, so a well-rated but
distant title no longer beats an obviously similar one.

**A row is allowed to come up short.** Padding a partly-filled row only draws from candidates at or
above `MIN_FILLER_AFFINITY` (0.35). Four genuinely-similar titles beat ten where six are filler.
When that happens the run log says so at INFO, naming the closest rejected title, so a short row
reads as the filter working rather than as a failure.

Each delivered pick records its provenance (`sources`, `affinity`, returned by `GET /api/users` and
the run detail) and the UI shows it under the title, as _"suggested by TMDB · loosely related"_. At
DEBUG the run log prints the same per row: every pick with its seed, source and affinity.

## How rows stay private

Each row is a Plex collection labelled `shortlist_<userslug>`. Every _other_ account's share filter
gets a `label!=shortlist_<userslug>` exclusion (merged into their existing `filterMovies` /
`filterTelevision`, never rebuilt), so only its owner ever sees it. The write ordering is what keeps
this leak-safe: a run delivers rows **unpromoted**, merges all the exclusions, and only **then**
promotes rows onto Home, so a new row is never visible before the exclusion that hides it exists. Rows
Plex cannot hide (wrong media type for their library) are swept away first, before anything else.

Every row also carries a second, constant label — `shortlist` (Plex stores it title-cased, as
`Shortlist`). It hides nothing and excludes nothing; it exists so a co-managing tool can be told to
leave Shortlist's rows alone in **one** entry. Agregarr's _Exclude from Ordering (Plex Label)_ and
Kometa's equivalents take a list of labels, and the per-person ones are no use there: a 46-account
server has 46 of them plus one per shared row, and the list goes stale the moment somebody joins or
leaves. Existing rows pick the label up the next time they are built — there is nothing to run.

Before Shortlist first edits an account's filters it snapshots them (`restriction_snapshots`), so
**Uninstall** restores every share exactly as it found it. The one hard requirement is a Plex Media Server
**≥ 1.43.2.10687** (older builds ignore the label exclusion).

> Earlier versions ran an automatic _Privacy Check_ that verified the hiding before each write and
> refused to write if it couldn't confirm it. That check + its write gate were removed at the
> maintainer's request; the hiding above still happens on every run, but it is no longer verified
> after the fact.
