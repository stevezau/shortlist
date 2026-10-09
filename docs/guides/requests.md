---
title: "Requests: Radarr and Sonarr for Plex recommendations"
description: Let Shortlist ask Radarr or Sonarr for titles your people want that the library doesn't have yet, with an approval inbox and guardrails.
heading: Requests (Radarr and Sonarr)
updated: 2026-10-09
---

## Reviewing the inbox

The Waiting, Sent and Rejected views keep their existing filters and actions. Waiting cards show
the title, rating, demand and current status; open **Details & title links** for the synopsis,
recommendation reasons and TMDB, IMDb and Trakt links. Opening details or an external link does not
select the title. Batch actions appear once you select titles.

**Delete** removes a waiting title for now, so a later run may suggest it again. **Reject** blocks
future requests for it until you choose **Allow again**. Clearing a sent log entry only clears the
record in Shortlist; it does not remove the title from the connected app.

## Requests (Radarr / Sonarr, or Overseerr)

Off by default. When on, Shortlist notices the titles your people's taste surfaced that your library
doesn't have yet. That means everything the recommendation sources turned up, not just what made it
into a row. It then asks for a few of the best ones on each run.

You choose **where requests go**, under Settings → Defaults → Requests:

- **Radarr & Sonarr** (the default) — Shortlist adds the title itself, using a quality profile and
  folder you pick here.
