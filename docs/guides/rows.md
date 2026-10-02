---
title: Rows and templates
description: Start a row from a template, name it, choose how its titles are ordered, and pick which Plex screens it shows on.
heading: Rows and templates
nav_order: 2
---

## Starting from a template

Stable {{ site.stable_version }} has nine starting templates: _Picked for You_, _Because you
watched…_, _Watch it again_, _Fresh finds_, _Seasonal_, _From the vault_, _Popular on this server_,
_Movie night_ and _More TV to watch_. **Rows → Add a row** opens the gallery. Each tile describes
what it changes; every field remains editable afterwards.

**Development preview:** the compact gallery adds search, filters and a selected-template preview.
**Use template** opens the editor; **Start from scratch** opens an empty row directly. The development
build also adds _Your requests_, for titles someone requested that are now ready on Plex.

## Editing a row

On stable, the row editor groups its fields under descriptive headings such as **How it looks on
Plex** and **What kind of row is this?**. Settings stay a draft until you save; renaming and artwork
operations have their own actions.

### Development preview: editor navigation

The editor labels its current **Row type** and starts with the appearance fields. The sticky section
buttons use the same names as their headings: **Appearance**, **Row settings**, **Audience**,
**Titles & filters**, **Schedule**, **Plex placement** and **Requests**. Choosing one opens it,
places its heading below the navigation and briefly highlights the destination. The active button
also follows the section you read while scrolling. Keyboard focus moves to the section heading;
reduced-motion preferences disable animated scrolling. Folding a section keeps its draft intact.
Delivery and watch metrics are visible under **How this row is doing**, including when it is too
early to judge a row. The preview shows an explicitly illustrative row, audience, size, the viewing
basis and sources, watched-title policy, schedule and placement. **All outcome details** retains
the remaining settings summary.
The outcome facts stay beside the form on a desktop and remain visible above the settings on a
phone; its section buttons appear immediately below the page heading, before the visible performance
and outcome facts. Only the illustrative artwork preview is folded there. **Save changes** remains at the bottom of the screen.

Row settings stay a draft until you save. **Rename**, artwork operations and the row's on/off switch
keep their separate actions. **Rated by · global setting** saves immediately and affects every row
and Requests; its saving, success or error message appears beside that control.

## Row kinds

Stable has five row kinds; the development build adds **Your requests** as a sixth. In stable,
use **What kind of row is this?**; in the development editor, choose **Change row type**. Each kind fills the row in a different way, and picking one changes which
settings appear below it, so you're never hunting for a setting that doesn't apply to what your row
does:

- **Picked for You** — Titles they haven't seen yet, matched to everything they like.
- **Because you watched** — More like one thing they watched recently. Named after it, like "Because
  you watched Dune".
- **Watch it again** — Favourites they've already finished, ready to rewatch.
- **Your requests — Development preview** — What they asked for in Overseerr, once it's on Plex. Each title leaves once
  they've watched it. See [Your requests rows](#your-requests-rows) below.
- **Seasonal** — Only appears around the holidays you pick, like Halloween or Christmas. Filled in any
  of the ways above, except Your requests.
- **Popular on this server** — What lots of people here are watching. Everyone sees the same row.

