---
title: "Requests: Radarr and Sonarr"
description: Let Shortlist ask Radarr or Sonarr for titles your people want that the library doesn't have yet, with an approval inbox and guardrails.
heading: Requests (Radarr and Sonarr)
nav_order: 6
---

## Requests (Radarr / Sonarr)

Off by default. When on, Shortlist notices the titles your people's taste surfaced that your library
doesn't have yet. That means everything the recommendation sources turned up, not just what made it
into a row. It then asks Radarr (movies) or Sonarr (shows) to grab a few of the best ones on each run.

Set it up under **Settings → Requests**:

1. Turn on **Fill in the gaps automatically**.
2. For each app, paste its **address** (e.g. `http://localhost:7878` for Radarr,
   `http://localhost:8989` for Sonarr) and **API key** (found in the app under _Settings →
   General_), then click **Test connection**. Save.
3. Once connected, pick a **Quality** profile and a **Save to** folder from the dropdowns. Shortlist
   reads these straight from the app, so there are no ids to look up.
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
without asking only if it clears **both** `requests.auto_min_demand` (default 3 distinct people) and
`requests.auto_min_rating` (default 8.0). A 7.9 wanted by twenty people still waits. Beyond that:

- **On an exclusion list** — a past delete in Radarr/Sonarr leaves the title on an import-exclusion
  list, and Shortlist will never auto-send one (the app would refuse the add anyway). The card says
  so; clear it in Radarr/Sonarr first, then approve.
- **Over the per-run cap** — `requests.max_per_run` auto-worthy titles go per run; the rest wait.
- **Already in Radarr/Sonarr** — the card shows a **Downloaded / Downloading / Searching / Not
  monitored** badge if either app already tracks it, which normally means it was added by hand after
  it landed here. Films drop off the list on the next run. **Shows only drop off on Sonarr v4**,
  because matching them back to the request needs Sonarr's own TMDB id, which v3 doesn't report. On v3
  the badge appears but the entry stays until you clear it yourself.