- **Overseerr / Jellyseerr** — Shortlist files a request instead, and Overseerr fetches it using its
  own quality settings, folder rules and approvals. See
  [Requesting through Overseerr](#requesting-through-overseerr) below.

Set it up under **Settings → Defaults → Requests**:

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

If you already run **Overseerr**, **Jellyseerr** or **Seerr**, pointing Shortlist at it instead of at
Radarr and Sonarr means what Shortlist asks for shows up alongside everything your users request, and
gets the quality profile, folder and 4K routing you already configured there. All three share one
API (`/api/v1`), so one setting covers them all — verified against Seerr 3.4.1.

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
only lists the accounts already there. On many servers every existing account can already
auto-approve, in which case making one is the only way to get a queue in Overseerr at all.

The picker lists every account on the instance, with accounts made for this first and people on your
server after them, and tells you which ones auto-approve.

**Picking a person has a cost, and the screen says so when you do.** A title Shortlist wants is
usually wanted by several people at once, while an Overseerr request has exactly one requester — so
choosing a person does not file each title under whoever wanted it. It puts that one name on
_everything_, spends their request quota and notifies them each time. A local account avoids all
three, which is why it is the recommendation — but on many servers a person is the only account that
does not auto-approve, so the choice stays yours.

**Two things work differently on this route:**

- **Tags don't travel.** Overseerr's request API has no tags field, so the global **Tag added
  items** setting and the per-person tags have nothing to attach to — those controls disappear from
  the screen when you switch. The "Request as" account is the attribution instead.
- **The blocklist is read, but only on the newer builds.** Overseerr, Jellyseerr and Seerr keep a
  blocklist ("never fetch this"), and Shortlist reads it: a blocklisted title is never sent on its
  own, and lands in your Requests inbox flagged with why. Older builds serve no blocklist endpoint,
  and there Shortlist simply applies none — so turn those titles down in the Requests inbox instead.
  Either way a rejected title is never asked for again, so one **No** is enough.

Everything else is unchanged: the same guardrails, the same auto-send bar, the same inbox. The only
difference is who does the fetching.

### A row of what they asked for

The other direction is a row: a **Your requests** row kind puts what each person asked for in
Overseerr — once it's on Plex and until they've watched it — in a private row of their own, newest
arrival first. It reads Overseerr, Radarr and Sonarr whenever their address and key are set on this
Connections screen, whether or not Shortlist sends requests of its own, so it works on a server where
people request and Shortlist never does. How the row is set up, matched to people and emptied is in
[Your requests rows](#your-requests-rows). Three things to know from this side:

- **Turn on Tag Requests in Overseerr** (_Settings → Services_, on each Radarr and Sonarr server) so
  each title it sends carries a tag naming who asked for it. Only requests made after that switch is on
  carry one; earlier requests are still covered by Overseerr's own list while they remain in it.
- **Deleting a filled request in Overseerr is fine.** The tag stays on the title in Radarr/Sonarr, and
  Overseerr keeps the date it arrived, so the row is unchanged.
- **Shortlist's own requests never count.** A title carrying Shortlist's request tag, or filed by the
  **Request as** account above, is not treated as anyone's request.

### The Requests inbox

The **Requests** tab (in the sidebar) is your approval queue. Each run adds the wanted-but-missing
titles it didn't auto-send, with its poster, title, year, rating, TMDB's synopsis, and a full **why it's here**
breakdown: one line per person and row that wanted it, with the reason (e.g. "Sarah · Comedy Classics · because
they watched Fawlty Towers"). That answers where a request came from and why, not just a count.
The synopsis is there so a title you've never heard of can be judged without opening a tab for it;
titles queued before Shortlist stored synopses show none until the next run re-surfaces them.
Above the queue, **Waiting**, **Sent** and **Rejected** are tabs, and one toolbar narrows the list:
movies or shows, a search that finds a title by name or by who wanted it (pick a name from its list to
see every title of theirs on file), **Filters** for a minimum rating, a minimum vote count and a
title's original language, and a sort by **Recent**, **Top rated** or **Most wanted**. The language
choice lists only the languages on the tab you're on; titles with no language on record are under
**Unknown**, never under a named language. Whatever is narrowing the list, the search text included,
shows as a removable chip beside **Clear filters**, and switching tabs clears it all.
Posters come straight from TMDB's image CDN (`image.tmdb.org`), the only third-party asset Shortlist's
web UI fetches. An install behind a restrictive network, or a browser with an ad-blocker, will show a
placeholder tile instead; so will a title TMDB has no artwork for, and one queued before posters existed
(those fill in on the next run that re-surfaces the title). Nothing else on the page depends on it.

Every waiting title carries visible **Send**, **Delete** and **Reject** buttons, so you can
work straight down the list deciding one at a time. For a batch, tick the ones you want instead and use the same three
buttons on the action bar above the queue — they act on everything ticked, and **Clear selection**
unticks them. The two ways don't interfere:
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

Both carry a hover hint wherever they appear, and a short line on the action bar spells out the difference.
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

The bar for sending on its own is higher than the bar for being requestable at all. Under
**Settings → Defaults → Requests → Send the strongest titles without asking**, a title has to clear **both**
bars: **Send without asking when wanted by** (3 people by default, counted **within one row**) and
**Send without asking when rated** (8.0 by default). A 7.9 wanted by twenty people still waits.
Beyond that:

- **On an exclusion list** — a past delete in Radarr/Sonarr leaves the title on an import-exclusion
  list, and Shortlist will never auto-send one (the app would refuse the add anyway). The card says
  so; clear it in Radarr/Sonarr first, then approve.
- **It's in another language** — if you've set a language preference (below), a title outside your
  languages has a higher bar to clear before it is sent on its own. Below that bar it waits here
  rather than being dropped, so you can still approve it. The card shows the language as a chip.
- **Over the per-run cap** — **Most to send automatically in one run** caps how many go out at
  once. The rest wait.
- **The run never rated it** — when **Judge titles by** is set to anything other than TMDB, a run
  only rates as many titles as its rating-lookup budget allows (see below).
- **Already in Radarr/Sonarr** — the card shows a **Downloaded / Downloading / Searching / Not
  monitored** badge if either app already tracks it, which normally means it was added by hand after
  it landed here. **Not monitored** is also what a show added under **None** reads as, which is that
  setting working as asked rather than a problem to fix. Films drop off the list on the next run. **Shows only drop off on Sonarr v4**,
  because matching them back to the request needs Sonarr's own TMDB id, which v3 doesn't report. On v3
  the badge appears but the entry stays until you clear it yourself.

### Nothing is being requested at all

First, check whether the runs you are looking at covered **everybody**. **Wanted by at least** counts
_different people_, so a run over one person can never produce a title wanted by two — nothing
qualifies whatever your settings say. That is a fact about the run's scope rather than about your
settings, so Shortlist doesn't count it against you: a run smaller than its own **Wanted by at
least** never raises the **Nothing is being requested** notification. Judge the settings on a nightly
run over everyone.

If full runs keep finishing with **0 requested** and the inbox stays empty, open the run and read the
**Requested** tile. Its second line says which of four things happened:

- **"nothing new was missing"** — your library already has everything anyone was matched with, or
  every missing title has already been sent or rejected. Nothing to fix.
- **"nothing cleared the demand or year limits"** — no title even got as far as being rated.
  **Wanted by at least** and **Released on or after** are the two to loosen, in Guardrails.
- **"rated 80 of 400 — none good enough"** — the run ran out of rating lookups before it reached
  anything worth sending. This is the one to act on.
- **"rated all 400 — none cleared the rating limit"** — it rated everything and nothing was good
  enough. The minimum-rating guardrail is the setting, named after whichever source you chose —
  **Minimum IMDb rating**, say.

The "ran out of lookups" case has a fix. Raise **Most to send automatically in one run** — the run's
rating budget is four times that number, never fewer than 100 lookups — or lower that minimum
rating. The budget only binds when **Judge titles by** is something other than TMDB, because only
those scores cost a lookup; already-known scores are reused free for a week.

Why a run can rate so much less than it wanted to: it rates titles **most-wanted first** but judges
them **on their score**. On a large library the most-wanted _missing_ titles are often the ones
nobody thought worth adding, so the top of the list can be the worst-rated part of it, and the titles
that would pass sit further down. A bigger budget reaches them.

## Keeping concerts and other kinds out of auto-send

Concert films, music documentaries and stand-up specials often carry fan-inflated ratings, so they
can clear the auto-send bar easily. To stop that, open **Settings → Defaults → Requests**, find
**Don't request these automatically**, and pick what to keep out:

- **Genres.** TMDB's movie genres, such as Documentary. Genres are broad: Music also covers musicals
  like _A Star Is Born_.
- **TMDB tags.** The keywords TMDB attaches to films. Search for one, or click a suggestion: _concert
  film_, _live performance_, _music documentary_, _stand-up comedy_, _behind the scenes_. A tag is
  usually more precise than a genre. The broad _concert_ tag, for one, is also on _A Star Is Born_ and
  _Almost Famous_.

A movie with **any** genre or tag you picked is never sent automatically. It waits in the Requests
inbox, marked **Held by your request filter**, and doesn't use an automatic slot. **Send** still
sends it, so making an exception takes one click.

Under your picks, a preview lists which movies waiting in your inbox right now would be held. A
story film in that list is flagged: that usually means a pick is broader than you meant.

This applies to movies only, and only while **Send the strongest titles without asking** is on.
With it off, everything waits for you anyway. If TMDB can't be read during a run, the movie waits
too, and the next run checks it again.

## Too many subtitles

The request pool is, by definition, **what your library doesn't have**. If your library already holds
the popular English titles, what's left missing skews non-English before any setting is applied — and
the rating floor then favours it further, because TMDB's audience rates anime and K-drama generously.
The result is a nightly run that mostly asks for subtitled titles.

**Settings → Defaults → Requests → Guardrails → Language** fixes it without throwing the good ones away:

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

A field left on "use the setting from Settings → Defaults → Requests" follows the global, and follows it as you
change it. Only the ones you deliberately override differ. Every on/off setting in this group,
including that "use the setting from Settings" choice, is a switch — there are no checkboxes here,
only where you're picking items from a list (languages, tags, and the like).

The group only shows a setting the row can actually use: Radarr's root folder and quality profile
appear only when requests go to Radarr and the row includes a movie library (Sonarr's equivalents, and
its "how much of a show to grab" setting, need requests going to Sonarr and a show library); the
request tag and "tag with who it's for" appear only when requests go to Radarr/Sonarr, since
Overseerr/Jellyseerr has no tags field to carry them. If requests are off, the group is replaced by a
note saying so, with a link to Settings.

**How many people must want it** is counted within the row's own audience, so it can only ever be
reached by a row that more than one person gets. Set it above 1 on a row whose audience is a single
person and the editor warns you: any value above 1 there means the row will never ask for anything.

See [Requests on a row](#requests-on-a-row) for these settings from the row editor's own side.

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

## Requests on a row

A per-person row (any kind except Popular on this server, which never requests anything missing — see
[Shared rows](#shared-rows)) can override the server-wide request settings in its own
**Requests** group in the Row editor. Full detail — including how rows share the run's request limit,
and what happens when two rows both want the same title — is in
[Requests → Different settings per row](#different-settings-per-row). Three things worth
knowing about the group in the row editor itself:

- Every on/off setting in it is a switch, the same as everywhere else in the editor.
- A setting the row can't actually use is left off the screen rather than shown disabled: Radarr's
  root folder and quality profile only appear when requests go to Radarr and the row has a movie
  library (the same pairing applies to Sonarr, plus Sonarr's "how much of a show" setting, for a show
  library); the request tag and "tag with who it's for" only appear when requests go to Radarr/Sonarr,
  since Overseerr/Jellyseerr has no tags field for them to reach. If requests are off entirely, the
  group just shows a note saying so, linking to Settings.
- **How many people must want it** only means something when the row reaches more than one person.
  Set it above 1 on a row whose audience is a single person and the editor warns that any value above
  1 means the row will never request anything.

## Your requests rows

A row of what each person asked for, once it is on Plex.

Choose **Your requests** as a row's kind — or start from the _Your requests_ template — and each
person gets a private row of the titles **they** asked for that are now on Plex and they haven't
watched yet, newest arrival first. Nothing is recommended, ranked or padded, and no AI is involved:
the row is exactly what they asked for, or nothing. A title leaves the row once they've watched it,
and a person with nothing ready has no row at all — theirs is taken off Plex rather than left holding
titles they've already seen.

**Where requests are read from.** Two sources, both read whenever their address and key are filled
in under **Settings → Connections** — whether or not Shortlist's own requests are switched on, and
wherever those go:

- **Overseerr's request list** (Jellyseerr and Seerr too). A request counts once it's approved, and
  shows in the row once the title is on Plex.
- **Radarr and Sonarr requester tags.** Overseerr can stamp every title it sends with a tag naming who
  asked for it, like `12-sarah`. Shortlist reads those tags and traces them back to the person through
  Overseerr's user list. The tag stays on the title after the request is gone, so **deleting a filled
  request in Overseerr is fine** — the title stays in the row, and Overseerr still remembers when it
  arrived.

To get those tags, turn on **Tag Requests** in Overseerr under _Settings → Services_, on each Radarr
and Sonarr server it sends to. Only titles requested **after** that switch was turned on carry a tag;
requests made before it are covered by Overseerr's own list for as long as they're still in it. The
editor's **Where requests are read from** panel shows each source's state, whether Tag Requests is on
for each server, how many titles the tags credit to someone, and how many people on your server are
linked to an Overseerr account.

**People are matched by Plex account, and nothing else.** An Overseerr account is linked to a person
when it signed in with the same Plex account; a tag names that account through Overseerr. A tag that
fits nobody, or that two people could both claim, is ignored and listed in the panel, because a wrong
guess would put one person's requests in another person's private row. The **Users** page has a
**Requests** column that says where each person stands: **Linked**, **No account** (nobody in
Overseerr signed in as them), **Can't use Overseerr** (a Home profile can't sign in to Overseerr at
all), or the tag they've been given by hand.

**Use my own tags.** If you tag requests yourself in Radarr/Sonarr rather than through Overseerr,
open **Use my own tags** in the editor and give the row a **tag pattern** such as `req-{username}`:
`{username}` is their Plex username, `{name}` their name in Shortlist. Matching ignores case, and
spaces count as dashes, which is how Radarr and Sonarr store a tag. Press **Check** to see every tag
the pattern (or Overseerr) matched and who it belongs to, before anything is saved. For a tag that
fits no pattern, a person's own page has **Their request tag in Radarr/Sonarr**, which credits that
one tag to them. Overseerr's tags are still read alongside either. Titles Shortlist requested itself —
carrying its own request tag, or filed by the **Request as** account in Overseerr — never count as
anyone's request.

**Titles Shortlist requests for you.** That exclusion needs a **Request as** user. Left at its
default, Shortlist files its Overseerr requests as the API key's own account, usually yours, so the titles
it requests automatically count as that account's requests and appear in its own Your requests row.
Choose a dedicated Overseerr user under **Request as** to keep them apart.

**Which requests show up.** **Show titles that landed in the last** (default 90 days) drops older
arrivals, so a request they've lost interest in doesn't sit there for good; 0 keeps every title until
they've watched it. Beyond that the row uses the same **Libraries**, **Row size** and schedule
settings as any other row. A show counts as ready when the season they asked for has landed (from
Overseerr) or when it has episodes they haven't watched (from a tag), and a title their Plex
restrictions hide from them stays out. A run's **How we picked** page has a **What they asked for**
step listing every request and where it ended up: in the row, not on Plex yet, that season hasn't
landed, already watched, landed more than N days ago, hidden by their Plex restrictions, or past the
row size.

A requests row is private the same way every per-person row is — see
[How rows stay private](../reference/concepts.md#how-rows-stay-private). Because the row disappears
when a person has nothing ready, each run costs one collections listing per library per person with
an empty row, to check there is nothing left to remove. It is only ever removed on a night every
request source was read in full: if Overseerr or an Arr is down, every row stays as it was.