**Seasonal isn't another way of filling a row** — it's a schedule wrapped around one of four of the
others. Pick it, choose which seasons the row follows, then choose **how it's filled**: Picked for You,
Because you watched, Watch it again, or Popular on this server. The settings for whichever fill you
pick then show underneath, exactly as they would if the row weren't seasonal at all — so every
combination (a seasonal "Because you watched" row, a seasonal shared row, and so on) stays reachable.
A Your requests row is the one kind that can't be seasonal: a request lands when it lands, so no
season decides whether the row shows. See [Seasonal rows](#seasonal-rows) below.

The default row can't be Seasonal — its name is the one every person's everyday row uses — so the
picker shows Seasonal disabled there, with an explanation. Every other kind is still available on the
default row, unless its name (which lives in **Settings › Row defaults**) uses `{top_seed}`: that
name makes it a Because you watched row whatever else you pick, so the other kinds are disabled, with
a link to where the name is changed.

**You can switch a row's kind at any time.** On a row you've already saved, switching opens a dialog
first: "Change this row to X?", then a plain sentence for each thing that will change — settings that
appear, settings that disappear, and anything the new kind forces (a row whose name follows a watch,
for example, picks new titles every night). Nothing is applied until you confirm. On a new, unsaved
row the switch just applies, since there's nothing on Plex yet to warn you about.

Switching is always worked out from the row as you opened it (or, for a new row, as its template
filled it in), plus anything you've changed by hand in a setting that kind has — never from the kind
you switched to a moment ago. So switching back gives you the row exactly as it was, and a setting you
only changed after switching (say, Take turns on a Picked for You row you'd made Because you watched)
goes back with it; the dialog lists it. The one exception is **Seasonal**: picking it makes the row
you're looking at seasonal, filled the same way, and changing only how it's filled keeps the seasons
you've narrowed.

When a switch needs a new name, saving stores it with the switch. Where it shows up on Plex depends
on the row: the rename screen renames it straight away; a name that uses `{top_seed}` (before or
after) appears the next time the row runs; a switch to or from Popular on this server uses it when
the row is rebuilt; and a switched-off row takes it when you turn it back on and it runs. Until you
save, the new name sits in the Name box, where you can still change it.

That dialog also says what happens on Plex once you save, because it isn't the same for every switch:

- **Switching between a per-person kind and Popular on this server** changes what's on Plex
  immediately, not on the row's next run. Switching a per-person row (Picked for You, Because you
  watched, Watch it again) to **Popular on this server** removes everyone's own copy of the row from
  Plex right away; one shared row, the same for everyone who gets it, is built the next time the row
  runs — if enough people have watched titles in common. Switching a shared row back to a per-person
  kind removes the shared row from Plex right away, and each person gets their own private row the
  next time the row runs. Either way, until that next run, nobody has the row. This is all that
  saving does on Plex, even when the same switch also turns Seasonal on or off.
- **Switching to Seasonal** applies the season dates on Plex straight away. If today is outside
  them, the row is hidden as soon as you save.
- **Switching away from Seasonal**: if the row is hidden between seasons right now, saving puts it
  back on Plex straight away, still holding the titles from its last season.
- **Every other switch** doesn't touch Plex until you save and the row next runs.
- **A switched-off row** isn't on Plex, so no switch changes anything there; its settings apply when
  you turn it back on. If **Pause all runs** is on in Settings, anything that waits on a run waits
  until runs are resumed, and the dialog says so.

A switch to a per-person kind can still be refused when you save, if another per-person row already
uses the same name in a shared library — the editor shows that the same way it does today.

## Naming a row

A row's name can be plain text ("Hidden Gems") or use a placeholder that fills in per person when
the row is built:

- `{library_name}` — the library the row is built in. `✨ {library_name} Picked for You` becomes
  "✨ Movies Picked for You" in your Movies library and "✨ TV Shows Picked for You" in your TV
  library. This is the default row name, so a server with several libraries gets distinct titles
  instead of two identical "Picked for You" rows.
- `{user}` — the person's name. `{user}'s picks` becomes "Sarah's picks". That name is their
  **nickname** if you've set one (Users → open someone → "What to call them"), otherwise whatever
  Tautulli calls them, otherwise their Plex username. Which is often a handle nobody uses. Changing
  a nickname renames their existing rows on Plex; it never changes their label, so their privacy is
  unaffected.
- `{top_seed}` — the title that most drove their recommendations. `Because you watched {top_seed}`
  becomes "Because you watched The Bear".
- `{season}` and `{season_emoji}` — on a [seasonal row](#seasonal-rows) only, the season it's in and
  its emoji. `{season_emoji} {season} picks` becomes "🎃 Halloween picks" in October and "🎄 Christmas
  picks" in December. A row that follows no season can't use them, and Shortlist refuses the save.

Two rows can have the same name as long as they never build in the same library — a movies-only
row and a TV-only row can both be called "Picked for You". Two rows that could land in one library
can't share a name, because Plex would hold them as a single collection there; Shortlist refuses the
save and tells you which row already has it. Rows set to "every library of this type" count as
reaching libraries you add later, so pick specific libraries if you want to reuse a name.

The seed is the strongest pick that came from something they watched. Some sources suggest a title
without following one — what's trending, what's popular on your server, a web-search find — so those
contribute picks but no seed. The name uses the strongest pick that has one. If none has one — say
their newest film has no look-alikes in your library, so the row was filled from discover and web
search — the name uses the watch the row was built from.

In a row covering Movies and TV, each library is named after its own watch: the Movies row after a
film, the TV row after a show. A library borrows the other's name only when the row has no watch of
its type to follow.

If a `{top_seed}` row is built for someone with too little history to have a favourite at all, it
falls back to a clean default ("✨ Picked for You") rather than a half-finished sentence — or you can
have the row not appear for them; see
[People without enough watch history](#people-without-enough-watch-history) below. You can rename any
row at any time in the **Row editor**, and the collection on Plex is renamed in place, so its place
in the shelf and its privacy are preserved.

**A `{top_seed}` row needs the right "Based on" setting to be honest.** By default every row is
built from a person's 30 most recent watches blended together, so a row titled "Because you watched
The Bear" would really be "because you watched these thirty things, one of which was The Bear" unless
you narrow it down. **Row editor → Because you watched → Which watch it's based on → Based on** is
where that lives — see [Because you watched rows](#because-you-watched-rows) below for the options and
what each one means for the name.

There is a server-wide default for how many recent watches every discovery source searches from, in
**Settings → Finding titles → How many recent watches to match**, and a row can override it in the
row's own kind settings (for a Picked for You row that's under **How picks are chosen**; for Watch it
again, under **When their finished titles run out**). The global stops at 5 while a row can go down to
1, because narrowing to one watch is a choice worth making for a single row rather than imposing on
every row at once. **Watches the AI web search looks up** is a slice off the front of that same list,
and caps the AI web-search source alone.

## Because you watched rows

Choose **Because you watched** as a row's kind — or start from the _Because you watched…_ template —
and it names the row after one recent watch and fills it with things like that title. Its **Which
watch it's based on** block decides which watch (or watches) that is.

**Based on**, for a row covering both Movies and TV:

- **Their latest film and their latest show** — one watch of each. The row's name is genuinely about
  that one film or show, not an average of many.
- **Only the very last thing they watched** — a single watch, film or show, whichever is more recent.
- **A blend of their last N watches** (3 or more) — several recent watches blended into one set of
  suggestions. The row still names the strongest of them (see [Naming a row](#naming-a-row) above),
  but the further past 2 you go, the less that name describes what actually built the row. A blend
  can't take turns. A row whose name uses `{top_seed}` can also leave N to the global default with
  **Use the global default** beside it, so it follows whatever Settings says.

A row covering only Movies (or only TV) instead offers **Their latest film** (or show) — a single
watch — or **A blend of their last N films** (2 or more).

**One catch, which the editor also tells you:** choosing **Only the very last thing they watched** on
a row covering both Movies and TV has the same limit it always did — a single watch is either a film
or a show, never both, so only one of the two libraries gets seeded and the other's collection never
builds. Use **Their latest film and their latest show** for a row covering both, or split the row to
Movies only or TV only.

**Take turns between their last [N] watches** cycles which single watch the row follows: instead of
sticking to the most recent one until it's replaced, the row works through the last N (up to 20),
moving on by one each day and coming back around once it reaches the end. It cycles rather than
picking at random, because a random pick can repeat, and a repeat looks exactly like a row that's
stopped working. Two people's rows cycle out of step, so a whole server doesn't rebuild the same
night. This setting is always shown, but it only makes sense when the row follows a single watch, so
it's enabled while **Based on** is one of the single-watch options above and disabled — with a note
explaining why — for a blend of 3 or more.

**Name for someone who's new** sets what the row is called for a person with too little watch history
to have a `{top_seed}` at all — a plain fallback like "✨ Picked for You" rather than a half-finished
sentence. See [People without enough watch history](#people-without-enough-watch-history) below for
the rest of what that setting controls.

**A Because you watched row refreshes nightly when its name uses `{top_seed}`, or when it takes turns
between more than one watch.** A row whose title claims a recent watch can't be allowed to lag behind
it — at the usual pace it would go on naming last week's film for a week after the person moved on —
and a row that takes turns has a different watch to follow each day. So in either case **How often
it changes** isn't offered; the row simply picks new titles every night. A Because you watched row
with neither — a plain name and one watch at a time — keeps **How often it changes**, and refreshes
at whatever pace that says.

There is a modest cost to that. Refreshing nightly does not only mean "a write when the watch
changes". On the nights it hasn't changed the row still swaps its weakest third for new titles, so it
writes to Plex most nights, per person, per library, where an ordinary row on the default pace writes
about weekly. It does not cost any extra AI usage: candidates are gathered once per run however often
a row refreshes, so how often a row refreshes has no bearing on it.

## Watch-it-again rows

Choose **Watch it again** as a row's kind — or start from the _Watch it again_ template — and the row
is built from what each person has already **finished** in that library — not from titles similar to
what they watch. New suggestions only fill the row when they haven't finished enough titles.

What leads the row:

1. Titles they rated 4 stars or more in Plex, highest first (when Plex ratings are switched on).
2. Titles close to what they've been watching lately.
3. Whatever they've gone longest without seeing.

What stays out: anything they finished in the last **30 days** (change it with **Skip titles
finished in the last**, or set 0 to allow everything), titles they rated low (again, only when Plex
ratings are on), and genres you excluded for them. Someone with too little history still gets their
finished titles first, with the server's top-rated titles filling any room left.

### When their finished titles run out

Once a person's finished titles for the row run dry, the rest of the row is filled the same way a
Picked for You row is — the Row editor groups those settings under **When their finished titles run
out**: **How many recent watches to match**, **Sources**, and **Recent releases** — plus **Take turns
between their last [N] watches** while the watch count is 1 or 2, since those new picks can follow one
watch at a time just as a Because you watched row does. Two settings that
make sense for new suggestions don't apply here and don't show: **Already-watched titles** (the whole
point of this kind is titles they HAVE watched) and **Only series they haven't started** (the opposite
of a rewatch row). A fill-up title can still be requested if your library doesn't have it — see
[Requests on a row](#requests-on-a-row) below.

## Your requests rows

**Development preview:** this row kind is available on `:dev`, not stable {{ site.stable_version }}.

Choose **Your requests** as a row's kind — or start from the _Your requests_ template — and each
person gets a private row of the titles **they** asked for that are now on Plex and they haven't
watched yet, newest arrival first. Nothing is recommended, ranked or padded, and no AI is involved:
the row is exactly what they asked for, or nothing. A title leaves the row once they've watched it,
and a person with nothing ready has no row at all — theirs is taken off Plex rather than left holding
titles they've already seen.

**Where requests are read from.** Two sources, both read whenever their address and key are filled
in under **Settings › Connections** — whether or not Shortlist's own requests are switched on, and
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

## Seasonal rows

A seasonal row follows the calendar. In October it holds Halloween films and horror, in December
Christmas films, and before Valentine's Day romance. Between seasons it is hidden. Start from the
_Seasonal_ template, or choose **Seasonal** as a row's kind in the Row editor.

Seasonal is a schedule wrapped around one of the other four kinds, not a fifth way of picking titles —
see [Row kinds](#row-kinds) above. Picking Seasonal ticks the three built-in seasons to start, then shows
**How it's filled**: Picked for You, Because you watched, Watch it again, or Popular on this server.
Whichever you choose, that kind's own settings (described elsewhere in this guide) show underneath.

**The seasons**

Three seasons are built in. You can add ready-made ones for other holidays, or make your own.

| Season          | Its day | What the row holds                                                                          |
| --------------- | ------- | ------------------------------------------------------------------------------------------- |
| Valentine's Day | 14 Feb  | Films TMDB tags for Valentine's Day, and romance                                            |
| Halloween       | 31 Oct  | Films TMDB tags for Halloween (not dramas and romances merely set on the night), and horror |
| Christmas       | 25 Dec  | Films TMDB tags for Christmas: Home Alone and Klaus, and also Die Hard                      |

Tick the ones this row follows. Each season is ticked per row, so one seasonal row can follow Halloween
and another Christmas. Built-in seasons use the row's timing: **Built-in seasons show from N days
before and stay N days after** (0–90 before, default 30; 0–30 after, default 0). With the defaults,
Halloween shows 1–31 October and Christmas 25 November – 25 December. Your own seasons carry their own
timing instead (see below). When two windows overlap, the season coming up next wins, and the year
strip under the list draws every ticked season's window and says where two overlap. Weekdays under
**Where and when people see it** narrow a season further.

**Adding a ready-made season.** Open **Add more seasons** and press **Add** on a card. The editor opens
filled in, so you can check it, rename it or change the films before saving. Saving ticks it in the row
you are editing. Ready-made seasons are New Year's Eve, 4th of July, Thanksgiving (US), Thanksgiving (Canada), St
Patrick's Day, Easter, Mother's Day (US, CA, AU, NZ), Mothering Sunday (UK, IE), Father's Day (US, UK,
CA, IE) and Father's Day (AU, NZ). A card shows its region, but the season's name does not, because the name appears in Plex row titles. Season names must
be unique, so to add both regional versions of a holiday, rename one first. A season name is also
refused when it would give a row named after its season (`{season} picks`) the title another row already
has in a library they share, because the two rows would then be one collection on Plex.

**Making your own.** Press **Create your own**. Give it a name and an emoji, then choose when it is:

- a fixed day, such as 17 March;
- the nth or last weekday of a month, such as the 4th Thursday of November;
- a day counted from Easter, up to 63 days either side.

29 February is refused, because the season would skip three years in four. Under **Shows from N days
before / stays N days after** (default 7 before, 0 after) set its window. The editor shows the next
date and the window it gives.

**Where its films come from.** A season's films are the union of four sources. Add at least one:

- **TMDB tags** — search TMDB's keywords (for example "thanksgiving") and add as many as you like.
- **Also include a genre** — optionally include every film of one TMDB genre. Use **Leave out films of
  these genres** to drop genres you don't want (Horror from a St Patrick's Day season, say). Leave-out never drops a film you picked by hand.
- **From your library** — pick Plex collections on your server. They are only read, never changed, and are
  matched by title. A Kometa collection that is absent out of season is normal: the season counts it
  as empty until Kometa recreates it, and the row picks it up from its next refresh.
- **Picked by hand** — search your libraries and add titles one at a time.

Only films that are in your libraries are used, whatever the source.

**Counts and verdicts.** The editor counts what the row you opened it from can draw, as you change
sources: titles of the row's type (films for a films row, shows for a shows row, titles for both) in the
row's libraries. It sets the count against the row's size. A season shows one of three verdicts:

- **Too few films to fill this row (n of size)** — add a tag, a collection or a few films. A row of films
  and shows fills each library from its own type, so it is short when either half is: **Too few shows to
  fill this row's TV library (0 of 15)**.
- **People's rows will be much alike — works best in a shared row** — a per-person row has fewer than
  100 films to draw from, so everyone's row would be nearly the same. This never shows for a shared row.
- **Enough films for this row.**

**When a season finds nothing.** A season can find nothing for a row in one of its libraries: a thin
season everyone has already watched, a TV library no tag reaches, or a Kometa collection that does not
exist yet. The row then builds nothing there, and that library's collection still holds the previous
season's films under its title. Shortlist keeps that collection hidden, as it does between seasons,
until a run builds the new season into it. It is never deleted. The same goes for a collection built for
another year's showing, or for a day you have since moved your season away from. Moving a season's day
within the window it is showing in keeps its row on screen, and the row is rebuilt for the new day at its
next run.

**Editing and deleting.** Built-in seasons can't be edited or deleted. Deleting one of your own seasons
unticks it in every row that follows it. If it is a row's only season, Shortlist refuses and names the
rows: give them another season, or delete them, first.

**What goes in it.** The season's films that are on your server, ranked for each person: films close to
what they watch lead the row, and the rest are weighed by how well their genres fit that person's
viewing. So someone who watches thrillers gets Violent Night and Die Hard before a Christmas romance, and
a family that watches animation gets Casper and Hocus Pocus rather than slasher films. The row changes
every night it rebuilds (the template sets nightly), keeping the strongest two-thirds. The template
also ignores release dates, because seasonal favourites are mostly old: on a real server the Christmas
films people actually watched had a median release year of 2008.

**Between seasons** the row is taken off every Plex screen but kept, so it comes straight back — the
same way a row's days off work. On the night before a season opens, the nightly run builds the row
for that season while it is still hidden, and it appears at midnight on the season's first day.
Leaving a season hides it at midnight too. A row whose schedule isn't nightly can't do this on time,
and the editor says so.

**Films only, by default.** TMDB tags few shows for a season (on a 5,000-show library, 13 for Christmas
and 2 for Halloween). A seasonal row that also covers TV keeps last season's TV collection up through any
season with nothing to put in it, so the template leaves TV out.

**Per person or shared.** A shared seasonal row is the season's films that the most people on your
server have watched, with the same watched-by-at-least floor as any shared row.

Two things behave differently on a seasonal row:

- **AI web search is skipped.** It searches from what someone watched, not from the season, so almost
  everything it proposed would be thrown away after you'd paid for it.
- **People without enough watch history** get the season's best-rated films on your server, not the
  server's overall top-rated.

The default row can't follow seasons: its name is the one every person's everyday row uses. Add a
seasonal row beside it instead.

Shortlist reads each season's list from TMDB once a run (a few hundred requests the first time, then
cached for a week), and only while a seasonal row is in, or about to start, its season. On a night the
list can't be read, the row keeps what it has, like a row whose sources are all down: the run names it,
and marks a person as failed only if every row they had to build that night was left with nothing to
build from.

## People without enough watch history

Someone new to the server, or someone who barely watches, has too little history for Shortlist to
recommend from. **Settings → Finding titles → Enough watch history** is where that line sits
(10 watched titles by default), and the setting beneath it decides what those people get:

| Choice                                     | What lands on their Plex                                                                                                  |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| **Show the server's highest-rated titles** | They still get the row, filled with what rates highest on this server. This is the default.                                 |
| **Don't build their row**                  | No row is created — and any row they already have is **removed**, so "skip" means gone rather than left to go stale.      |

Either way it resolves itself: the row appears (or returns) on its own the night they cross the
threshold. Nothing needs setting back.

**Any row can override this in the Row editor**, which is the point of having it per row rather than
only server-wide. A `{top_seed}` row is the one worth skipping — it has no favourite to name itself
after, so for a cold-start person it falls back to the plain default title. A general
"Picked for You" row is perfectly happy holding popular titles in the meantime. Leave a row on **Use
the global setting** and it follows Settings.

The person still appears on the Users page, flagged as needing more watch history — a row that is
missing on purpose is never left looking like a failure. The run's entry for them says how many
titles they have watched so far and what was skipped. Shared rows are unaffected: this only governs
the per-person ones.

## Requests on a row

A per-person row (any kind except Popular on this server, which never requests anything missing — see
[Shared rows](requests.md#shared-rows)) can override the server-wide request settings in its own
**Requests** group in the Row editor. Full detail — including how rows share the run's request limit,
and what happens when two rows both want the same title — is in
[Requests → Different settings per row](requests.md#different-settings-per-row). Three things worth
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

## The order titles appear in

**Row editor → Order** decides how a row's titles are arranged in Plex:

| Order               | What you get                                                                 |
| ------------------- | ---------------------------------------------------------------------------- |
| **Best match**      | Strongest suggestions first, by how well each title matches their viewing    |
| **Highest rated**   | Highest score first, from whichever service you configured                   |
| **Newest released** | Most recently released first                                                 |
| **Shuffled**        | A different order every day, from the same titles                            |
| **Just added**      | Whatever is new to the row goes to the front, the rest follow in match order |
| **Taking turns**    | The front moves along by one title a day, so every pick gets a turn there    |

Plex itself only sorts a collection by release date, alphabetically, or by a custom order, so every
one of these is applied by Shortlist and delivered as that custom order, which is what the Home row
displays.

**Newest released** and **Just added** are different things, and the difference matters: the first is
about when a film or show came out, the second about when it joined this row.

**Highest rated** uses TMDB by default, which needs no setup. To sort on IMDb, Trakt, Rotten Tomatoes
or Metacritic instead, set **Settings → Finding titles → Rate titles using**. Those come from MDBList
and need its API key (the same one the Requests feature uses). Without a key, or once MDBList's daily
quota is spent, the row falls back to TMDB for its _whole_ ordering rather than sorting half the row
on one scale and half on another.

**Shuffled** and **Taking turns** are the two with a cost worth knowing about. The other four are
applied while the row is being written anyway, so they are free; these two reorder the row on Plex
every day, including days when nothing about the row has changed. The whole row is ordered, and it is
one Plex write per title actually out of place, per person, per night — so a row that barely moved
costs a handful of writes and a row that turned over completely costs one per title. On a server with
many people that is real write volume, so neither is on by default.

Both are stable within a day. Re-running a row the same night reproduces the same order, and two
people's copies of one row shuffle differently.

**Just added** only moves on the nights a row actually refreshes — on the other nights nothing has
arrived, so there is nothing to put in front. How often that happens is **How often rows rebuild**,
not this setting. If the front of a row feels stuck, the rebuild cadence is usually the dial you
want, and **Taking turns** is the one that moves the front every night regardless.

## Where a row shows

The **Row editor** → **Where it shows** grid picks which Plex screens a row appears on. Two
surfaces, two audiences, and every one of the four switches is independent:

|                       | You | Everyone else |
| --------------------- | --- | ------------- |
| **Recommended shelf** | ☑   | ☑             |
| **Home screen**       | ☑   | ☑             |

The columns are real, not cosmetic: every person gets their **own** Plex collection, so each switch
is set on a different collection. **You** is your own row, and Plex's Home shelf applies to the server
owner alone. **Everyone else** covers the people you've shared with plus Plex Home members, whom
Plex groups together under Shared Users' Home. Each of them only ever sees their own row; everyone
else's is excluded from their share filter.

Turn all four off and the row still gets built and kept private; it claims no Recommended slot, and
you'll find it under the library's **Collections** tab. One caveat: on a run where Shortlist can't
match an existing collection back to its row (a `{top_seed}` row that produced no picks, say) that
row keeps its own Home flag for that run. It stays off the Recommended shelf, and it is only ever
visible to the person it belongs to.

**What this can't do:** hide friends' rows from _your_ Recommended shelf while leaving them
on theirs. Share filters are what hide a row from someone, and you own the server, so there is no
share with yourself to attach one to. So with **Everyone else → Recommended shelf** on, every friend's
row is on your shelf too. Turn it off (leaving **Everyone else → Home screen** on) and each friend still
gets their row on their own Home, while your shelf stays yours. Shortlist shows this warning at the
switch itself.

A common setup: **You** both on, **Everyone else** Home only. You get your row on your Home and your
shelf, everyone else gets theirs on their Home, and nobody's row clutters anybody else's view.

## Row placement (Recommended shelf)

By default Plex adds new collections at the **end** of a library's _Recommended_ shelf, so if another
tool (like **Kometa**) manages collections on the same server, Shortlist's rows can end up buried at
the bottom.

Each row chooses its own spot, per library, in the **Row editor** under **Where it sits** on stable, or **Plex placement** in the development preview:

- **Top of the shelf** — the default, and the one position that always works.
- **Right after / before a collection**. Pick an existing collection and sit the row next to it. It
  has to be a collection that is actually showing on one of that library's shelves — one switched off
  in Plex's *Manage Recommendations* has no position to sit beside, and Shortlist tells you so rather
  than guessing a spot.
- **Right after / before another Shortlist row**, so "Because you watched" can follow "Picked for
  You" wherever that ends up — including when that row is itself anchored to a collection. The other
  row needs its own position switched on in that library too: Shortlist can only hold two rows
  together if it is placing both. If it isn't, this row goes to the top instead, in your Rows order.
- **Don't place this row**. Shortlist never positions it, so it stays wherever Plex put it. Be aware
  that Plex adds new collections at the end of the shelf, so a new row that nothing places starts at
  the bottom.

Settings → **Row placement** now holds one switch, **Let Shortlist order the Recommended shelf**.
Turn it off and Shortlist leaves the order entirely alone. (It used to also hold a per-library
default, which was a second place to set the same thing and disagreed with the engine about what its
own "Wherever Plex puts them" option meant.)

Since each person only sees their own row, moving rows up lifts everyone's at once.

Behind the scenes Shortlist re-applies your choice at the end of every run, and checks the shelf
first — if it is already right, it writes nothing at all.

When it does have work to do, it rebuilds the whole shelf in one pass, so other tools' rows are
moved too. Their order **relative to each other** is preserved exactly; they shift only as far as
placing your rows among them requires. Nothing else about them is touched — no collection is edited,
renamed or promoted, only positions. The reason it works this way is the next section.

### Why every move goes to the bottom

Plex stores each row's shelf position as a decimal number, and "put this row after that one" works by
picking the number halfway between two neighbours. Halve a gap fifty times and there is no number
left that fits: from then on Plex accepts every move and applies none, for every tool including its
own web app, until that library's positions are spread out again.

Two moves never halve anything. "To the very top" takes a number below the lowest, and "after
whichever row is currently last" takes one above the highest. The top one is not usable, because a
library's built-in row — "Recently Added" — can hold the lowest number and refuses to be moved, so
everything sent above it lands *on* its number instead. One rebuild done that way collapsed 72 rows
onto a single value.

So Shortlist builds the arrangement from the **bottom**: it walks your wanted order and sends each
row to the end of the shelf in turn. The shelf finishes in exactly that order with the numbers spread
1000 apart, which means the pass repairs a library whose numbers have collapsed rather than wearing
it down further.

The trade-off is that it repositions every row on that shelf, not only Shortlist's. Their order
relative to each other is preserved exactly — the only thing that changes is where Shortlist's rows
sit among them — and it is the only way to honour "put my row after that collection" without the
halving insert. Rows that are on no shelf at all are left alone.

### If you also run Agregarr

Agregarr arranges the same shelf, and it re-applies its own stored order roughly every 30 minutes.
Shortlist applies yours once, at the end of the nightly run, and does nothing at all if the shelf is
already right. So Agregarr wins on volume: what you see during the day is Agregarr's layout.

There are two ways to settle it, and both are configuration rather than something Shortlist can do
for you:

- **Exclude Shortlist's rows in Agregarr**, or stop its "Randomize Home Order" job, so it stops
  moving collections labelled `shortlist_*`.
- **Turn off "Let Shortlist order the Recommended shelf"** so Shortlist never touches the order and
  Agregarr owns it outright.

Either way your rows are still built, delivered and kept private — only their position on the shelf
is affected. Shortlist tells you when this is happening: three passes moving the same row in a day
raises the "Something else is reordering your shelf" notification.

#### Check which Agregarr you are running

The original at `agregarr/agregarr` is no longer actively released, and it has a bug that matters
more than the ordering one: when it reorders a shelf it re-promotes every collection with Plex's
defaults, which puts them on the **server owner's Home** — your Home, the one place no share filter
can hide a row, because the owner account has no share filter.

Shortlist clears that flag on every run, so it is a gap between runs rather than something
permanent — but the gap is as long as the time between your runs.

The maintained fork at [bitr8/agregarr-dev](https://github.com/bitr8/agregarr-dev) (Docker image
`bitr8/agregarr`) fixes it at the source. It is a drop-in swap — same config, same database. It
still reorders the shelf, so the two options above still apply.

## Row posters

Each row can have its own artwork on Plex. In the **Row editor** → **How it looks on Plex** (called **Appearance** in the development preview) → **Poster**, pick one of:

- **Plex default** — leave Plex's own collection artwork alone (the default). Switching a row _back_
  to this after it had a custom poster reverts the artwork on Plex on save.
- **Upload** — upload your own image (a tall 2:3 poster looks best; up to 8 MB). It's downscaled and
  stored, then applied to the row's collection(s) on the next run.
- **Text** — a clean built-in poster: your **Title** and **Subtitle** over a gradient. No AI needed,
  works on any setup. Use `{user}`, `{library_name}`, and `{top_seed}` to personalise the text.
- **AI image** — an image generated from your text and **Art style**, using your AI provider's image
  model. This reuses your AI provider's key, so it's available when that provider is **OpenAI** or
  **Google** (Anthropic and local servers can't generate images. Use a Text poster or Upload instead).

Hit **Preview** to see a sample before saving. Generated images are made once and reused across
runs (they refresh when you change the text or style), so posters don't slow a run down or cost per
user. Posters are cosmetic. A poster that can't be made never blocks a row from building.

## Description and sort order

Each row can also set two of its Plex collection's own fields in the **Row editor**:

- **Description**, under **How it looks on Plex** (**Appearance** in the development preview) — the summary Plex shows when someone opens the
  row. It fills in `{user}`, `{library_name}` and `{top_seed}` the same way the row's name does, so
  every person's copy can say something about them. A `{top_seed}` description for someone with
  nothing watched is left empty. The **On Plex** card beside it shows the name, description and
  poster filled in for a sample person.
- **Sort title prefix**, under **Where and when people see it** — text put in front of the row's name to make its Plex sort title, such as
  `!010_`. It decides where the row sorts in the library's **Collections** tab (`!` sorts before
  letters). It does not move the row on Home or the Recommended shelf; that is
  [Row placement](#row-placement-recommended-shelf). The prefix always goes in front of the row's
  current name, so a renamed row, or one named after `{top_seed}`, keeps sorting under it.

Both are empty by default, and empty means Shortlist leaves that field on Plex alone. Changes reach
Plex the next time the row runs, and replace whatever that field held — including a value another
tool set. Clearing a field hands it back to Plex: no description, and a sort title Plex builds from
the row's name. That only happens where Plex still holds the value Shortlist wrote; if someone changed
it in Plex or another tool since, it is left alone. A value another tool had before Shortlist set the
field is not brought back.

Set each field in one tool only. Agregarr and Kometa can set a collection's summary and sort title
too, and two tools setting the same field overwrite each other on every sync. If Shortlist sets them,
leave them unset for Shortlist's rows in the other tool. Agregarr can skip Shortlist's rows entirely
if you add the `shortlist` label to its excluded labels.
