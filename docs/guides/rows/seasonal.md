---
title: Seasonal rows
description: "A seasonal row follows the calendar: Halloween films and horror in October, Christmas films in December, romance before Valentine's Day. Between seasons it is hidden from every Plex screen, and it comes back by itself when its next season opens."
heading: Seasonal rows
updated: 2026-10-03
dev_preview: true
---

<figure class="shot">
  <picture>
    <source media="(max-width: 600px)" srcset="{{ '/images/rows-crop-sm.webp' | relative_url }}" width="792" height="835">
    <img src="{{ '/images/rows-crop.webp' | relative_url }}" width="1560" height="800" loading="lazy"
         alt="The Rows page in Shortlist 1.9.3: four rows, each with an on/off switch, Run now and Edit, and an Add a row button at the top right.">
  </picture>
  <figcaption><strong>Rows → Add a row</strong> opens the template gallery. <em>Seasonal</em> is one of the nine starting templates on stable {{ site.stable_version }}.</figcaption>
</figure>

## Start a seasonal row

Start from the _Seasonal_ template, or choose **Seasonal** as a row's kind in the Row editor.

Seasonal is a schedule wrapped around one of the other four kinds, not a fifth way of picking titles
(see [Row kinds](../rows.md#row-kinds)). Picking Seasonal ticks every season to start, then shows
**How it's filled**: Picked for You, Because you watched, Watch it again, or Popular on this server.
That kind's own settings show underneath.

## The seasons

Three seasons are built in. Tick the ones this row follows. Each row ticks its own, so one seasonal
row can follow Halloween and another Christmas.

<div class="table-scroll table--compact">
<table>
<thead>
<tr><th>Season</th><th>Its day</th><th>Shows (defaults)</th><th>What the row holds</th><th>API value</th></tr>
</thead>
<tbody>
<tr><td>Valentine's Day</td><td class="nw">14 Feb</td><td class="nw">15 Jan – 14 Feb</td><td>Films TMDB tags for Valentine's Day, and romance</td><td><code>valentines</code></td></tr>
<tr><td>Halloween</td><td class="nw">31 Oct</td><td class="nw">1 – 31 Oct</td><td>Films TMDB tags for Halloween (not dramas and romances merely set on the night), and horror</td><td><code>halloween</code></td></tr>
<tr><td>Christmas</td><td class="nw">25 Dec</td><td class="nw">25 Nov – 25 Dec</td><td>Films TMDB tags for Christmas: Home Alone and Klaus, and also Die Hard</td><td><code>christmas</code></td></tr>
</tbody>
</table>
</div>

### When a season shows

**Start showing (days before)** (0–90, default 30) and **Keep it up (days after)** (0–30, default 0)
set each season's window. They give the dates in the table above. When two windows overlap, the
season coming up next wins. Weekdays under **Where and when people see it** narrow a season further.

<details class="dev-preview" markdown="1">
<summary><span class="dev-preview__tag">Development preview</span> Your own seasons, and ten ready-made holidays</summary>
<div class="dev-preview__body" markdown="1">

On the `:dev` image, not stable {{ site.stable_version }}. Everything outside this box applies to both.

**Built-in timing, renamed.** The two window settings read **Built-in seasons show from N days before
and stay N days after**, with the same ranges and defaults, because your own seasons carry their own
timing. The year strip under the list draws every ticked season's window and says where two overlap.

**Adding a ready-made season.** Open **Add more seasons** and press **Add** on a card. The editor opens
filled in, so you can check it, rename it or change the films before saving. Saving ticks it in the
row you are editing. Ready-made seasons are New Year's Eve, 4th of July, Thanksgiving (US),
Thanksgiving (Canada), St Patrick's Day, Easter, Mother's Day (US, CA, AU, NZ), Mothering Sunday (UK,
IE), Father's Day (US, UK, CA, IE) and Father's Day (AU, NZ). A card shows its region, but the
season's name does not, because the name appears in Plex row titles. Season names must be unique, so
to add both regional versions of a holiday, rename one first. A season name is also refused when it
would give a row named after its season (`{season} picks`) the title another row already has in a
library they share, because the two rows would then be one collection on Plex.

**Making your own.** Press **Create your own**. Give it a name and an emoji, then choose when it is:

- a fixed day, such as 17 March;
- the nth or last weekday of a month, such as the 4th Thursday of November;
- a day counted from Easter, up to 63 days either side.

29 February is refused, because the season would skip three years in four. Under **Shows from N days
before / stays N days after** (default 7 before, 0 after) set its window. The editor shows the next
date and the window it gives.

**Where its films come from.** A season's films are the union of four sources. Add at least one:

- **TMDB tags**: search TMDB's keywords (for example "thanksgiving") and add as many as you like.
- **Also include a genre**: optionally include every film of one TMDB genre. Use **Leave out films of
  these genres** to drop genres you don't want (Horror from a St Patrick's Day season, say).
  Leave-out never drops a film you picked by hand.
- **From your library**: pick Plex collections on your server. They are only read, never changed,
  and are matched by title. A Kometa collection that is absent out of season is normal: the season
  counts it as empty until Kometa recreates it, and the row picks it up from its next refresh.
- **Picked by hand**: search your libraries and add titles one at a time.

Only films that are in your libraries are used, whatever the source.

**Counts and verdicts.** The editor counts what the row you opened it from can draw, as you change
sources: titles of the row's type (films for a films row, shows for a shows row, titles for both) in
the row's libraries. It sets the count against the row's size. A season shows one of three verdicts:

- **Too few films to fill this row (n of size)**: add a tag, a collection or a few films. A row of
  films and shows fills each library from its own type, so it is short when either half is: **Too few
  shows to fill this row's TV library (0 of 15)**.
- **People's rows will be much alike — works best in a shared row**: a per-person row has fewer than
  100 films to draw from, so everyone's row would be nearly the same. This never shows for a shared row.
- **Enough films for this row.**

**When a season finds nothing.** A season can find nothing for a row in one of its libraries: a thin
season everyone has already watched, a TV library no tag reaches, or a Kometa collection that does
not exist yet. The row then builds nothing there, and that library's collection still holds the
previous season's films under its title. Shortlist keeps that collection hidden, as it does between
seasons, until a run builds the new season into it. It is never deleted. The same goes for a
collection built for another year's showing, or for a day you have since moved your season well away
from. Moving a showing season's day, earlier or later, by no more than its days before plus its days
after keeps its row on screen, and the row is rebuilt for the new day at its next run.

**Editing and deleting.** Built-in seasons can't be edited or deleted. Deleting one of your own
seasons unticks it in every row that follows it. If it is a row's only season, Shortlist refuses and
names the rows: give them another season, or delete them, first.

[Release channels](../../getting-started.md#release-channels) explains how to switch to `:dev` and
back.

</div>
</details>

## Name it after the season

On a seasonal row, `{season}` and `{season_emoji}` fill in the season it is in. Paste this into the
row's name:

```text
{season_emoji} {season} picks
```

It shows as "🎃 Halloween picks" in October and "🎄 Christmas picks" in December. A row that follows
no season can't use either placeholder, and Shortlist refuses the save. The other placeholders are in
[Naming a row](../rows.md#naming-a-row).

## What goes in it

The season's films that are on your server, ranked for each person. Films close to what they watch
lead the row; the rest are weighed by how well their genres fit that person's viewing. Someone who
watches thrillers gets Violent Night and Die Hard before a Christmas romance, and a family that
watches animation gets Casper and Hocus Pocus rather than slasher films.

The row changes every night it rebuilds (the template sets nightly), keeping the strongest
two-thirds. The template also ignores release dates, because seasonal favourites are mostly old: on a
real server, the Christmas films people actually watched had a median release year of 2008.

### Per person or shared

A shared seasonal row is the season's films that the most people on your server have watched, with
the same watched-by-at-least floor as any shared row.

### Films only, by default

TMDB tags few shows for a season: on a 5,000-show library, 13 for Christmas and 2 for Halloween. A
seasonal row that also covers TV keeps last season's TV collection up through any season with nothing
to put in it, so the template leaves TV out.

## Between seasons

The row is taken off every Plex screen but kept, so it comes straight back, the same way a row's days
off work. On the night before a season opens, the nightly run builds the row for that season while it
is still hidden, and it appears at midnight on the season's first day. Leaving a season hides it at
midnight too.

A row whose schedule isn't nightly can't do this on time, and the editor says so.

## What a seasonal row does differently

- **AI web search is skipped.** It searches from what someone watched, not from the season, so almost
  everything it proposed would be thrown away after you'd paid for it.
- **People without enough watch history** get the season's best-rated films on your server, not the
  server's overall top-rated.
- **The default row can't follow seasons.** Its name is the one every person's everyday row uses. Add
  a seasonal row beside it instead.

## How it reads TMDB

Shortlist reads each season's list from TMDB once a run (a few hundred requests the first time, then
cached for a week), and only while a seasonal row is in, or about to start, its season.

On a night the list can't be read, the row keeps what it has, like a row whose sources are all down.
The run names it, and marks a person as failed only if every row they had to build that night was
left with nothing to build from.
