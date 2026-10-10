---
title: Plex recommendations based on each user's watch history
description: Plex's library rows are identical for everyone and ignore what you've watched. What Plex really does with watch history, why smart collections aren't personal, how to build Netflix-style rows that are, and what good looks like.
heading: How to get Plex recommendations based on each user's watch history
updated: 2026-10-10
byline: true
redirect_from:
  - /plex-netflix-style-recommendations/
---

**Short answer:** Plex records everyone's watch history, but it doesn't use it to build the rows on
your server. Recommended, Home and every pinned collection are library-wide — identical for every
account. Getting rows built from a person's own viewing means reading that history yourself and
creating collections from it.

<figure class="shot shot--pair">
  <picture>
    <source media="(max-width: 600px)" srcset="{{ '/images/home-sarah-sm.webp' | relative_url }}" width="600" height="565">
    <img src="{{ '/images/home-sarah.webp' | relative_url }}" width="1000" height="942"
         alt="Sarah's Plex Home: Movies Picked for You (Fight Club, Parasite, Whiplash, Forrest Gump, Se7en) and TV Shows Picked for You (Severance, The Bear, Succession, Better Call Saul, True Detective).">
  </picture>
  <picture>
    <source media="(max-width: 600px)" srcset="{{ '/images/home-mike-sm.webp' | relative_url }}" width="600" height="565">
    <img src="{{ '/images/home-mike.webp' | relative_url }}" width="1000" height="942" loading="lazy"
         alt="Mike's Plex Home on the same server: TV Shows Picked for You (The Expanse, Peaky Blinders, Ted Lasso, Sherlock, Black Mirror), and none of Sarah's rows.">
  </picture>
  <figcaption>Two accounts, one server, one library. Each row is an ordinary Plex collection carrying its owner's label, such as <code>shortlist_sarah</code>; every other account's sharing filter excludes that label, so Mike never sees Sarah's picks and she never sees his.</figcaption>
</figure>

This page covers what Plex genuinely does, why the advice people are usually given doesn't get them
there, and what reading the history yourself actually involves.

## What Plex does with watch history today

Plex tracks watch state per account, and it does use it — just not where you want it.

**Continue Watching and Up Next are personal.** These are the only genuinely per-user rows on your
server, and they only ever contain things you've already started. They answer "where was I", not
"what next".

**Discover recommendations are personal, but off-server.** If you've enabled it, Plex builds
suggestions from your account activity and shows them under the Discover source. They're mostly
titles on streaming services, not things in your library, and the setting that controls it lives in
your Plex **account** profile rather than your server.

**Library rows are not personal at all.** Recommended, Home shelves, genre rows, "Recently Added",
and anything an admin has published — every account with access sees the same rows with the same
titles. Plex has no per-account library personalisation, and no setting anywhere turns it on.

That last point is the whole problem. On a server with any number of users, the person who has
watched every sci-fi film you own and the person who has watched nothing but comedies are looking at
an identical home screen.

## The one thing smart collections can't do

