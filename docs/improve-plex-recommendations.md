---
title: How to improve Plex recommendations
description: What the Manage Recommendations menu actually changes, which Plex settings are worth touching, how far smart collections get you, and what to do when none of it helps.
heading: How to improve Plex recommendations
---

**Short answer:** most of what people try — reordering rows, toggling Manage Recommendations,
rebuilding metadata — changes which rows appear, not what's in them. Plex's library rows aren't
personalised, so no setting makes them better at suggesting things for a particular person. This
page covers what each setting genuinely does, in order of how much it helps.

## First, work out which "recommendations" you mean

Two different systems get called the same thing, and the fix is different for each.

**Rows on your own server** — Recommended, the Home shelf, genre rows. These come from the library.
Everyone with access sees the same rows containing the same titles. Nothing in Plex personalises
them per account.

**Discover** — Plex's account-level suggestions, which _are_ personal to you but mostly point at
titles on streaming services rather than in your library.

If your complaint is "it keeps suggesting things I've already watched" or "it suggests the same
stuff to everyone", you mean the first one, and that's the rest of this page.

## What Manage Recommendations actually does

This is the setting everyone finds first, so it's worth being precise about it.

Open a library → **Manage Recommendations**. You get the list of rows on the Recommended shelf, and
you can reorder them, hide them, and choose whether each appears on Home, on the library's
Recommended tab, or both.

**What it changes:** which rows exist and in what order.

**What it does not change:** what's inside them, or who sees what. It's server-wide — reorder a row
and you've reordered it for everyone with access to that library.

It's genuinely useful for one thing: **turning off rows that are noise on your server**. If you have
no music videos, hide that row. If "Recently Added" dominates your Home and you'd rather lead with
something else, move it down. That's real improvement, and it takes two minutes. It just isn't
personalisation.

## Settings actually worth changing

In rough order of payoff.

**Hide the rows you never use.** As above. The fastest visible improvement available.

**Turn on "Hide items which are in collections"** (library → Edit → Advanced) if your library is
full of franchises. Twelve Marvel films collapse to one collection poster, and the shelf stops being
a wall of near-identical entries.

**Check your Discover privacy setting** if Discover suggestions look wrong or empty. It lives in
your Plex **account** profile, not in server settings — "recommendations based on watch history and
ratings" is a toggle there. This only affects Discover, not your library rows.

**Rate things.** Plex uses your ratings in some Discover surfaces. It does not use them to rebuild
your library shelves, so don't expect the Recommended tab to change.

**Fix your metadata.** Rows built on genre are only as good as your genre tags. A library where half
the films matched badly will produce bad genre rows no matter what else you do. Worth a pass with
the match/fix-match tool if rows look nonsensical.

## Smart collections: the real ceiling of the built-in tools

This is as far as Plex alone goes, and it's further than most people get.

A smart collection is a saved filter that stays current as the library grows, and you can promote it
to Home or Recommended like any other row.

```
Unplayed = true
AND Genre = Thriller
AND Audience Rating >= 7.5
AND Year >= 2015
```

Build a handful of these — "highly rated thrillers", "under 100 minutes", "90s action", "recently
added documentaries" — and your Home shelf goes from an alphabetical wall to something with shape.
For a lot of servers this is enough, and it costs nothing.

**Where it stops:** the filter runs against the library, not against the viewer. `Unplayed` means
unplayed **by the admin account**. So everyone sees the same thriller row, including the people who
have already watched all of it and the people who don't like thrillers. You've organised the
library; you haven't personalised it.

## What none of the above fixes

If you've done all of it and the complaint is still one of these, you've hit the actual limit:

- **"It suggests things I've already seen."** Watched state is per account; library rows are not.
  Only Continue Watching and Up Next follow the viewer.
- **"Everyone on my server sees the same suggestions."** Correct, and there is no setting for it.
- **"It doesn't know what I like."** Nothing on the server side reads one person's history to build
  them a row.

That gap is why a whole category of third-party tools exists. Closing it means reading each person's
watch history yourself, building them a collection from titles already in your library, and — if you
don't want everyone seeing everyone else's row — hiding it from every other account with a label
restriction on their share filter.

The mechanics of each part:

- [Recommendations from watch history](plex-recommendations-watch-history.md) — where the titles come from
- [Per-user collections](plex-per-user-collections.md) — making a row only one person can see
- [A home screen per user](plex-per-user-home-screen.md) — what the rows land on
- [Plex recommendation tools compared](plex-recommendation-tools.md) — which project does which part

## The automated version

[**Shortlist**](https://github.com/stevezau/shortlist) is a self-hosted container that does all of
it on a schedule: reads each person's own watch history, builds them a "Picked for You" row of
titles verified to exist in your library, explains every pick, and makes each row visible only to
its owner.

```bash
docker run -d --name shortlist -p 5959:5959 \
  -v /path/to/config:/config \
  stevezzau/shortlist:latest
```

Add `-e SHORTLIST_DRY_RUN=1` to see every change it would make without writing one.

Needs Plex Media Server 1.43.2.10687+ and a Plex Pass on the admin account. No AI key required —
see [AI recommendations for Plex](plex-ai-recommendations.md) for where a model helps and where it
actively hurts.
