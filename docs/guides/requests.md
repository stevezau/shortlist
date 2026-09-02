---
title: "Requests: Radarr and Sonarr"
description: Let Shortlist ask Radarr or Sonarr for titles your people want that the library doesn't have yet, with an approval inbox and guardrails.
heading: Requests (Radarr and Sonarr)
nav_order: 6
---

## Requests (Radarr / Sonarr, or Overseerr)

Off by default. When on, Shortlist notices the titles your people's taste surfaced that your library
doesn't have yet. That means everything the recommendation sources turned up, not just what made it
into a row. It then asks for a few of the best ones on each run.

You choose **where requests go**, under Settings → Requests:

- **Radarr & Sonarr** (the default) — Shortlist adds the title itself, using a quality profile and
  folder you pick here.
- **Overseerr / Jellyseerr** — Shortlist files a request instead, and Overseerr fetches it using its
  own quality settings, folder rules and approvals. See
  [Requesting through Overseerr](#requesting-through-overseerr) below.

Set it up under **Settings → Requests**:

1. Turn on **Fill in the gaps automatically**.
2. For each app, paste its **address** (e.g. `http://localhost:7878` for Radarr,
   `http://localhost:8989` for Sonarr) and **API key** (found in the app under _Settings →
   General_), then click **Test connection**. Save.
3. Once connected, pick a **Quality** profile and a **Save to** folder from the dropdowns. Shortlist
   reads these straight from the app, so there are no ids to look up. For Sonarr, also pick **how
   much of a show to grab** — these are Sonarr's own Add Series _Monitor_ choices, so they mean
   exactly what they mean there. (Sonarr's other monitor options aren't offered: on a show your
   server doesn't have yet, _Future_, _Existing_ and _Recent_ each monitor nothing at all, which
   **None** already says plainly.) **All Episodes** (the default) takes the whole back catalogue, which
   on a twelve-season show is twelve seasons of downloads the night it is added; **First Season**
   makes it a taster you can extend in Sonarr later; **None** files the show unmonitored so nothing
   downloads until you say so. Anything other than All Episodes also turns Sonarr's **Monitor New
   Seasons** off for that show, so a restriction on a still-running series holds when the next season
   airs instead of quietly growing back. It applies only to shows Shortlist adds — one Sonarr already tracks is
   left exactly as you have it.
4. Choose **Send on its own, or ask me first**: titles wanted by enough people _and_ rated highly
   enough go out as soon as a run finds them; everything else that clears the guardrails waits in
   your **Requests** inbox. Turn it off for a fully manual queue. While it's on, also set **the most
   to send automatically in one run**, a hard cap across both apps, so a single run can't flood
   your downloads. Titles you approve by hand in the inbox aren't capped.
5. Tune the **Guardrails**, the lowest bar a title must clear before Shortlist will ask for it at
   all, whether it goes out on its own or waits for you. Pick a **rating source**: TMDB (no extra
   setup), or IMDb / Rotten Tomatoes / Metacritic / Trakt (these read scores from **MDBList**, so
   add a free MDBList API key under Settings → Connections first). Then set a minimum rating and
   minimum number of votes a title must clear, the fewest people who must want it, and an optional
   **release-year window** (_on or after_ and _on or before_; leave either blank for no bound; a show
   is judged by its first-air year).
6. Optionally set a **tag** (default `shortlist`). Every title Shortlist requests gets this tag in
   Radarr or Sonarr, created there if it doesn't exist, so you can filter, find, or hang tag-based
   rules (quality/release/cleanup) on exactly what Shortlist added. Leave blank for no tag.

Tags come in three layers, and a requested title carries the union of all that apply:

- **Global** (above) — added to everything Shortlist requests.
- **Per person** — on a user's detail page, a **Request tag** field tags titles requested because
  that person wanted them (e.g. `sarah`), so you can route their picks to their own folder or rules.
- **Per row** — in a per-person row's editor, a **Request tag** field tags titles requested for
  anyone in that row's audience (e.g. `picked-for-family`). Shared "popular on this server" rows
  don't request missing titles, so they have no request tag.

A title three people want ends up with the global tag plus each of those people's tags and the tags
of every per-person row they're in. Missing tags are created in Radarr/Sonarr on first use, exactly
like the global one.

### Requesting through Overseerr

If you already run **Overseerr** or **Jellyseerr**, pointing Shortlist at it instead of at Radarr and
Sonarr means what Shortlist asks for shows up alongside everything your users request, and gets the
quality profile, folder and 4K routing you already configured there. Both products share one API, so
one setting covers both.

1. In Settings → **Connections**, fill in the **Overseerr / Jellyseerr** card with its address (e.g.
   `http://localhost:5055`) and an **API key** (in Overseerr under _Settings → General_), and press
   **Test**.
2. In Settings → **Requests**, set **Where requests go** to **Overseerr / Jellyseerr**.
3. Pick **Request as**.

That third choice is the one worth thinking about. Your API key belongs to an admin, and admins
normally auto-approve their own requests — so leaving it on **Server default** means Shortlist's
picks go straight through to Radarr/Sonarr without anyone looking at them in Overseerr. That is fine
if you want Shortlist's own guardrails and inbox to be the only gate.

If you'd rather see them first, make a user in Overseerr called **Shortlist** with auto-approve
turned off, and pick it here. Its requests then wait in Overseerr for your yes, clearly labelled as
coming from Shortlist rather than from a person. Shortlist never creates that account for you — it
only lists the accounts already there.

Attributing requests to the person whose taste surfaced a title is deliberately not offered. They
never asked for it, so it would spend their request quota and notify them about something they had
no part in.

**Two things work differently on this route:**

- **Tags don't travel.** Overseerr's request API has no tags field, so the global **Tag added
  items** setting and the per-person tags have nothing to attach to — those controls disappear from
  the screen when you switch. The "Request as" account is the attribution instead.
- **There's no blocklist.** Radarr and Sonarr keep an import-exclusion list ("never fetch this");
  Overseerr has no equivalent, so a title you don't want has to be turned down in Shortlist's own
  Requests inbox. A rejected title is never asked for again, so one **No** is enough.

Everything else is unchanged: the same guardrails, the same auto-send bar, the same inbox. The only
difference is who does the fetching.

### The Requests inbox

The **Requests** tab (in the sidebar) is your approval queue. Each run adds the wanted-but-missing
titles it didn't auto-send, with its poster, title, year, rating, TMDB's synopsis, and a full **why it's here**
breakdown: one line per person and row that wanted it, with the reason (e.g. "Sarah · Comedy Classics · because
they watched Fawlty Towers"). That answers where a request came from and why, not just a count.
The synopsis is there so a title you've never heard of can be judged without opening a tab for it;
titles queued before Shortlist stored synopses show none until the next run re-surfaces them.
A long queue can be narrowed by a minimum rating and vote count (and to movies or shows) and
sorted by **Newest**, **Top rated**, or **Most wanted**, so the best picks triage first.
Posters come straight from TMDB's image CDN (`image.tmdb.org`), the only third-party asset Shortlist's
web UI fetches. An install behind a restrictive network, or a browser with an ad-blocker, will show a
placeholder tile instead; so will a title TMDB has no artwork for, and one queued before posters existed
(those fill in on the next run that re-surfaces the title). Nothing else on the page depends on it.

Every title carries its own **Send**, **Delete** and **Reject** buttons, so you can work straight down
the list deciding one at a time. For a batch, tick the ones you want instead and use the same three
buttons on the toolbar above the queue — they act on everything ticked. The two ways don't interfere:
deciding a single title from its own row leaves a selection you're part-way through assembling alone.

For anything you're not sending you have two choices, and the difference is exactly what happens on
the next run:

- **Reject** — a permanent "no". The title is never re-queued AND never auto-sent by a later run. It
  moves to the **Rejected** tab as a record. Changed your mind? **Allow again** (or **Allow all
  again**) on that tab moves it straight back to Waiting immediately, with its who-wanted-it detail
  intact and ready to send. No waiting for a run.
- **Delete** — a "not right now". The title is removed from the list with no block, so if your people's
  taste turns it up again on a later run, it comes back to Waiting. Use it to clear clutter without
  slamming the door.

Both carry a hover hint wherever they appear, and an always-visible line under the queue spells out the difference.
A title already in the library stops appearing on its own, and one that's already been sent (still
downloading, say) never re-consumes an auto-request slot, so a slow grab can't starve the queue.
Everything sent moves to the **Sent to Radarr & Sonarr** log, each entry keeping when
it went, the app's answer (e.g. "added to Radarr"), and the same why-it-was-wanted breakdown. Each
sent entry links straight to the title's page in Radarr or Sonarr, and a **Clear** button tidies items
out of the log once you're done with them. Clear only hides the entry (the title stays in
Radarr or Sonarr and is never re-requested), it never un-sends.

