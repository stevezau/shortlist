# Guides

## The web interface

- **Dashboard** — the impact report: what Shortlist delivered versus what people actually
  watched (hit rate over time, per user and per row), recent watches, and a **Sync watched now**
  button to refresh those numbers on demand.
- **Rows** — create, edit, and reorder your rows. Each card shows who sees it and how it
  differs from the defaults (sources, libraries, curation style, placement). This is where
  the whole multi-row feature lives — see "Naming a row" and "Row placement" below.
- **Users** — everyone the server is shared with, plus you (badged `owner` — plex.tv's user list
  leaves the owner out, so Shortlist adds you itself). **Sync from Plex** pulls the roster again
  after you invite someone new (or to pick up your own owner row on an install that predates it).
  Enable/disable each person or **Enable all / Disable all** at once, pause someone (keeps their
  row, skips them on runs), set a request tag, add per-row overrides
  (size, curation style, mute), and see each user's restriction status. Opening a person shows
  their recent watch history (distinct titles, with season/episode numbers for TV), their picks
  grouped by row (long lists collapse behind a "show more"), and a **Run now** button to rebuild
  just that person.
- **Runs** — a live **Activity** log streams each user through history → candidates → curating →
  delivering as the run happens (seeded from the server so a reload replays it); per-user diffs
  grouped by row then library ("added X to Movies, Y to TV Shows"), each library showing its own
  ranked picks; errors as first-class rows with copy-for-GitHub buttons, LLM token usage.
- **Logs** — what this instance has been doing, with a level filter (this level _and louder_), a
  text filter, live follow, **Copy**, and **Download .zip** for attaching to a bug report. Tokens,
  API keys and passwords are stripped out server-side before anything reaches the page or the zip,
  so it's safe to share. The file keeps the last 10 × 10 MB and always records at DEBUG, regardless
  of the console level in Settings → Advanced.
- **Requests** — the approval inbox for titles your picks wanted but the library doesn't have
  yet. Approve to send to Radarr/Sonarr, or reject so they never come back (see "Requests" below).
- **Settings** — organised into a grouped sidebar sub-nav so it doesn't read as one long wall:
  **Connect** (Connections), **Rows** (Recommendations, Curation style, Row defaults, Row
  placement), **Add-ons** (Requests), and **System** (Advanced, API access, Danger Zone). Every
  connection is re-testable in place. (Each row's run schedule lives in that row's editor, not
  here — see Schedules below.)

## Schedules

**Every row runs on its own schedule** — there is no single server-wide one. Open a row (Rows → edit)
and set its **Schedule**: **Nightly** or **Weekly** presets (just pick a run time), **Custom (cron)**
for any 5-field expression (e.g. `0 */6 * * *` for every six hours, or `0 4 * * 1` for Mondays at
4am), or **Off** to only run that row by hand. New rows default to nightly at 03:30 server-local;
on upgrade, existing rows keep whatever your old global schedule was. Rows that share a cron run
together. To skip a person entirely, pause them on their detail page.

## Naming a row

A row's name can be plain text ("Hidden Gems") or use a placeholder that fills in per person when
the row is built:

- `{library_name}` — the library the row is built in. `✨ {library_name} Picked for You` becomes
  "✨ Movies Picked for You" in your Movies library and "✨ TV Shows Picked for You" in your TV
  library. This is the default row name, so a server with several libraries gets distinct titles
  instead of two identical "Picked for You" rows.
