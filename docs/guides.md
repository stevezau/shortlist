# Guides

## The web interface

- **Dashboard** — privacy badge (last verified), next scheduled run, per-user cards with
  their current row, hit rate, and a Run now button. Live-updates during runs.
- **Users** — enable/disable, pause, a request tag, per-row overrides (size, curation style,
  mute), and each user's restriction status.
- **Runs** — every run with per-user diffs ("added X, removed Y (watched ✓)"), errors as
  first-class rows with copy-for-GitHub buttons, LLM token usage.
- **Settings** — every connection re-testable in place; **Recommendations** (which candidate
  sources to pool); curation style; row defaults; schedule editor (cron for power users); the
  Danger Zone.

## Schedules

Default is nightly at 03:30 server-local. Settings → Schedules takes a full cron expression
(`30 3 * * *`) with a human-readable preview. One schedule covers the whole server; to skip someone,
pause them on their detail page.

## Hit rate

The % of recommended items a user actually watched within 30 days, computed from the same
history source that feeds recommendations. It's Shortlist's own proof of value — visible
globally and per user. Expect ~20-40% on engaged users after a few weeks.

## Recommendation sources

Settings → **Recommendations** controls where candidate titles come from. Shortlist pools every
source you enable, keeps only what's already in your library, then the AI re-ranks. More sources =
wider reach. Available today:

- **TMDB — similar titles**: the baseline — titles TMDB says are similar to what each person watched.
- **TMDB — discover by taste**: widens into popular, well-rated titles in the genres each person
  leans toward (derived from their watch history).
- **AI — suggests from your library** (needs an AI curator): the curator reads each person's taste
  and proposes owned titles that fit, reaching across your whole library rather than just what's
  similar to one seed. Large libraries are sliced to each person's genres before the LLM sees them.
- **Trakt — related titles** (needs a Trakt API key, added in Connections): uses Trakt's
  recommendation graph, which often surfaces "what to watch next" picks TMDB's similar list misses.
- **AI — web search for what to watch next** (needs a curator with web search — Claude or GPT): the
  curator searches the live web for current, well-reviewed titles to watch next, then resolves each
  against your library. Reaches beyond TMDB/Trakt to fresh releases and critics' lists.