It is cautious by design. Missing titles are deduplicated across all your users. Three people
wanting the same one is a single entry, and multi-person demand ranks it higher and can push it over
the auto-send bar. A title already in Radarr/Sonarr is skipped, never re-added, and a dry-run only
logs what it _would_ ask for. Every request (and every skip) is recorded in the audit feed, and the
run's detail page shows how many titles it requested.

Requires Radarr v3+ / Sonarr v4+ reachable from the Shortlist container.

### Why is a title still waiting?

The bar for sending on its own is higher than the bar for being requestable at all: a title is sent
without asking only if it clears **both** `requests.auto_min_demand` (default 3 distinct people, counted **within one row**) and
`requests.auto_min_rating` (default 8.0). A 7.9 wanted by twenty people still waits. Beyond that:

- **On an exclusion list** — a past delete in Radarr/Sonarr leaves the title on an import-exclusion
  list, and Shortlist will never auto-send one (the app would refuse the add anyway). The card says
  so; clear it in Radarr/Sonarr first, then approve.
- **It's in another language** — if you've set a language preference (below), a title outside your
  languages has a higher bar to clear before it is sent on its own. Below that bar it waits here
  rather than being dropped, so you can still approve it. The card shows the language as a chip.
- **Over the per-run cap** — `requests.max_per_run` auto-worthy titles go per run; the rest wait.
- **The run never rated it** — when `requests.rating_source` is not `tmdb`, a run only rates as many
  titles as its lookup budget allows (see below).
