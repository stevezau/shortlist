# Guides

## The web interface

- **Dashboard** — next scheduled run, per-user cards with their current row, hit rate, and a
  Run now button. Live-updates during runs.
- **Rows** — create, edit, and reorder your rows. Each card shows who sees it and how it
  differs from the defaults (sources, libraries, curation style, placement). This is where
  the whole multi-row feature lives — see "Naming a row" and "Row placement" below.
- **Users** — enable/disable each person or **Enable all / Disable all** at once, pause
  someone (keeps their row, skips them on runs), set a request tag, add per-row overrides
  (size, curation style, mute), and see each user's restriction status.
- **Runs** — a live **Activity** log streams each user through history → candidates → curating →
  delivering as the run happens (seeded from the server so a reload replays it); per-user diffs
  grouped by row then library ("added X to Movies, Y to TV Shows"), each library showing its own
  ranked picks; errors as first-class rows with copy-for-GitHub buttons, LLM token usage.
- **Requests** — the approval inbox for titles your picks wanted but the library doesn't have
  yet. Approve to send to Radarr/Sonarr, or reject so they never come back (see "Requests" below).
- **Settings** — every connection re-testable in place; **Recommendations** (which candidate
  sources to pool); curation style; row defaults; schedule editor (cron for power users); the
  Danger Zone.

## Schedules

Default is nightly at 03:30 server-local. Settings → Schedule offers **Nightly** and **Weekly**
presets (just pick a run time), or switch to **Custom (cron)** to enter any 5-field cron expression
— e.g. `0 */6 * * *` for every six hours, or `0 4 * * 1` for Mondays at 4am. One schedule covers
the whole server; to skip someone, pause them on their detail page.

## Naming a row

A row's name can be plain text ("Hidden Gems") or use a placeholder that fills in per person when
the row is built:

- `{user}` — the person's name. `{user}'s picks` becomes "Sarah's picks".
- `{top_seed}` — the title that most drove their recommendations. `Because you watched {top_seed}`
  becomes "Because you watched The Bear".

If a `{top_seed}` row is built for someone with too little history to have a favourite, it falls
back to a clean default ("✨ Picked for You") rather than a half-finished sentence. You can rename
any row at any time in the **Row editor** — the collection on Plex is renamed in place, so its
place in the shelf and its privacy are preserved.

## Row placement (Recommended shelf)

By default Plex adds new collections at the **end** of a library's _Recommended_ shelf, so if another
tool (like **Kometa**) manages collections on the same server, Shortlist's rows can end up buried at
the bottom. Settings → **Row placement** sets a server-wide default; you get three choices per library:

- **Wherever Plex puts them** — leave the order alone (the default).
- **Top of the shelf** — put Shortlist's rows at the very top. No anchor needed. (This replaces the
  old "pin to top" switch.)
- **Right before / after a collection** — pick an existing collection and sit the rows next to it.

Any individual row can override the default in the **Row editor** ("Position in the Recommended
shelf"), per library — so "Picked for You" can sit at the top while another row sits right after New
Series. Since each person only sees their own row, moving rows up lifts everyone's at once.

Behind the scenes Shortlist re-applies your choice at the end of every run (so a co-managing tool
can't re-bury the rows), only ever moves its own rows, and never touches the collection you anchored
to. It works with or without Kometa — Kometa is only _why_ this matters (it fills the shelf), not
_how_ it works; the anchor can be any collection, Kometa's or one of Plex's own.

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
- **AI — web search for what to watch next**: searches the live web for current, well-reviewed titles
  to watch next, then resolves each against your library — reaching beyond TMDB/Trakt to fresh releases
  and critics' lists. Works on **every** provider, via the **Search backend** you pick in its card:
  your curator's own web search (Claude, GPT, or Gemini), an **Exa** key (any provider — the only path
  for a local Ollama model), or **Auto** (the default), which uses your curator's tool _and_ Exa
  together when both are set up, since they surface mostly different titles. If a backend needs a key
  you don't have yet, the card lets you enter it right there.

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

**Changes clean up Plex right away.** You don't have to wait for a run:

- **Delete a row** → its collections are removed from Plex immediately, for everyone who had it
  (including rows whose title is built from a person's top pick — Shortlist finds them by the exact
  title the last run delivered). The titles stay in your library; only the row goes.
- **Rename a row** → its collection is retitled in place for every user, so nothing is orphaned.
- **Disable a user, or drop someone from a row's audience** → that person's now-stale collections are
  removed immediately.
- **Remove from Plex** (the button on each row) → clears a row's collections on demand, without
  deleting the row's settings — handy to force a rebuild on the next run.
- **Disable a row** (its on/off switch) → its collection comes off Plex Home on the next run. A row
  whose title is dynamic (built from a top pick) is left for that rebuild; use **Remove from Plex** if
  you want it gone right now. Everything left in place stays private — the row's label keeps it
  excluded from everyone else.

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
   fewest people who must want it, an optional **release-year window** (_on or after_ / _on or
   before_ — leave either blank for no bound; a show is judged by its first-air year), and the most
   titles to auto-request per night (a hard cap across both apps).
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
titles it didn't auto-send — with the title, year, rating, and **who wanted it** (the names of the
people whose picks surfaced it, e.g. "Wanted by Sarah, Mike"; older entries just show the count).
Tick the ones you want and click **Send to Sonarr/Radarr**, or **Reject** the rest. A rejected title is never
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

## Troubleshooting

- **A user says they can see someone else's row** — run Shortlist again (Run now): every run
  re-merges the `label!=` exclusions into each account's share filters. Check whether the share
  was edited by hand in plex.tv (Shortlist re-merges but never deletes filter conditions it
  didn't add), and confirm the PMS is ≥ 1.43.2.10687 (older builds ignore the exclusion).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **Tautulli shows fewer watches than expected** — Tautulli only knows sessions it observed
  live. Shortlist automatically falls back to Plex's own history per user when Tautulli's
  answer is thin.
- **Everything broke, get me out** — Settings → Danger Zone → **Uninstall** restores every
  user's share filters from the pre-Shortlist snapshots and deletes every shortlist-labeled
  collection. Kometa and other tools' collections are never touched.
- **Did anything drift out of sync?** — Settings → Danger Zone → **What Shortlist has on your
  Plex** ("Check Plex") lists every shortlist-labeled collection read straight from the server (not
  the database), flagging any whose user/row no longer exists in the app. Every collection is
  labeled at creation (atomically — a collection that can't be labeled is deleted rather than left
  as an orphan), so a cleanup always finds them all; this is how you confirm it.
