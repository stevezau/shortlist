---
title: Guides — rows, schedules and troubleshooting
description: A tour of the Shortlist web interface, how to configure rows and refresh schedules, per-user overrides, and how to troubleshoot a run that didn't deliver.
heading: Guides
---

## The web interface

- **Dashboard** — the impact report: what Shortlist delivered versus what people actually
  watched, for a window you choose (7 / 30 / 90 days, or all time — 30 by default). Each headline
  figure carries its change against the previous equal period, so you can see direction rather than
  a running total. There's also a **Sync watched now** button to refresh the numbers on demand.
  See "Reading the dashboard" below for what each figure actually means.
- **Rows** — create, edit, and reorder your rows. Each card shows who sees it and how it
  differs from the defaults (sources, libraries, freshness, placement). This is where
  the whole multi-row feature lives — see "Naming a row" and "Row placement" below.
- **Users** — everyone the server is shared with, plus you (badged `owner` — plex.tv's user list
  leaves the owner out, so Shortlist adds you itself). **Sync from Plex** pulls the roster again
  after you invite someone new (or to pick up your own owner row on an install that predates it).
  Enable/disable each person or **Enable all / Disable all** at once, pause someone (keeps their
  row, skips them on runs), set a request tag, add per-person row overrides
  (mute a row, resize it, or set its watch-history depth just for them), and see each user's
  restriction status. Turning someone **off** removes their rows from Plex and rewrites the share
  filters so they stop seeing the shared rows too; turning them back **on** undoes the second half
  straight away (their own row returns on the next run). **Pause** takes their rows off every shelf
  and **unpause** puts them straight back — neither waits for a run. If **Sync from Plex** finds
  somebody who no longer has access to the server, Shortlist turns them off and cleans up their rows;
  their history is kept, so you can switch them back on if they return. All of this runs as
  background jobs, visible on the **Jobs** page. Accounts with a Plex **restriction profile** (Younger Kid / Older Kid / Teen) are
  badged with that profile's name. Plex hides every collection from them, so no row is built — and
  Plex also refuses the privacy filters Shortlist writes, so those accounts are left out of them.
  Both go away by setting **Restriction Profile → None** in Plex → Settings → Users & Sharing; you can
  still limit them by rating or label there, which Plex only permits once the profile is None.
  A Plex Home account with **no** profile is an ordinary user: it gets a row and privacy filters like
  anybody else. Opening a person shows
  their recent watch history (distinct titles, with season/episode numbers for TV), their picks
  grouped by row (long lists collapse behind a "show more"), and a **Run now** button to rebuild
  just that person.
- **Runs** — a live **Activity** log streams each user through history → candidates → ranking →
  delivering as the run happens (seeded from the server so a reload replays it); per-user diffs
  grouped by row then library ("added X to Movies, Y to TV Shows"), each library showing its own
  ranked picks; errors as first-class rows with copy-for-GitHub buttons, LLM token usage. Open a
  person and click **How we picked** to read the full pipeline for them as one flow, per library:
  the watch history and seeds it started from → every candidate source's query and what it returned,
  each title tagged with whether it made the shortlist or the plain reason it fell out (already
  watched, not in your libraries, lost the ranking cut) → **how the shortlist was ordered** (the
  plain-code score plus the two fair-share passes — no AI ranks) → what was finally delivered and
  why. The AI web-search card shows the exact Exa queries and the prompt the model searched from,
  and marks each proposed title kept vs. dropped — or struck through when it resolved to no real
  match (a hallucination). Long lists of returned titles expand in place. A **cold-start** user (too
  little history to search from) gets the same page, showing the highest-rated titles pulled from the
  server as their fallback.
- **Logs** — what this instance has been doing, with a level filter (this level _and louder_), a
  text filter, live follow, **Copy**, and **Download .zip** for attaching to a bug report. Tokens,
  API keys and passwords are stripped out server-side before anything reaches the page or the zip,
  so it's safe to share. The file keeps the last 10 × 10 MB and always records at DEBUG, regardless
  of the console level in Settings → Advanced.
- **Requests** — the approval inbox for titles your picks wanted but the library doesn't have
  yet. Approve to send to Radarr/Sonarr, or reject so they never come back (see "Requests" below).