- **Already in Radarr/Sonarr** — the card shows a **Downloaded / Downloading / Searching / Not
  monitored** badge if either app already tracks it, which normally means it was added by hand after
  it landed here. **Not monitored** is also what a show added under **None** reads as, which is that
  setting working as asked rather than a problem to fix. Films drop off the list on the next run. **Shows only drop off on Sonarr v4**,
  because matching them back to the request needs Sonarr's own TMDB id, which v3 doesn't report. On v3
  the badge appears but the entry stays until you clear it yourself.

### Nothing is being requested at all

If runs keep finishing with **0 requested** and the inbox stays empty, the rating gate is rejecting
everything it managed to rate. The run's stats carry the three numbers that tell you which:

- `requests_pool` — titles that cleared the base floors (`min_demand`, the year window). If this is
  **0**, those floors are the problem, not the ratings: `requests.min_year` and `requests.min_demand`
  are the ones to loosen.
- `requests_examined` — how many of that pool the run actually rated.
- `requests_lookups` — how many of those cost an MDBList API call. Cached ratings are free.

When `examined` is well below `pool`, the run ran out of lookup budget before it reached anything
good. That is the case to act on, and it is what `requests.none_qualified` in the event log means.
Raise `requests.max_per_run` (the budget is 4x it, floor 20) so each run rates more, or lower
`requests.min_rating`.