"Make a smart collection" is the advice you'll get most often, and it is worth doing — a saved filter
like "highly rated thrillers you haven't seen" gives your shelf real shape for no cost. It just
doesn't read anyone's watch history. The filter runs against the library rather than the viewer, so
`Unplayed` means unplayed **by the admin account**, and every user sees the same row whatever they
have watched. [How to improve Plex recommendations](improve-plex-recommendations.md#smart-collections-the-real-ceiling-of-the-built-in-tools)
covers what to build and exactly where it stops.

Everything below is about the thing smart collections can't reach: a row built from one person's own
viewing.

## Read the history and build collections yourself

The data you need does exist and is reachable.

Each account's watch history lives on the Plex Media Server and can be read per user, which is what
every tool in this space is doing under the hood. There are two routes:

- **Ask the Plex server directly, using each share's own access key.** When someone accepts a share,
  that share comes with a key that identifies them, and history read with it is genuinely that
  person's. This is the accurate route, and it's the one Shortlist uses.
- **[Tautulli](https://tautulli.com/).** Tautulli has watched your server for as long as it's been
  installed and exposes per-user history over its API. Widely used and easy to query. Its
  identifiers are display names rather than stable account IDs, which matters if anyone on your
  server has ever renamed themselves or shares a name with someone else.

From there the shape of the job is: take what a person watched, find similar titles **that are
already in your library**, drop anything they've seen, and put the result in a collection. Similarity
usually comes from [TMDB](https://www.themoviedb.org/) — shared genres, keywords, cast, crew — or
from a recommendations service like Trakt, optionally with an AI model ranking the shortlist at
the end.

**Where it stops:** the collection you just built is visible to everyone with access to that
library. You've made a personal row and published it to the whole server, which is both a privacy
problem and a clutter problem — twenty users means twenty rows on everyone's home screen. Fixing
that is a separate mechanism, covered in [per-user collections](plex-per-user-collections.md).

## What "good" looks like

If you're evaluating approaches — your own script or
[someone else's tool](plex-recommendation-tools.md) — these are the things that
actually distinguish a usable result from a demo:

**Picks must exist in your library.** Any approach that asks a language model "what should this
person watch?" and trusts the answer will produce titles you don't own, titles that don't exist, and
titles under slightly wrong names. Generate candidates from your library and use the model to rank
them, never to invent them.

**History has to be per person, not per server.** Reading the admin's history and calling it
everyone's is the most common shortcut, and it produces one recommendation set wearing several
names.

**Rows need to refresh, and to change when they do.** A row rebuilt nightly from an unchanged
history should still shuffle its picks, or people stop looking at it after a week.

**Say why.** "Because you watched _Arrival_" is the difference between a row people trust and a row
that looks arbitrary. It's also how you debug a bad pick.

<figure class="reasons">
  <ul>
    <li><img src="{{ '/images/poster-fight-club.webp' | relative_url }}" width="160" height="240" alt="">
      <span><strong>Fight Club</strong><span class="why">Because you watched GoodFellas — more crime and drama</span><span class="whose">Sarah's movies row</span></span></li>
    <li><img src="{{ '/images/poster-severance.webp' | relative_url }}" width="160" height="240" alt="">
      <span><strong>Severance</strong><span class="why">Because you watched The Wire — more drama and crime</span><span class="whose">Sarah's TV row</span></span></li>
    <li><img src="{{ '/images/poster-the-expanse.webp' | relative_url }}" width="160" height="240" alt="">
      <span><strong>The Expanse</strong><span class="why">Because you watched Breaking Bad — more drama and crime</span><span class="whose">Mike's TV row</span></span></li>
  </ul>
  <figcaption>What each person's reason looks like: the pick, the watch that earned it, and whose row it is in.</figcaption>
</figure>

**Handle the person who's watched nothing.** New users have no history. Falling back to
library-popular or recently-added is fine; producing an empty row is not.

## If what you want is "like Netflix"

Ask for Netflix-style rows and you're usually asking for four separate things: different rows for
different people, a stated reason on each row, rows that change, and rows built from things you can
actually watch right now. Plex gives you the last one for free, because it's your library. The rest
is the work above, plus two details worth stealing.

**Name the row after its reason.** A Plex collection has a title and a summary, both of which show in
the UI, so a row can be called "Because you watched _Arrival_" with a summary explaining each pick.
Naming rows after their seed is most of the Netflix feel for almost no effort.

**Change the row on purpose.** Rebuilding on a schedule isn't enough on its own: the same watch history
scored the same way produces the same row every time. You need either fresh input (new watches, new
library additions) or deliberate variation: rotate which seed drives the row, sample from a larger
candidate pool than the row can hold, or weight recent watches more heavily.

Two limits stay put whatever you build: **the server owner sees every row**, and **films and shows
need separate rows**.
[Per-user collections](plex-per-user-collections.md#two-things-to-watch-out-for) explains why.

{% include seo-closing.html shot="user-detail-crop.webp" shot_w="1453" shot_h="1025"
   shot_alt="Shortlist's page for one person, sarah: 12 titles watched, and her Picked for You row with each pick's reason, such as Fight Club, because you watched drama like GoodFellas."
   shot_caption="The same row from the admin's side: Sarah's picks in Shortlist, each with the watch that earned it." %}

## Related

- [How to improve Plex recommendations](improve-plex-recommendations.md) — the settings to change first
- [AI recommendations for Plex](plex-ai-recommendations.md) — where a model helps, and where it invents films
- [Per-user collections](plex-per-user-collections.md) — making a row only one person can see
- [Plex recommendation tools compared](plex-recommendation-tools.md) — the other projects in this space
- [What goes in a row](guides/picks.md) — tuning sources and seeds in Shortlist