- `{user}` — the person's name. `{user}'s picks` becomes "Sarah's picks". That name is their
  **nickname** if you've set one (Users → open someone → "What to call them"), otherwise whatever
  Tautulli calls them, otherwise their Plex username — which is often a handle nobody uses. Changing
  a nickname renames their existing rows on Plex; it never changes their label, so their privacy is
  unaffected.
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

## Row posters

Each row can have its own artwork on Plex. In the **Row editor** → **Poster**, pick one of:

- **Plex default** — leave Plex's own collection artwork alone (the default). Switching a row _back_
  to this after it had a custom poster reverts the artwork on Plex on save.
- **Upload** — upload your own image (a tall 2:3 poster looks best; up to 8 MB). It's downscaled and
  stored, then applied to the row's collection(s) on the next run.
- **Text** — a clean built-in poster: your **Title** and **Subtitle** over a gradient. No AI needed,
  works on any setup. Use `{user}`, `{library_name}`, and `{top_seed}` to personalise the text.
- **AI image** — an image generated from your text and **Art style**, using your AI provider's image
  model. This reuses your AI curator's key, so it's available when that provider is **OpenAI** or
  **Google** (Anthropic and local servers can't generate images — use a Text poster or Upload instead).

Hit **Preview** to see a sample before saving. Generated images are made once and reused across
runs (they refresh when you change the text or style), so posters don't slow a run down or cost per
user. Posters are cosmetic — a poster that can't be made never blocks a row from building.

## Hit rate

The % of recommended items a user actually watched within 30 days, computed from the same
history source that feeds recommendations. It's Shortlist's own proof of value — visible
globally and per user. Expect ~20-40% on engaged users after a few weeks.

## Recommendation sources

Settings → **Recommendations** controls where candidate titles come from. Shortlist pools every
source you enable, keeps only what's already in your library, then ranks them (a simple, no-AI
score); if you've set up an AI curator it makes the final pick and writes the "why". More sources =
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

### Freshness, already-watched, and cost

Settings → Recommendations has three more dials (each per-row overridable):

- **Freshness** — how often a row's picks change. This is a **cadence, not a nightly shuffle**:
  `1.0` refreshes every night, lower means every few days, and `0.0` means "build once, then never
  reshuffle". On most nights an unchanged row is left exactly as-is — no re-curation, no Plex write
  — which is why a person's row stays familiar instead of being reshuffled daily. On a refresh night
  the strongest ~two-thirds of picks stay and the weakest third rotates out. Default `0.5`
  (about weekly). If you trigger two runs the same day, a row that already isn't due won't change —
  that's expected.
- **Already-watched titles** — how much of a partly-watched title still counts as "watched" and gets
  filtered out. Default keeps anything finished out of the picks.
- **Recent watches to search** — how many of each person's recent titles the AI web-search source
  looks up (one cached search each). It's the main **cost lever** on that source — lower it to spend
  fewer tokens/Exa searches.

### If a watched title still gets recommended

Almost always one cause: **Plex's history only records what someone actually _played_.** A title
_marked_ watched — ticked off without playing it, or a whole season marked at once — leaves no play
record anywhere in Plex's API, so Shortlist can't see it and may recommend it back. On one real
server that hid **13,201** of a user's watched titles behind the ~1,000 the API reported.

If Shortlist runs on the same machine as Plex, you can close that gap completely. Mount the Plex
database read-only:

```yaml
volumes:
  - /path/to/plex/.../Plug-in Support/Databases:/plexdb:ro
