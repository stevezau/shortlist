---
title: Plex recommendations based on watch history
description: Plex's library rows are identical for everyone and ignore what you've watched. What Plex really does with watch history, why smart collections aren't personal, and how to build rows that are.
heading: How to get Plex recommendations based on watch history
---

**Short answer:** Plex records everyone's watch history, but it doesn't use it to build the rows on
your server. Recommended, Home and every pinned collection are library-wide — identical for every
account. Getting rows built from a person's own viewing means reading that history yourself and
creating collections from it.

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
have watched. [How to improve Plex
recommendations](improve-plex-recommendations.md#smart-collections-the-real-ceiling-of-the-built-in-tools)
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

If you're evaluating approaches — your own script or someone else's tool — these are the things that
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

**Handle the person who's watched nothing.** New users have no history. Falling back to
library-popular or recently-added is fine; producing an empty row is not.

## The automated version

[**Shortlist**](https://github.com/stevezau/shortlist) is a self-hosted container that does this for
every user on your server, on a schedule. It reads each person's own watch history through their
own share's access key, finds similar titles verified to exist in your library, and builds them a
"Picked for You" collection that only they can see.

Every pick carries its reason. AI is optional — the built-in picker runs entirely in code with no
keys and no cloud — and if you do enable a provider (Claude, GPT, Gemini, or a local model via
Ollama, llama.cpp, LM Studio, vLLM or LocalAI) it only ever ranks and explains candidates drawn from
your library.

```bash
docker run -d --name shortlist -p 5959:5959 \
  -v /path/to/config:/config \
  stevezzau/shortlist:latest
```

The doubled **z** in `stevezzau` is deliberate — it's the project's Docker Hub account, not a
typo. The same image is on GHCR as `ghcr.io/stevezau/shortlist`.

Set `-e SHORTLIST_DRY_RUN=1` to see every change it would make without writing one.

## Related

- [How to improve Plex recommendations](improve-plex-recommendations.md) — the settings to change first
- [AI recommendations for Plex](plex-ai-recommendations.md) — where a model helps, and where it invents films
- [Per-user collections](plex-per-user-collections.md) — making a row only one person can see
- [A different home screen per user](plex-per-user-home-screen.md) — the surfaces rows appear on
- [Plex recommendation tools compared](plex-recommendation-tools.md) — the other projects in this space
- [What goes in a row](guides/picks.md) — tuning sources and seeds in Shortlist