Why the two can disagree so sharply: the run rates titles in **demand** order — most-wanted first —
but judges them on **rating**. On a large library the most-wanted _missing_ titles are often the ones
nobody thought worth adding, so the top of the list can be the worst-rated part of it, and the titles
that would pass sit further down. A bigger budget reaches them.

## Too many subtitles

The request pool is, by definition, **what your library doesn't have**. If your library already holds
the popular English titles, what's left missing skews non-English before any setting is applied — and
the rating floor then favours it further, because TMDB's audience rates anime and K-drama generously.
The result is a nightly run that mostly asks for subtitled titles.

**Settings → Requests → Guardrails → Language** fixes it without throwing the good ones away:

- **Any language** — one bar for everything. This is the default and how Shortlist has always
  behaved; nothing changes until you pick something else.
- **Prefer these** — titles in your languages keep the normal bars. Anything else has to be rated
  higher to be sent on its own. Below that bar it waits in your inbox with the reason on it, so a
  Korean thriller you'd have wanted is still one click away rather than gone.
- **Only these** — never ask for another language at all. These are dropped rather than queued: if
  you've said never, being asked about them nightly isn't an answer.

The second bar has **no fixed default**. It follows your own minimum rating plus 1.5 and keeps
following it — so a permissive 6.0 server starts at 7.5 and a strict 8.0 server at 9.5. Type a number
to pin it; "Follow my minimum rating again" puts it back.

Two things worth knowing before you choose a number:

- **8.5 on TMDB is a soft bar for anime.** TMDB's audience rates it generously, and plenty sits above
  8.5 there. If you want the bar to actually bite, switch **Judge titles by** to **IMDb** first — its
  scale is harsher and the gate already supports it.
- **You'll get fewer requests, not automatically more English ones.** Cutting the mid-tier foreign
  titles frees the slots they were taking, and the English titles below them move up into those slots
  by the ordinary ranking. But if nothing English is left above your minimum rating, the run simply
  sends fewer titles rather than reaching down.

The ranking itself is untouched: a foreign title that clears the higher bar competes on merit and
usually wins, because it out-rates the English titles around it. That is the point — this thins the
middle, it doesn't exclude a language.

A title Shortlist can't identify a language for — only a non-TMDB source like Trakt produces one —
counts as preferred, so turning this on never silently stops a source you've enabled from working.

## Different settings per row

Everything above is the server-wide default. Any per-person row can override most of it in the row
editor, under **Requests** — a kids row can file into its own folder at a lower quality profile, take
only the first season of a show, stay English-only, ask for a lower rating, and hold itself to one
title a night, while your main row carries on as it was.

A field left on "use the setting from Settings › Requests" follows the global, and follows it as you
change it. Only the ones you deliberately override differ.

Two things stay server-wide on purpose:

- **How many a run may request.** This is what stops a library ballooning, so a row can only ever ask
  for _less_ of it, never more.
- **The rating source and its MDBList key.** One account, one place to set it.

### How rows share the limit

Rows split the run's limit evenly, and any row that can't fill its share hands it back to the rows
that can. With the limit at 10 and two rows:

```
Row A capped at 3, Row B uncapped
  even split -> 5 each
  A takes 3 (its own limit)
  A's spare 2 goes to B -> B takes 7
                           -------
                           10 total
```

A run that builds one row is simply that row on its own, so a row capped at 3 asks for 3.

Rows on the **same schedule build together as one run** and share one limit. Rows on different
schedules are different runs, each with the full limit — so three rows on three different times can
ask for three times as much in a day as the same three rows on one schedule.

### When two rows want the same title

It's requested once, by the first row in your row order whose settings it passes — so it lands in
that row's folder, and the other row's slot frees up for its next pick. Ten slots always mean ten
titles. The Requests inbox shows every row that wanted it, not just the one that asked.

### Shared rows

A shared row ("Popular on your server") has no request settings, and the editor doesn't show the
section for one. It's built from titles people have already watched, which are by definition already
on your server — so there is never anything missing for it to ask for.