```

and that's it — mounting it at `/plexdb` makes it available. (Settings → Advanced has a path field
if your layout differs, with a **Test** button.) One read covers every account on the server. The
database is opened read-only and Shortlist never writes to it — it only reads which items each
account has watched.

Then go to **Tools → Reconcile watched from Plex** and run it. Reading the database is a manual
action, not part of every nightly run — scanning a live multi-gigabyte database every night, for
marks people add rarely, would be needlessly heavy. The reconcile tells you how many watched titles
it found that the play history had missed. Run it once now, and again whenever watched state drifts;
because Plex's API still can't see marks, anything you mark watched later stays hidden until you
reconcile again.

This is off unless you set it: it needs a deliberate mount, and it can't work if Shortlist runs on a
different machine to Plex.

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

## How Shortlist uses AI (and how to control the cost)

**AI is off by default** (the curator is set to "None" out of the box, and both AI sources are off) —
Shortlist works fully with **no AI at all**. Turning on a curator (Claude, GPT, Gemini, or a local
Ollama model) makes the picks noticeably better. Here's exactly where AI does and doesn't touch your
rows, in plain English, so you can decide what to pay for.

**Building a row happens in four steps:**

1. **Find candidates.** Every source you enabled goes looking for titles. Most of them use **no AI**:
   the two TMDB sources (similar + discover) and Trakt are plain lookups against those services — free,
   no API key beyond the ones you already set up. Two sources _do_ use your AI curator — see below.
2. **Keep only what you own.** Everything found is matched against your actual library and against what
   the person has already watched. Anything you don't have, or they've already seen, is dropped. **This
   is why the AI can never invent a title you don't own** — it only ever gets to choose from real,
   owned, unwatched titles.
3. **Balance the shortlist.** Shortlist takes a fair share from each source (so one chatty source
   can't crowd out the rest) and scores them. **No AI here** — it's a simple ranking.
4. **Curate + explain.** Your AI curator makes the final selection from that balanced shortlist and
   writes the one-line "why" under each pick ("Because you liked Fargo…"). This is the single place AI
   adds the most value, and it's what most people notice.

### The two AI-powered sources

- **AI — web search** (the "AI web search" toggle): searches the live web for acclaimed, current
  "what to watch next" titles, then keeps the ones you own. In our own testing this was the **strongest
  extra source** — it surfaces well-reviewed titles the TMDB lists simply don't return. If you pay for
  any AI source, this is the one to keep.
- **AI — suggests from your library** (the "AI from library" toggle): asks the curator to scan your
  library for owned titles that fit each person. Honest assessment from our testing: it adds the
  **fewest unique picks for the most AI cost** — nearly everything it finds, the other sources already
  found. **It's the first thing to turn off to cut your bill**, and it's off by default.

### If you don't want to use AI

Leave the curator on **None** (Settings → Curation style — this is the default) and the two AI sources off. You still get full,
per-person private rows: candidates come from TMDB/Trakt, and the final selection uses the score
ranking with plain "Because you watched…" reasons instead of AI-written ones. Everything about privacy,
scheduling and requests works exactly the same. You lose the AI web-search source and the tailored
"why" text — that's the whole difference.

### Tuning AI cost

Costs come from the curator (Anthropic/OpenAI/Google charge per token; Ollama is free but runs on your
own hardware). Roughly cheapest-to-priciest levers:

1. **Turn off "AI from library."** Biggest saving for the least loss — most people should leave it off.
2. **Use a small, cheap model.** A fast/mini model (e.g. Claude Haiku, GPT-mini, Gemini Flash) is
   plenty for curation; you don't need a flagship model to pick from a 40-title shortlist.
3. **Run less often.** Nightly is the default; a longer schedule means fewer runs and fewer tokens.
   This is the most direct way to lower your total bill.
4. **Fewer AI sources per row.** A row can override the global sources (Rows → Edit) — keep AI web
   search only on the rows that benefit, and let the rest run on the free TMDB sources.

Worth knowing: the curator re-picks and re-explains **every** enabled person on every run, so a quiet
night with no library changes still spends roughly the same tokens as a busy one — how _often_ you run
(and which model) is what drives cost, not how much changed. (Runs do skip the Plex _write_ when a
person's row is unchanged, but that saves time on the Plex side, not AI tokens.)

The "AI web search" card also lets you pick the **search backend** — your curator's own web search
(Claude/GPT/Gemini), an **Exa** key (works with any provider, and the only option for Ollama), or
**Auto**, which uses both when available because they tend to find different titles.

**Seeing where the tokens go.** Every run records its AI cost so there's no guessing. Open a run
(Runs → click a run) and you'll see the **total AI tokens** for the run, then per person a breakdown
by _what the AI did_ — `final picks` (curation), `web search`, `library scan` — plus any **Exa
searches** (counted separately, since Exa bills per search, not per token). Each row also shows the
tokens its curation cost. The runs list shows each run's token total at a glance. Use it to spot which
step or which people cost the most, then tune with the levers above.

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
4. Tune the **Guardrails**: pick a **rating source** — TMDB (no extra setup), or IMDb / Rotten
   Tomatoes / Metacritic / Trakt (these read scores from **MDBList**, so add a free MDBList API key
   under Settings → Connections first). Then set a minimum rating and minimum number of reviews a
   title must clear, the fewest people who must want it, an optional **release-year window** (_on or
   after_ / _on or before_ — leave either blank for no bound; a show is judged by its first-air
   year), and the most titles to auto-request per night (a hard cap across both apps).
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
titles it didn't auto-send — with the title, year, rating, and a full **why it's here** breakdown:
one line per person and row that wanted it, with the reason (e.g. "Sarah · Comedy Classics · because
they watched Fawlty Towers"). That answers where a request came from and why, not just a count.
A long queue can be narrowed by a minimum rating and vote count (and to movies or shows) and
sorted by **Newest**, **Top rated**, or **Most wanted**, so the best picks triage first.
Tick the ones you want and click **Send to Sonarr/Radarr**. For the rest you have two choices, and the
difference is exactly what happens on the next run:

- **Reject** — a permanent "no". The title is never re-queued AND never auto-sent by a later run. It
  moves to the **Rejected** tab as a record. Changed your mind? **Allow again** (or **Allow all
  again**) on that tab moves it straight back to Waiting — immediately, with its who-wanted-it detail
  intact — ready to send. No waiting for a run.
- **Delete** — a "not right now". The title is removed from the list with no block, so if your people's
  taste turns it up again on a later run, it comes back to Waiting. Use it to clear clutter without
  slamming the door.

Both buttons carry a hover hint, and an always-visible line under the queue spells out the difference.
A title already in the library stops appearing on its own, and one that's already been sent (still
downloading, say) never re-consumes an auto-request slot, so a slow grab can't starve the queue.
Everything sent to Sonarr/Radarr moves to the **Sent to Sonarr/Radarr** log — each entry keeping when
it went, the app's answer (e.g. "added to Radarr"), and the same why-it-was-wanted breakdown. Each
sent entry links straight to the title's page in Sonarr/Radarr, and a **Clear** button tidies items
out of the log once you're done with them — Clear only hides the entry (the title stays in
Sonarr/Radarr and is never re-requested), it never un-sends.

It stays cautious on purpose. Missing titles are deduplicated across all your users — three people
wanting the same one is a single entry, and multi-person demand ranks it higher and can push it over
the auto-send bar. A title already in Radarr/Sonarr is skipped, never re-added, and a dry-run only
logs what it _would_ ask for. Every request (and every skip) is recorded in the audit feed, and the
run's detail page shows how many titles it requested.

Requires Radarr v3+ / Sonarr v4+ reachable from the Shortlist container.

## Troubleshooting

- **A run says "skipped" and no collections were made** — a skip is always a configuration
  outcome, and the run page now says which one. The two common ones: _every enabled row is a
  **shared** row_, so there is no per-person row to build for anybody (add one under Rows), or a
  **shared row can't reach its threshold** — a shared row is built only from titles several people
  have watched, so it needs at least 2 enabled users with viewing in common and will skip forever
  below that. Make it a per-person row instead if you want one person to get it.
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
