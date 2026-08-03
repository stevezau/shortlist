---
title: Shortlist vs netplexflix, SuggestArr and Seerr
description: Where Shortlist sits among the self-hosted Plex recommendation and request tools, what problem each one is built for, and when Shortlist is the wrong choice.
heading: How Shortlist relates to other Plex tools
---

Several self-hosted tools touch recommendations for Plex, and they mostly solve different halves of
the problem rather than competing. This page is a map of where Shortlist sits — including the cases
where it's the wrong tool.

> **On accuracy:** every project below is described by **its own stated purpose**, with a link so you
> can check rather than take this page's word for it. Feature sets move fast and this page will not
> try to keep a scorecard of what other tools can and can't do — that's how comparison pages end up
> quietly wrong and unfair. If something here misrepresents your project,
> [open an issue]({{ site.repo }}/issues/new/choose) and it'll be fixed.

## What Shortlist does

One narrow thing: for **each person** on your Plex server, build a set of recommendations from
**their own** watch history, and put it on **their** Plex home screen as a row **only they can see**.

The per-person privacy is the part that's hard, and it only became possible in 2026 — Plex fixed
label hiding on the Home and Recommended shelves in 1.43.1 and on Related rows in 1.43.2. The
mechanism is worth understanding whether or not you use Shortlist:
[How to make a Plex collection visible to only one user](plex-per-user-collections.md).

## The other tools, and what they're for

### netplexflix — [Movie-](https://github.com/netplexflix/Movie-Recommendations-for-Plex) and [TV-Show-Recommendations-for-Plex](https://github.com/netplexflix/TV-Show-Recommendations-for-Plex)

Describes itself as analysing your Plex watch history to recommend unwatched titles, optionally
labelling them in Plex and adding them to Radarr/Sonarr.

**Closest in intent to Shortlist.** The clearest difference in approach: netplexflix is a pair of
scripts you run, and what you do with the labels afterwards is yours to arrange — including any
per-user sharing configuration. Shortlist is a service with a UI whose whole job is owning that
sharing configuration, because getting the write ordering wrong is what leaks a "private" row to your
whole server. If you'd rather drive this from files and a cron and configure Plex yourself,
netplexflix is a reasonable fit.

### [SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr)

Describes itself as reading recently-watched content and automatically requesting similar titles via
Radarr/Sonarr/Jellyseerr.

Its output is **downloads**. It's aimed at growing the library. Shortlist's output is **a row people
see**, aimed at surfacing what's already on your shelves. Shortlist can also request missing titles,
but that's off by default and secondary. If your problem is "my library doesn't have enough of what
my users like," that's SuggestArr's problem, not Shortlist's. They work fine together.

### [Seerr](https://github.com/seerr-team/seerr) (the merged Overseerr / Jellyseerr project)

A request-management application where users sign in and ask for titles they want.

It lives at **its own URL**, and it's request-driven — someone has to already know what they want and
go somewhere to ask for it. Shortlist assumes the opposite: the person is staring at Plex with no
idea what to put on, so the answer goes where they already are. Different question; plenty of servers
run both.

### [Kometa](https://kometa.wiki/) (formerly Plex Meta Manager)

The standard for building Plex collections and applying metadata and artwork from YAML.

Kometa's collections are **the same for everyone**, which is exactly right for a curated "90s Action"
or "Oscar Winners" shelf. Shortlist builds a different collection per person and hides it from
everyone else. **They coexist by design** — Shortlist only modifies collections carrying its own
`shortlist_*` label, and its share-filter writes are merges that leave existing conditions
exactly as they were.

### [Tautulli](https://tautulli.com/)

Monitoring and reporting for Plex watch activity.

Tautulli tells you what **was** watched; Shortlist decides what to watch **next**. Shortlist reads
watch history straight from Plex and doesn't require Tautulli — connecting it is optional and only
improves the names people are displayed under.

### Plex's own "Recommended for You"

Plex's built-in discovery shelves are server-wide and weighted toward Plex's own streaming catalogue,
so they routinely surface things you don't own. Shortlist is per-account and built only from titles
actually in your library. Per-user recommendations from watch history is a long-standing
[feature request on the Plex forums](https://forums.plex.tv/t/feature-request-recommendations-for-each-individual-user-ideally-based-on-watch-history/835212).

## When Shortlist is the wrong choice

Being straight about this is more useful than a feature table:

- **You run Jellyfin or Emby.** Shortlist is Plex-only. The privacy model depends specifically on
  Plex's label-based share filters, and there's no equivalent to port to.
- **You can't run Plex Media Server 1.43.2.10687 or newer, or you don't have Plex Pass.** Label
  hiding is a Pass feature, and older servers ignore the restriction on Home and Recommended shelves —
  so a "private" row would not actually be private.
- **You're the only user and you don't mind server-wide collections.** The per-user privacy engine
  isn't earning its keep. Shortlist still works — the owner gets a row like anyone else — but a
  simpler tool will do.
- **You want recommendations for titles you don't own.** Shortlist recommends from your shelves, and
  only optionally asks Radarr/Sonarr for the gaps.

## Related reading

- [Getting started](getting-started.md) — requirements and the Docker install
- [FAQ](faq.md) — privacy model, Kometa coexistence, uninstalling
- [How to make a Plex collection visible to only one user](plex-per-user-collections.md)
