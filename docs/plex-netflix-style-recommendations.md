---
title: Netflix-style recommendations for your Plex library
description: What makes Netflix's rows feel personal, which of those properties Plex can reproduce, and how to build per-person recommendation rows from your own library.
heading: How to get Netflix-style recommendations on Plex
---

**Short answer:** the thing that makes Netflix feel personal isn't the algorithm — it's that every
profile gets different rows, each with a reason attached, refreshed constantly. Plex can reproduce
all three, but none of them out of the box.

This page breaks "make it like Netflix" into the properties people actually mean, and says which are
achievable on a Plex server and how.

## What people mean by "like Netflix"

Ask for Netflix-style rows and you're usually asking for four separate things:

1. **Different rows for different people.** Your profile and your partner's profile share a library
   and show completely different things.
2. **Rows with a stated reason.** "Because you watched _Arrival_", "Top picks for Sam". The label is
   half the value — it makes the row feel chosen rather than random.
3. **Rows that change.** Not the same twelve posters every time you open the app.
4. **Rows built from things you can actually watch right now.** Not a wishlist.

Plex gives you exactly one of these for free: number four, trivially, because it's your library.
The rest is work.

## What Plex gives you, honestly

**Recommended shelves** are library-wide. Genre rows, recently added, unwatched — everyone with
access to that library sees the same rows containing the same titles in the same order, whatever
they've watched. This is the single biggest gap between Plex and Netflix and there is no setting
that closes it.

**Discover** is genuinely personalised to your Plex account, but it recommends across streaming
services rather than from your library, and it's account-level rather than something a server admin
configures.

**Continue Watching** is per-user, and only contains what you've already started.

**Smart collections** get you the _shape_ of Netflix rows cheaply. "Highly rated thrillers",
"90s action", "under 100 minutes" — saved filters that stay current as the library grows, promotable
to the Home shelf. Genuinely worth doing, and where most people should start. But the filter runs
against the library, not against the viewer: `Unplayed` means unplayed by the admin account, so
everyone sees the same row including the people who've watched all of it.

That's the ceiling of the built-in features. Rows that look right, contents that aren't personal.

## Building the four properties

### Different rows for different people

This is the hard one, and it isn't a recommendation problem — it's an access-control problem.

Plex evaluates **label restrictions** per account. Label a collection, then add
`label!=<that label>` to every _other_ account's share filter, and you're left with a collection one
person can see. It needs Plex Media Server **1.43.2.10687** or newer; before that the restriction was
ignored on the Home, Recommended and Related shelves and the row leaked.

Get the ordering wrong and it leaks anyway: create the row unpromoted, label it, merge the
exclusions everywhere, _then_ promote. Full mechanism, manual steps and pitfalls on
[per-user collections](plex-per-user-collections.md).

### Rows with a stated reason

Two parts: having a reason, and being able to say it.

Having one falls out of how you pick. Start from a title the person actually watched and finished,
find similar titles in your library through [TMDB](https://www.themoviedb.org/) similarity — shared
genres, keywords, cast, crew — and the seed _is_ the reason. Ask a language model for
recommendations cold and you get plausible titles with no traceable reason and, frequently, films you
don't own.

Saying it is easier than it looks. A Plex collection has a title and a summary, both of which show
in the UI, so a row can be called "Because you watched _Arrival_" with a summary explaining each
pick. Naming rows after their seed is most of the Netflix feel for almost no effort.

### Rows that change

A recommendation row that shows the same posters for a fortnight becomes invisible. Rebuilding on a
schedule isn't enough on its own — an unchanged watch history run through a deterministic scorer
produces an identical row. You need either fresh input (new watches, new library additions) or
deliberate variation: rotate which seed drives the row, sample from a larger candidate pool than the
row can hold, or weight recent watches more heavily.

### Rows from things you can watch now

Verify every pick exists in your library before it goes in a collection, and drop anything the person
has already watched. This sounds obvious and it's the most common failure of the AI-first approach —
a model asked "what should Sam watch?" will confidently return titles you don't own, titles under
alternate release names, and occasionally titles that don't exist.

The fix is ordering: generate candidates from your library, then let the model rank and explain them.
Never let it invent.

## Where this leaves you

You can get most of the Netflix experience on a Plex server. Rows per person, labelled with why,
refreshed nightly, drawn from what you own. What you can't get is any of it from Plex's settings —
it's the label mechanism plus a scheduled job, and the scheduled job has to be careful about write
ordering or it publishes people's private rows to the whole server.

Two limits stay put whatever you build: **the server owner sees every row** (Plex doesn't filter the
admin account — there's no share to filter), and **films and shows need separate rows** because label
restrictions are tracked per library.

## The automated version

[**Shortlist**](https://github.com/stevezau/shortlist) is a self-hosted container that builds all four
properties for every user on your server. Per-person rows from each person's own watch history, named
after the film that inspired them, refreshed on a schedule, every pick verified to exist in your
library — and made private through the label mechanism, in the order that doesn't leak.

No AI key required: the built-in picker runs entirely in code. An optional provider (Claude, GPT,
Gemini, or a local model through Ollama, llama.cpp, LM Studio, vLLM or LocalAI) adds a ranking and
explanation pass over candidates already drawn from your library.

```bash
docker run -d --name shortlist -p 5959:5959 \
  -v /path/to/config:/config \
  stevezzau/shortlist:latest
```

Set `-e SHORTLIST_DRY_RUN=1` to preview every change without writing one.

## Related

- [How to improve Plex recommendations](improve-plex-recommendations.md) — the settings to change first
- [AI recommendations for Plex](plex-ai-recommendations.md) — where a model helps, and where it invents films
- [Recommendations from watch history](plex-recommendations-watch-history.md) — where picks come from
- [A different home screen per user](plex-per-user-home-screen.md) — the surfaces rows land on
- [Per-user collections](plex-per-user-collections.md) — the privacy mechanism in full
- [Plex recommendation tools compared](plex-recommendation-tools.md) — the other projects in this space