Each row also chooses **which libraries** it builds in (the row editor's Libraries picker). A Plex
collection lives in one library, so a row builds one collection per library you tick — leave them all
ticked (the default) to cover every library, or point a row at just one (e.g. "4K Movies") on a
server with several libraries of a type. What the row recommends (movies, shows, or both) follows the
libraries you pick.

### Everything above is only the _default_ — rows override it

Settings → Recommendations sets what a row uses **unless the row says otherwise**. Open any row
(Rows → Edit) and it defines its own recipe:

| In the row editor          | What it overrides                                                      |
| -------------------------- | ---------------------------------------------------------------------- |
| **Recommendation sources** | Switch to "Choose for this row" and tick its own sources               |
| **Curation style**         | Its own tone, guidance, and (optionally) a full custom AI prompt       |
| **Libraries**              | Which Plex libraries it builds in — which also sets what it recommends |
| **Row size**, **Audience** | How many titles, and who gets it                                       |
| **Request tag**            | The Sonarr/Radarr tag on titles requested for this row's audience      |

So a "What to watch next" row can be Trakt-only with a concise tone, a "Hidden gems" row can be
AI-from-library with a cinephile prompt pointed at just your 4K library, and your default
"Picked for You" can stay on the global settings — all on the same server, all at once. The Rows
list shows each row's overrides on its card, so you can see at a glance which rows differ.

A row left on "Use global default" stays in sync with Settings → Recommendations.

**The one exception is the seeded "Picked for You" row**: its **name**, **size** and **curation
style** always follow the global Settings (Defaults and Curation style) so they stay in sync
everywhere — the row editor points you there instead of offering its own. Its sources, libraries and
audience are its own, exactly like any other row.

**Disabling a row removes it.** Turn a row off and its collection is taken off its audience's Plex
Home on the next run — "off" means gone, not just "stops updating". Two things are left in place
(still private — the row's label keeps it excluded from everyone else — just not auto-removed): a row
whose title is built from a person's top pick (its title can't be matched without those picks), and a
row you *delete* outright (Shortlist no longer has the record it needs to find the collection). In
both cases the collection stays until you rebuild the row (re-enable it) or remove it by hand.

## Requests (Radarr / Sonarr)

Off by default. When on, Shortlist notices the titles your people's taste surfaced that your library
doesn't have yet — everything the recommendation sources turned up, not just what made it into a row —
and asks Radarr (movies) or Sonarr (shows) to grab a few of the best ones on each run.

Set it up under **Settings → Requests**:

1. Turn on **Fill in the gaps automatically**.
2. For each app, paste its **address** (e.g. `http://localhost:7878` for Radarr,
   `http://localhost:8989` for Sonarr) and **API key** (found in the app under _Settings →
   General_), then click **Test connection**. Save.
3. Once connected, pick a **Quality** profile and a **Save to** folder from the dropdowns — Shortlist
   reads these straight from the app, so there are no ids to look up.
4. Tune the **Guardrails**: a minimum rating and minimum number of reviews a title must clear, the
   fewest people who must want it, and the most titles to auto-request per night (a hard cap across
   both apps).
5. Set the **Auto-send vs. ask me** bar: titles wanted by enough people _and_ rated highly enough
   are requested automatically each night; everything else that clears the guardrails waits in your
   **Requests** inbox. Turn auto-send off for a fully manual queue.
6. Optionally set a **tag** (default `shortlist`). Every title Shortlist requests gets this tag in
   Radarr/Sonarr — created there if it doesn't exist — so you can filter, find, or hang tag-based
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
titles it didn't auto-send — with the title, year, rating, and how many people wanted it. Tick the
ones you want and click **Send to Sonarr/Radarr**, or **Reject** the rest. A rejected title is never
re-queued AND never auto-sent by a later run — a "no" is a no. A title already in the library stops
appearing on its own, and one that's already been sent (still downloading, say) never re-consumes an
auto-request slot, so a slow grab can't starve the queue. Sent and dismissed titles
move to **Already handled** so you can see what you've actioned.

It stays cautious on purpose. Missing titles are deduplicated across all your users — three people
wanting the same one is a single entry, and multi-person demand ranks it higher and can push it over
the auto-send bar. A title already in Radarr/Sonarr is skipped, never re-added, and a dry-run only
logs what it _would_ ask for. Every request (and every skip) is recorded in the audit feed, and the
run's detail page shows how many titles it requested.

Requires Radarr v3+ / Sonarr v4+ reachable from the Shortlist container.

## The CLI

The same engine, no web UI — useful for cron-driven setups and CI smoke tests:

```bash
shortlist --config-dir /config run --dry-run   # log every would-be change, write nothing
shortlist --config-dir /config run             # the nightly pipeline
shortlist --config-dir /config verify          # T1 read-back + T2 canary view
shortlist --config-dir /config verify --probe  # full probe (throwaway collection, ~90s)
shortlist --config-dir /config uninstall       # restore snapshots, delete rowarr collections
```

`run` refuses real writes unless a passing `verify` is on record from the last 7 days —
that's deliberate.

## Troubleshooting

- **A user says they can see someone else's row** — run `verify` immediately; if T1 fails it
  names the user and the missing exclusion. Re-running `run` re-merges filters. Check
  whether the share was edited by hand in plex.tv (Shortlist re-merges but never deletes
  foreign filter conditions).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **Tautulli shows fewer watches than expected** — Tautulli only knows sessions it observed
  live. Shortlist automatically falls back to Plex's own history per user when Tautulli's
  answer is thin.
- **Everything broke, get me out** — `shortlist uninstall` (or Settings → Danger Zone →
  Uninstall) restores every user's share filters from the pre-Shortlist snapshots and deletes
  every rowarr-labeled collection. Kometa and other tools' collections are never touched.