- **Jobs** — every piece of background maintenance Shortlist does, in two areas.

  **Jobs** lists them one per line: the name, how the last run went, when the next one fires, and the
  button. **Run now** holds the six you start yourself — **Sync people from Plex** (pull the roster
  from plex.tv + Tautulli), **Sync watch history** (re-read everyone's watched set), **Check and fix
  rows on Plex** (preview, then fix, rows left on the wrong shelf), **Privacy sync** (re-merge every
  share filter), **Back up the database**, and **Clear out old records** (drop run history past the
  limit you set). A tag on the line says what a job changes on your server: **Can delete** on the
  one that can remove a collection, **Changes Plex** on the ones that write. Anything untagged only
  reads, or only touches Shortlist's own records. **Automatic** holds the ones Shortlist queues for itself when
  something changes — removing a disabled person's rows, hiding a paused one's, tidying up after a
  row edit. Those have no button by design: each one is aimed at a specific person or row by the
  action that queued it.

  Open any job for its description, its settings (the frequency picker on the scheduled ones, the
  backup retention and restore list), what the last run reported, and **Previous runs** — that job's
  own history. Anything a run is doing right now (a progress bar, a drift preview and its Fix button)
  stays visible on the line without opening it.

  **Activity** is the other half: every job run across every kind, newest first, filterable by All /
  In flight / Failed. Open a row for what it was asked to do, what came back, how long it took, and
  the error if it failed. Anything that fails is retried with backoff and survives a container
  restart; if it finally gives up, it reaches the notification bell.

  Clearing run history lives on the Runs page — it clears the browsable history but preserves your
  dashboard metrics (delivered/watched/hit rate survive indefinitely), and doesn't affect Shortlist's
  ability to tidy up rows on Plex.

- **Settings** — one scrolling page, organised into a grouped sidebar sub-nav that jumps to each
  section and tracks where you are: **Connect** (Connections), **Rows** (Finding titles, Row
  defaults, Row placement), **Add-ons** (Requests), and **System** (Advanced, API access, Danger
  Zone). Each section is walled off by a rule, and its own sub-headings sit a clear rank below the
  section title. Every connection is re-testable in place. (Each row's run schedule lives in that
  row's editor, not here — see Schedules below.)

## Schedules

**Jobs → Timeline** lists everything on a timer — rows and background jobs together, in the order they
actually fire, each with its next run and an inline editor. Row schedules are edited on the Rows page
(one place per setting), and jobs are edited in place.

It sits with Jobs rather than in its own nav entry because "what background work exists" and "when
does it run" are two views of one thing — as separate pages, every job was listed twice and neither
page could answer a whole question. `/schedule` still redirects here.

Two jobs are worth knowing about there:

- **Sync watch history** reads only what changed since last night, then does a complete re-read
  weekly (it is the only thing that can notice a title being un-watched or removed). If a library
  cannot be read incrementally, that library falls back to a complete read on its own rather than
  serving a stale watched set.
- **Privacy sync** runs nightly (05:15 by default). It re-merges every account's share filter and
  builds, delivers and promotes nothing — so it can only ever make your server _more_ private. It is
  the cheapest safety net against drift.
- **Check and fix rows on Plex** runs nightly at **05:45** — after the rows build and after the privacy pass, so it
  checks the state those actually left behind. Drift is the failure nobody notices: a row left on the
  wrong shelf stays there until somebody happens to look, so the thing that repairs it is on by
  default. It is also the **only** schedule you can switch off completely — it _writes corrections_
  to Plex, so clearing its box means off, not "fall back to the default" the way every other blank
  cron does.

**Every row runs on its own schedule** — there is no single server-wide one. Open a row (Rows → edit)
and set its **Schedule**: **Nightly** or **Weekly** presets (just pick a run time), **Custom** for
anything else, or **Off** to only run that row by hand. New rows default to nightly at 03:30 server-local;
on upgrade, existing rows keep whatever your old global schedule was. Rows that share a cron run
together. To skip a person entirely, pause them on their detail page.

### Writing a custom schedule

Every **Custom** schedule box in Shortlist — a row's schedule, and the watch-history / user-sync /
backup pickers on Jobs — takes either form:

- **Plain English**: `every 30 minutes`, `every 4 hours`, `every 4 hours at 17 past`, `hourly`,
  `nightly at 3:30am`, `daily at 21:15`, `mondays at 9pm`, `weekdays at 6am`, `weekends at 10am`.
- **A cron expression**, if you already think that way: five fields — minute, hour, day-of-month,
  month, day-of-week. `0 */6 * * *` is every six hours; `0 4 * * 1` is Mondays at 4am.

Whichever you type, the line underneath tells you what it will actually do and what gets saved, and
nothing saves until it parses — so a typo can't quietly leave you on the built-in default. Times are
the server's, not your browser's.

## Blocking a seed

A **seed** is one of a person's recent watches that Shortlist searches from. When a watch isn't
really them — a film they put on for someone else, a genre they don't want more of — block it: the
watch stays in their history, it just stops shaping their picks.

The natural place to do it is a run's **How we picked** page, on the seeds list, where a bad seed is
usually what you noticed in the first place. There's also a search box on a person's detail page
(**Users → someone → Settings → Blocked seeds**) for a title you remember but can't find a run for.

Blocks are personal. A **shared** row is public, so one person's block deliberately does _not_
reshape what everyone else sees — otherwise an individual preference would become a server-wide edit
nobody else can see or undo. Shared rows use their own server-wide list
(`recommendations.blocked_shared_seeds`).

## Starting from a template

**Rows → Add a row** opens a gallery rather than a blank form: _Picked for You_, _Because you
watched…_, _Happy to see again_, _Fresh finds_, _From the vault_, _Popular on this server_, _Movie
night_, _More TV to watch_, and _Start from scratch_. Each tile names the two or three settings it
changes, so picking one also shows you which knobs matter. Nothing is locked in — every field is
editable afterwards, and the template is not stored on the row.

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

**A `{top_seed}` row needs one more setting to be honest.** By default every row is built from a
person's 30 most recent watches blended together, so a row titled "Because you watched The Bear" is
really "because you watched these thirty things, one of which was The Bear". Set **Row editor →
Watches every source builds from** to `1` and the row genuinely is what that one title led to. The
editor prompts you for this as soon as a row's name uses `{top_seed}`.

One catch, which the editor also tells you: seeds are shared out across the media types a row
covers, and a single watch is either a film or a show — never both. So a row set to **Movies and
TV** with a budget of 1 seeds only one of them, and the other library's collection never builds.
For a row covering both, use `2` (one of each), or set the row to Movies only or TV only.

**Watches every source builds from** (1–100, default 30) is worth knowing about on its own: it
decides how many watched titles every discovery source searches from. Fewer means a tighter, more
coherent row about a couple of things; more means broader coverage of someone's taste. **Watches the
AI web search looks up** is a slice off the front of that same list, and caps the AI web-search
source alone.

There is a server-wide default for it in **Settings → Finding titles**, and any row can override it.
The global stops at 5 while a row goes down to 1 — a single seed is a deliberate choice for one row,
not something to impose on every row at once.

### Which watch a one-title row follows

A row built from one watch normally follows the most recent one, and stays on it until that person
finishes something else. If they watch little for a fortnight, the row says the same thing for a
fortnight.

**Row editor → Which watch it follows** changes that. Raise **Recent watches to choose from** above
`1` and the row cycles instead: still one watch per row, but a different one each day, working
through their last few and then coming round again. It cycles rather than picking at random —
a random pick repeats, and a repeat is indistinguishable from a row that has stopped working. Two
people's rows cycle out of step, so a whole server doesn't rebuild on the same night.

The setting only appears on rows built from one or two watches. Above that a row is blending a whole
history and has no single watch to follow. It is also not the same as raising **Watches to build
from**, which is the setting people reach for first and the wrong one: that blends more watches into
one row, diluting the very claim a `{top_seed}` title makes. Cycling keeps the row about one watch
and moves which one.

**Rows that follow a watch always refresh nightly** — whether they follow it by name (`{top_seed}`)
or by cycling. A row whose title claims a recent watch can't be allowed to lag behind it: at the
usual cadence it would go on naming last week's film for a week after the person moved on. So
**How often it changes** is not offered on those rows at all; the row simply refreshes nightly.
Every other row keeps the setting.

There is a modest cost to that. Refreshing nightly does not only mean "a write when the watch
changes" — on the nights it hasn't changed the row still swaps its weakest third for new titles, so
it writes to Plex most nights, per person, per library, where an ordinary row on the default cadence
writes about weekly. It does not cost any extra AI usage: candidates are gathered once per run
whatever a row's cadence is, so how often a row refreshes has no bearing on it.

## The order titles appear in

**Row editor → Order** decides how a row's titles are arranged in Plex:

| Order             | What you get                                                            |
| ----------------- | ----------------------------------------------------------------------- |
| **Best match**    | Strongest suggestions first — how well each title matches their viewing |
| **Highest rated** | Highest score first, from whichever service you configured              |
| **Newest**        | Most recently released first                                            |
| **Shuffled**      | A different order every day, from the same titles                       |

Plex itself only sorts a collection by release date, alphabetically, or by a custom order, so every
one of these is applied by Shortlist and delivered as that custom order — which is what the Home row
displays.

**Highest rated** uses TMDB by default, which needs no setup. To sort on IMDb, Trakt, Rotten Tomatoes
or Metacritic instead, set **Settings → Finding titles → Rate titles using** — those come from MDBList
and need its API key (the same one the Requests feature uses). Without a key, or once MDBList's daily
quota is spent, the row falls back to TMDB for its _whole_ ordering rather than sorting half the row
on one scale and half on another.

**Shuffled** is the only one with a cost worth knowing about. The other three are applied while the
row is being written anyway, so they are free; shuffled rewrites the collection on Plex every day,
including days when nothing about the row has changed. On a server with many people that is real
write volume, so it is off by default.

A shuffle is stable within a day — re-running a row the same night reproduces the same order, and two
people's copies of one row shuffle differently.

## Where a row shows

The **Row editor** → **Where it shows** grid picks which Plex screens a row appears on. Two
surfaces, two audiences, and every one of the four switches is independent:

|                       | You | Everyone else |
| --------------------- | --- | ------------- |
| **Recommended shelf** | ☑   | ☑             |
| **Home screen**       | ☑   | ☑             |

The columns are real, not cosmetic: every person gets their **own** Plex collection, so each switch
is set on a different collection. **You** is your own row — Plex's Home shelf applies to the server
owner alone. **Everyone else** covers the people you've shared with plus Plex Home members, whom
Plex groups together under Shared Users' Home. Each of them only ever sees their own row; everyone
else's is excluded from their share filter.

Turn all four off and the row still gets built and kept private; it claims no Recommended slot, and
you'll find it under the library's **Collections** tab. One caveat: on a run where Shortlist can't
match an existing collection back to its row — a `{top_seed}` row that produced no picks, say — that
row keeps its own Home flag for that run. It stays off the Recommended shelf, and it is only ever
visible to the person it belongs to.

**The one thing this can't do:** hide friends' rows from _your_ Recommended shelf while leaving them
on theirs. Share filters are what hide a row from someone, and you own the server — there is no
share with yourself to attach one to. So with **Everyone else → Recommended shelf** on, every friend's
row is on your shelf too. Turn it off (leaving **Everyone else → Home screen** on) and each friend still
gets their row on their own Home, while your shelf stays yours. Shortlist shows this warning at the
switch itself.

A common setup: **You** both on, **Everyone else** Home only — you get your row on your Home and your
shelf, everyone else gets theirs on their Home, and nobody's row clutters anybody else's view.

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

Each row can have its own artwork on Plex. In the **Row editor** → **Artwork**, pick one of:

- **Plex default** — leave Plex's own collection artwork alone (the default). Switching a row _back_
  to this after it had a custom poster reverts the artwork on Plex on save.
- **Upload** — upload your own image (a tall 2:3 poster looks best; up to 8 MB). It's downscaled and
  stored, then applied to the row's collection(s) on the next run.
- **Text** — a clean built-in poster: your **Title** and **Subtitle** over a gradient. No AI needed,
  works on any setup. Use `{user}`, `{library_name}`, and `{top_seed}` to personalise the text.
- **AI image** — an image generated from your text and **Art style**, using your AI provider's image
  model. This reuses your AI provider's key, so it's available when that provider is **OpenAI** or
  **Google** (Anthropic and local servers can't generate images — use a Text poster or Upload instead).

Hit **Preview** to see a sample before saving. Generated images are made once and reused across
runs (they refresh when you change the text or style), so posters don't slow a run down or cost per
user. Posters are cosmetic — a poster that can't be made never blocks a row from building.

## Reading the dashboard

Everything on the dashboard is scoped to the window selected at the top — **the last 30 days** by
default. That matters more than it sounds: these figures used to be lifetime totals, which made
every ratio a measure of how long Shortlist had been installed rather than of how good the picks
were. A pick can only ever be credited as watched within **30 days** of being delivered, but the old
denominator kept every pick ever delivered, for ever — so each night added ~60 permanently
uncreditable picks per person to the bottom of the fraction and the number could only sink.

**Watched** — picks people watched in the window. A pick delivered last month and watched this week
counts here: this figure is about watching, not delivery.

**People watching** — how many people watched at least one pick, out of everyone currently enabled.

**Avg to watch** — average days from a title first being recommended to it first being watched, over
titles first watched in the window. Lower is better, and the change arrow is coloured accordingly.

**Landing rate** — the one percentage, and the only one computed carefully enough to trust. It is the
share of picks watched within 30 days of delivery, measured over a **matured cohort**: picks
delivered in the window _and_ at least 30 days ago. A pick delivered yesterday cannot have been
"watched within 30 days" yet, so counting it would drag the rate toward zero for no reason. On a
7-day window there is usually no matured cohort at all, and the card says so instead of showing a
misleading number.

**By person / By row** — counts, not percentages, sorted by what was actually watched. At these
sample sizes a percentage is noise: ranking by one put a person with `1/31` above a person with
`3/103`. People and rows with nothing in the window fold away behind a disclosure rather than filling
the list with empty bars, and rows you have since deleted are hidden the same way — their picks still
count in the totals above.

Hiding a deleted row is the default because its picks are real history. If you want it actually gone —
a throwaway test row, say — expand the disclosure and choose **Delete their history**. That permanently
removes those picks from every total that counts them, here and on each person's page, and cannot be
undone. Rows that still exist are never affected, whichever slug is named: Shortlist recomputes what is
eligible on the server rather than trusting the request.

**Watches per week** is deliberately the long view: always the last 16 weeks, whatever window is
selected.

## Recommendation sources

Settings → **Finding titles** controls where candidate titles come from. Shortlist pools every
source you enable, keeps only what's already in your library, then ranks them (a simple, no-AI
score) and writes each pick's "why" in code. More sources = wider reach. Available today:

- **TMDB — similar titles**: the baseline — titles TMDB says are similar to what each person watched.
- **TMDB — discover by taste**: widens into popular, well-rated titles in the genres each person
  leans toward (derived from their watch history).
- **Trakt — related titles** (needs a Trakt API key, added in Connections): uses Trakt's
  recommendation graph, which often surfaces "what to watch next" picks TMDB's similar list misses.
- **AI — web search for what to watch next**: searches the live web for current, well-reviewed titles
  to watch next, then resolves each against your library — reaching beyond TMDB/Trakt to fresh releases
  and critics' lists. Works on **every** provider, via the **Search backend** you pick in its card:
  your provider's own web search (Claude, GPT, or Gemini), an **Exa** key (any provider — the only path
  for a local Ollama model), or **Auto** (the default), which uses your provider's tool _and_ Exa
  together when both are set up, since they surface mostly different titles. If a backend needs a key
  you don't have yet, the card lets you enter it right there.

Each row also chooses **which libraries** it builds in (the row editor's Libraries picker). A Plex
collection lives in one library, so a row builds one collection per library you tick — leave them all
ticked (the default) to cover every library, or point a row at just one (e.g. "4K Movies") on a
server with several libraries of a type. What the row recommends (movies, shows, or both) follows the
libraries you pick.

### Freshness, already-watched, and cost

Settings → Finding titles has three more dials (each per-row overridable):

- **How often it changes** (called **Freshness** in Settings, where the global lives) — how
  often a row's picks change. This is a **cadence, not a nightly shuffle**:
  `1.0` refreshes every night, lower means every few days, and `0.0` means "build once, then never
  reshuffle". On most nights an unchanged row is left exactly as-is — no rebuild, no Plex write
  — which is why a person's row stays familiar instead of being reshuffled daily. On a refresh night
  the strongest ~two-thirds of picks stay and the weakest third rotates out. Default `0.5`
  (about weekly). If you trigger two runs the same day, a row that already isn't due won't change —
  that's expected.
- **Already-watched titles** — how much of a partly-watched title still counts as "watched" and gets
  filtered out. Default keeps anything finished out of the picks.
- **Watches the AI web search looks up** — how many of each person's recent titles the AI web-search
  source looks up (one cached search each), taken off the front of the list above. It's the main
  **cost lever** on that source — lower it to spend fewer tokens/Exa searches.

### If a watched title still gets recommended

Shortlist reads each person's **complete** watched set from Plex every run — including titles they
only _marked_ watched (ticked off, or a whole season marked) rather than played — so this is rare.
It reads the library _as that user_, with the per-user server token Plex mints for every share, and
`viewCount > 0` covers both plays and marks at any depth. Nothing to configure, and it works whether
or not Shortlist runs on the same machine as Plex. (This replaced the old playback-history read,
which saw plays only and capped at ~200 — on one real server that hid **13,201** of a user's watched
titles behind the ~1,000 the API reported.)

When it does happen, it's almost always timing: **the read is per-run, so a title you mark watched
after the last run stays eligible until the next one.** To fix it immediately without waiting for a
scheduled run, go to **Jobs → Sync history** — it re-reads every user's watched set right now,
writes nothing to Plex, and updates what Shortlist knows (and the "N titles watched" count on the
Users page). Any run after that leaves the title out.

### Everything above is only the _default_ — rows override it

Settings → Finding titles sets what a row uses **unless the row says otherwise**. Open any row
(Rows → Edit) and it defines its own recipe:

| In the row editor                                    | What it overrides                                                                         |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| **Recommendation sources**                           | Switch to "Choose for this row" and tick its own sources                                  |
| **Libraries**                                        | Which Plex libraries it builds in — which also sets what it recommends                    |
| **How often it changes**, **Already-watched titles** | How often it refreshes, and how much already-watched it allows                            |
| **Row size**, **Audience**                           | How many titles, and who gets it                                                          |
| **Watches the AI web search looks up**               | How many recent watches AI web search looks up for this row (shown only on rows using it) |
| **Request tag**                                      | The Sonarr/Radarr tag on titles requested for this row's audience                         |

So a "What to watch next" row can be Trakt-only, a "Hidden gems" row can be AI-web-search-only
pointed at just your 4K library, and your default "Picked for You" can stay on the global settings —
all on the same server, all at once. The Rows list shows each row's overrides on its card, so you can
see at a glance which rows differ.

A row left on "Use global default" stays in sync with Settings → Finding titles.

**And one step finer — per person.** Row size and recent-watches depth can also be set for a single
person on a single row: open that person (Users → click them), find the row, and use **Customize for
this person**. Their value wins over the row's, which wins over the global default. Leave a
customization on "default" and that person follows the row like everyone else.

**The one exception is the seeded "Picked for You" row**: its **name** and **size** always follow the
global Settings (Row defaults) so they stay in sync everywhere — the row editor points you there
instead of offering its own. Its sources, libraries and audience are its own, exactly like any other
row.

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

**AI is off by default** (the AI provider is set to "None" out of the box, and the AI web-search
source is off) — Shortlist works fully with **no AI at all**. AI now has exactly one job: the
**AI web-search source**, which finds acclaimed "what to watch next" titles the TMDB lists miss.
Everything else — gathering candidates, ranking them, and writing the "why" under each pick — is done
in code, with no AI and no per-token cost.

**Building a row happens in four steps:**

1. **Find candidates.** Every source you enabled goes looking for titles. Most use **no AI**: the two
   TMDB sources (similar + discover) and Trakt are plain lookups against those services — free, no API
   key beyond the ones you already set up. Only the **AI web-search** source uses your AI provider —
   see below.
2. **Keep only what you own.** Everything found is matched against your actual library and against what
   the person has already watched. Anything you don't have, or they've already seen, is dropped. **This
   is why a pick can never be a title you don't own** — every source only ever contributes real, owned,
   unwatched titles.
3. **Balance and rank.** Shortlist takes a fair share from each source (so one chatty source can't
   crowd out the rest) and scores them. **No AI here** — it's a simple ranking.
4. **Deliver + explain.** The top-ranked titles fill the row, each with a one-line "why" written from
   the seed behind it ("Because you liked sci-fi like Dune", "Because you watched Fargo"). **No AI
   here either** — the reasons are generated in code, so they cost nothing and read the same whether or
   not you use AI.

### The one AI-powered source

- **AI — web search** (the "AI web search" toggle): searches the live web for acclaimed, current
  "what to watch next" titles, then keeps the ones you own. In our own testing this was a strong extra
  source — it surfaces well-reviewed titles the TMDB lists simply don't return. It's the only place AI
  spends anything, and it's off by default.

**How it actually works.** Shortlist takes each person's recent watches, turns them into real web
searches ("what to watch if you liked X"), and hands the results to your model — which then picks
from what was found. The model never browses; it reads.

That ordering is the whole trick. Because **Shortlist** runs the search rather than the model, the
source works with **any** provider — including a local Ollama, llama.cpp or LM Studio server with no
internet access of its own. A local model that could never search the web still gets to recommend
from current web results.

You choose the search backend on the "AI web search" card:

| Backend                        | Works with                         | Trade-off                                               |
| ------------------------------ | ---------------------------------- | ------------------------------------------------------- |
| Your provider's own web search | Claude, GPT, Gemini only           | no extra signup; unavailable on local models            |
| [Exa](https://exa.ai) key      | **every provider, local included** | one extra free-tier signup                              |
| **Auto** (default)             | both, unioned when both are set up | widest coverage — they find noticeably different titles |

**Why we suggest adding an Exa key**, even when your provider can already search:

[Exa](https://exa.ai) is a search engine built for AI to read rather than for people to browse — it
returns ranked results with the relevant text already pulled out, so the model spends its effort
judging films instead of wading through web pages. In practice that buys you four things:

1. **It's the only option that works with a local model.** An Ollama or LM Studio server on your own
   hardware has no way to reach the internet. With Exa, Shortlist does the searching and hands over
   the findings, so a fully offline model still recommends current titles.
2. **Your results stop depending on which AI you picked.** Switch from Claude to a cheap local model
   and the search half stays identical — only the choosing changes.
3. **The cost is predictable.** Exa bills per search rather than per word, and those searches are
   reported separately from AI tokens. Results are reused for 14 days and shared across everyone on
   the server, so a popular film is looked up once, not once per person.
4. **Auto gives you both.** Left on the default with both configured, Shortlist unions the two —
   they reliably surface different films, so coverage is wider than either alone.

Entirely optional: leave it empty and everything still works, you're just limited to your provider's
own search (or to none at all).

### If you don't want to use AI

Leave the AI provider on **None** (Settings → Connections — this is the default) and the AI web-search
source off. You still get full, per-person private rows: candidates come from TMDB/Trakt, ranked by
score with plain "Because you watched…" reasons. Everything about privacy, scheduling and requests
works exactly the same. The only thing you lose is the AI web-search source — nothing else changes,
because nothing else used AI.

### Tuning AI cost

Cost comes entirely from the AI web-search source (Anthropic/OpenAI/Google charge per token; a local
Ollama/OpenAI-compatible server is free but runs on your own hardware). Roughly cheapest-to-priciest
levers:

1. **Turn AI web search off, or limit which rows use it.** A row can override the global sources
   (Rows → Edit) — keep AI web search only on the rows that benefit, and let the rest run on the free
   TMDB/Trakt sources.
2. **Search fewer recent watches.** The source runs one web search per person's recent watch; lower
   `recommendations.recent_count` (Settings → Finding titles) to cut searches. Results are cached 14
   days and shared across users, so a popular title is searched once server-wide.
3. **Use a small, cheap model.** A fast/mini model (e.g. Claude Haiku, GPT-mini, Gemini Flash) is
   plenty; you don't need a flagship model to read a few search results.
4. **Run less often.** Nightly is the default; a longer schedule means fewer runs and fewer searches.

5. **Use a local model.** An Ollama or LM Studio server on your own hardware costs nothing per run.
   This needs an Exa key, since a local model can't search the web itself — see
   [the backend comparison above](#the-one-ai-powered-source).

**Seeing where the tokens go.** Every run records its AI cost so there's no guessing. Open a run
(Runs → click a run) and you'll see the **total AI tokens** for the run, then per person a breakdown
by _what the AI did_ — `web search` — plus any **Exa searches** (counted separately, since Exa bills
per search, not per token). The runs list shows each run's token total at a glance. Use it to spot
which people cost the most, then tune with the levers above.

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
titles it didn't auto-send — with its poster, title, year, rating, and a full **why it's here**
breakdown: one line per person and row that wanted it, with the reason (e.g. "Sarah · Comedy Classics · because
they watched Fawlty Towers"). That answers where a request came from and why, not just a count.
A long queue can be narrowed by a minimum rating and vote count (and to movies or shows) and
sorted by **Newest**, **Top rated**, or **Most wanted**, so the best picks triage first.
Posters come straight from TMDB's image CDN (`image.tmdb.org`) — the only third-party asset Shortlist's
web UI fetches. An install behind a restrictive network, or a browser with an ad-blocker, will show a
placeholder tile instead; so will a title TMDB has no artwork for, and one queued before posters existed
(those fill in on the next run that re-surfaces the title). Nothing else on the page depends on it.

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

### Why is a title still waiting?

The auto-send bar is deliberately higher than the bar to be requestable at all: a title is sent
without asking only if it clears **both** `requests.auto_min_demand` (default 3 distinct people) and
`requests.auto_min_rating` (default 8.0) — a 7.9 wanted by twenty people still waits. Beyond that:

- **On an exclusion list** — a past delete in Radarr/Sonarr leaves the title on an import-exclusion
  list, and Shortlist will never auto-send one (the app would refuse the add anyway). The card says
  so; clear it in Radarr/Sonarr first, then approve.
- **Over the per-run cap** — `requests.max_per_run` auto-worthy titles go per run; the rest wait.
- **Already in Radarr/Sonarr** — the card shows a **Downloaded / Downloading / Searching / Not
  monitored** badge if either app already tracks it, which normally means it was added by hand after
  it landed here. It drops off the list on the next run.

## Backups

Shortlist copies its whole database to `/config/backups` on a schedule (Jobs → Backups; nightly at
3 AM by default), before every upgrade, and before any restore. It keeps the newest 10 by default.

A backup holds everything Shortlist knows: settings and connections, your rows and their audiences,
the people it tracks, run history and each run's picks, the request inbox — and, most importantly,
the `restriction_snapshots` of each user's original Plex share filters.

Because a backup holds your rows' **audiences**, restoring one also restores who could see which
rows at that moment. If you have narrowed a shared row's audience since the backup was taken,
restoring widens it again and those people will see the row after the next run. Shortlist says so
before you confirm and again afterwards, but it does not undo it for you — check Rows before
restarting. Those snapshots are the only
record of how your server's sharing looked before Shortlist touched it, and **Uninstall restores
from them**. Everything else is rebuildable by hand; that isn't.

Two things worth knowing:

- Backups sit beside the database in the `/config` volume, so they survive removing and recreating
  the container — but not losing the volume. Copy them off the host if that matters to you.
- `/config/secret.key` is **not** in a backup. It's the key your Plex token and AI keys are
  encrypted with, so restoring a database without that same file leaves those credentials unreadable
  and you'll have to re-enter them. Keep a copy of it alongside your backups.

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
  didn't add), and confirm Plex Media Server is ≥ 1.43.2.10687 (older builds ignore the exclusion).
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **A watched title keeps getting recommended** — the watched set is read per run, so a title you
  mark watched _after_ the last run stays eligible until the next one. **Jobs → Sync history**
  re-reads everyone's watched set immediately (writes nothing to Plex); any run after that drops it.
- **Everything broke, get me out** — Settings → Danger Zone → **Uninstall** restores every
  user's share filters from the pre-Shortlist snapshots and deletes every shortlist-labeled
  collection. Kometa and other tools' collections are never touched.
- **Did anything drift out of sync?** — Settings → Danger Zone → **What Shortlist has on your
  Plex** ("Check Plex") lists every shortlist-labeled collection read straight from the server (not
  the database), flagging any whose user/row no longer exists in the app. Every collection is
  labeled at creation (atomically — a collection that can't be labeled is deleted rather than left
  as an orphan), so a cleanup always finds them all; this is how you confirm it.

## Exposing Shortlist to the internet

Whether Shortlist is reachable from outside your network is **your call** — it is a normal web app and
it does not assume either way. If you do publish it, these are the things worth knowing.

**Put it behind a reverse proxy with HTTPS.** The session cookie is only marked `Secure` when the
request arrives over TLS, and HSTS is only sent then too. Over plain HTTP neither protects anything.

**`FORWARDED_ALLOW_IPS` defaults to `*`**, which trusts the `X-Forwarded-For` of whatever connects —
convenient when your proxy is on another host, but it means the client IP Shortlist logs is whatever
the caller claims. Set it to your proxy's address if you publish the container port directly.

**Only `/api/system/health` answers without a login**, and it returns nothing but `{"status": "ok"}`.
Everything else requires the owner's Plex account, re-checked on every request. Login is rate-limited,
and so are failed API-token attempts.

**The API token is owner-level access.** Anything holding it can do anything you can, including
deleting rows and rewriting share filters. Rotate it from Settings → API access if it leaks; the old
one stops working immediately.

**URLs you enter are fetched by the server**, which is the point — your Plex, Tautulli, Radarr,
Sonarr and Ollama are usually on private addresses, and all of those keep working. The one thing
Shortlist refuses is a cloud instance-metadata address (`169.254.169.254` and friends), which no media
server runs on and which hands out credentials on a hosted VM.

**Backups contain everything**, including your Plex token and the pre-Shortlist share-filter snapshots
that Uninstall restores from. They sit in `/config/backups` — treat that directory like a password
file.
