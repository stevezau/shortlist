---
title: Best Plex recommendation tools in 2026, compared
heading: Best Plex recommendation tools in 2026, compared
description: What each tool actually does, and which ones give every Plex user their own private recommendations.
# Re-checking this page against the projects it names? Change this date. Every tool fact on the page,
# the release dates in the table included, is meant to be true as of it. A claim a project's README
# does not make is "Unknown" in the table and left out of the prose.
facts_checked: 2026-10-03
updated: 2026-10-03
toc: false
---

<section class="answer full" aria-labelledby="short-answer">
<div class="answer__head">
<h2 id="short-answer">Short answer</h2>
<span class="answer__date">As of <time datetime="{{ page.facts_checked | date: '%Y-%m-%d' }}">{{ page.facts_checked | date: "%-d %B %Y" }}</time></span>
</div>
<ul class="verdicts">
<li class="verdicts__ours"><span class="verdicts__tool"><a href="#shortlist">Shortlist</a></span><span class="verdicts__need">For private per-user rows: each person gets their own rows (Picked for You, Because you watched, seasonal and more) on their Plex Home, hidden from every other account.</span></li>
<li><span class="verdicts__tool"><a href="#immaculaterr">Immaculaterr</a></span><span class="verdicts__need">For the richest set of automatic collections, reacting in real time, when it doesn&rsquo;t matter who sees whose row.</span></li>
<li><span class="verdicts__tool"><a href="#curatarr">Curatarr</a></span><span class="verdicts__need">For per-user collections without Docker: standalone binaries for Windows, macOS and Linux.</span></li>
<li><span class="verdicts__tool"><a href="#suggestarr">SuggestArr</a></span><span class="verdicts__need">For getting new content: it requests titles like what you watched through Seerr.</span></li>
<li><span class="verdicts__tool"><a href="#kometa">Kometa</a></span><span class="verdicts__need">For organising the library you already have: rule-built collections, artwork and metadata.</span></li>
<li><span class="verdicts__tool"><a href="#diskovarr">Diskovarr</a></span><span class="verdicts__need">For a separate discovery app your users sign into, on Plex or Jellyfin, with requests built in.</span></li>
<li><span class="verdicts__tool"><a href="#conjurr">Conjurr</a></span><span class="verdicts__need">For AI suggestions per user from Tautulli history, on a web page or in a newsletter. Needs a Google Gemini key.</span></li>
<li><span class="verdicts__tool"><a href="#nextt">Nextt</a></span><span class="verdicts__need">For a what-to-watch dashboard built from your star ratings, on Plex or Jellyfin, with no AI.</span></li>
</ul>
<p class="answer__disclosure"><strong>Disclosure:</strong> Shortlist is my project, so read its row with that in mind. I&rsquo;ve tried to be accurate about everything else and to say plainly where another tool is the better pick.</p>
</section>

There are a lot of these now, they overlap confusingly, and the READMEs all say "personalized
recommendations". The real split is between tools that _add_ content, tools that _organise_ it,
dashboards you open to choose, and tools that put a different row on each person's Plex Home. Only
the last group can be private, and that is where they differ most.

## The comparison

<div class="table-scroll compare full">
<table>
<thead>
<tr>
<th scope="col">Tool</th>
<th scope="col">Per-user rows</th>
<th scope="col">Private (hidden from other users)</th>
<th scope="col">Promotes to Home</th>
<th scope="col">AI optional</th>
<th scope="col">Maintained (last release)</th>
<th scope="col">Pick it if</th>
</tr>
</thead>
<tbody>
<tr class="compare__ours">
<td><a href="#shortlist">Shortlist</a></td>
<td><span class="yes">Yes</span></td>
<td><span class="yes">Yes</span><span class="q">hidden by sharing filters before the row is shown</span></td>
<td><span class="yes">Yes</span><span class="q">only after the hiding is in place</span></td>
<td><span class="yes">Yes</span><span class="q">built-in picker needs no key</span></td>
<td><span class="yes">Yes</span><span class="q">v1.9.3, 27 Sep 2026</span></td>
<td class="compare__pick">each person should get rows nobody else can see</td>
</tr>
<tr>
<td><a href="#immaculaterr">Immaculaterr</a></td>
<td><span class="yes">Yes</span></td>
<td><span class="no">No</span><span class="q">not claimed</span></td>
<td><span class="yes">Yes</span><span class="q">pins rows where that viewer can see them</span></td>
<td><span class="yes">Yes</span><span class="q">Google and OpenAI are optional helpers</span></td>
<td><span class="yes">Yes</span><span class="q">v1.7.10, 4 Sep 2026</span></td>
<td class="compare__pick">you want the most automatic collections and don&rsquo;t mind who sees them</td>
</tr>
<tr>
<td><a href="#curatarr">Curatarr</a></td>
<td><span class="yes">Yes</span></td>
<td><span class="partial">Partial</span><span class="q">label restrictions; its FAQ calls this UI-level, not access control</span></td>
<td><span class="unknown">Unknown</span><span class="q">not stated in its README</span></td>
<td><span class="yes">Yes</span><span class="q">scoring, no AI</span></td>
<td><span class="yes">Yes</span><span class="q">v2.22.0, 21 Aug 2026</span></td>
<td class="compare__pick">you want per-user collections without running Docker</td>
</tr>
<tr>
<td><a href="#diskovarr">Diskovarr</a></td>
<td><span class="partial">Partial</span><span class="q">per-user carousels inside its own app</span></td>
<td><span class="no">No</span><span class="q">not claimed for its Plex collections</span></td>
<td><span class="yes">Yes</span><span class="q">its list collections, set per collection</span></td>
<td><span class="yes">Yes</span><span class="q">scoring, no AI</span></td>
<td><span class="yes">Yes</span><span class="q">v3.3.5, 24 Sep 2026</span></td>
<td class="compare__pick">your users will sign into a separate discovery app</td>
</tr>
<tr>
<td><a href="#conjurr">Conjurr</a></td>
<td><span class="partial">Partial</span><span class="q">per-user lists on its own web page, not Plex rows</span></td>
<td><span class="no">No</span><span class="q">user mode asks only for a Plex email or username</span></td>
<td><span class="no">No</span><span class="q">writes nothing to Plex</span></td>
<td><span class="no">No</span><span class="q">needs a Google Gemini key</span></td>
<td><span class="yes">Yes</span><span class="q">v4.1.0, 2 Oct 2025; commits since</span></td>
<td class="compare__pick">you run Tautulli and want AI picks per user, or for a newsletter</td>
</tr>
<tr>
<td><a href="#nextt">Nextt</a></td>
<td><span class="no">No</span><span class="q">one profile per install</span></td>
<td><span class="no">No</span></td>
<td><span class="no">No</span><span class="q">a dashboard; writes nothing to Plex</span></td>
<td><span class="yes">Yes</span><span class="q">no AI at all</span></td>
<td><span class="no">No</span><span class="q">no releases; last commit Aug 2025</span></td>
<td class="compare__pick">you want a dashboard of picks from your ratings, Plex or Jellyfin</td>
</tr>
<tr>
<td><a href="#seekandwatch">SeekAndWatch</a></td>
<td><span class="no">No</span><span class="q">one taste profile for the server</span></td>
<td><span class="no">No</span></td>
<td><span class="yes">Yes</span><span class="q">Home, Recommended and Friends toggles per collection</span></td>
<td><span class="yes">Yes</span><span class="q">no AI</span></td>
<td><span class="yes">Yes</span><span class="q">v1.6.8, 11 Apr 2026</span></td>
<td class="compare__pick">your household can&rsquo;t choose, or you want a Kometa config builder</td>
</tr>
<tr>
<td><a href="#kometa">Kometa</a></td>
<td><span class="no">No</span><span class="q">library-wide collections</span></td>
<td><span class="no">No</span></td>
<td><span class="yes">Yes</span><span class="q">per collection, for everyone</span></td>
<td><span class="yes">Yes</span><span class="q">no AI</span></td>
<td><span class="yes">Yes</span><span class="q">v2.5.1, 24 Sep 2026</span></td>
<td class="compare__pick">you want to organise the library you already have</td>
</tr>
<tr>
<td><a href="#suggestarr">SuggestArr</a></td>
<td><span class="no">No</span><span class="q">it adds files, not rows</span></td>
<td><span class="no">No</span></td>
<td><span class="no">No</span></td>
<td><span class="yes">Yes</span><span class="q">AI ranking is an optional beta</span></td>
<td><span class="yes">Yes</span><span class="q">v2.15.0, 15 Sep 2026</span></td>
<td class="compare__pick">you want new content added automatically</td>
</tr>
<tr>
<td><a href="#tv-show-recommendations-for-plex">TV-Show-Recommendations-for-Plex</a></td>
<td><span class="partial">Partial</span><span class="q">via labels</span></td>
<td><span class="no">No</span></td>
<td><span class="no">No</span><span class="q">labels only</span></td>
<td><span class="yes">Yes</span><span class="q">no AI</span></td>
<td><span class="no">No</span><span class="q">v2.2, 19 Mar 2025</span></td>
<td class="compare__pick">you want a small script to run from cron and approve picks by hand</td>
</tr>
<tr>
<td><a href="#plex-recommendations-ai">plex-recommendations-ai</a></td>
<td><span class="no">No</span><span class="q">one shared collection</span></td>
<td><span class="no">No</span></td>
<td><span class="no">No</span></td>
<td><span class="no">No</span><span class="q">OpenAI required</span></td>
<td><span class="no">No</span><span class="q">no releases; last commit May 2023</span></td>
<td class="compare__pick">listed only because it still ranks in search</td>
</tr>
</tbody>
</table>
</div>

<p class="compare__key">Yes, No and Partial mean the same in every column, and a Partial says why in the cell. Unknown means the project&rsquo;s README does not say.</p>

## Tool by tool

### Shortlist

Per-user rows (Picked for You, Because you watched, Watch it again, seasonal picks, a "Your requests"
row and a shared Popular on this server row, from ten templates) built from each person's own watch
history, made private with Plex's label restrictions. Every other account's share filter gets `label!=shortlist_<user>` merged into it,
so each personal row is hidden from other supported accounts. The
[server owner and some restriction profiles](plex-per-user-collections.md#two-things-to-watch-out-for)
are exceptions. Rows are delivered unpromoted, exclusions merged, and only then promoted onto Home.

Share filters are snapshotted before the first write. The in-app uninstall flow previews restoring
them and reports any accounts it cannot restore; removing the container alone does not clean up Plex.
Sharing filters are merged rather than rebuilt, and the owner is skipped. Everything supports
`--dry-run`. AI is optional; the built-in picker needs no keys.

Plex-only, and it will stay that way: the privacy model depends on Plex's label-based share filters,
which Jellyfin and Emby don't have. Needs Plex Media Server 1.43.2.10687+ and a Plex Pass on the
admin account. MIT.

<p class="pick"><strong>Pick this if</strong> each person on your server should get their own rows, and nobody else should see them.</p>

### Immaculaterr

[ohmzi/Immaculaterr](https://github.com/ohmzi/Immaculaterr). The most feature-dense of these.
Event-driven rather than purely scheduled: when someone finishes a film or an episode it can build
their rows straight away, alongside scheduled refreshes, discovery and library cleanup. Builds a lot
of named collections ("Based on your recently watched", "Change of Taste", "Fresh Out Of The Oven"),
supports profiles with their own users, media types and filters, and integrates with Radarr and
Sonarr. Ships on both GHCR and Docker Hub.

Gives each monitored viewer separate rows and separate history, and pins rows to the Plex surfaces
that viewer can see, but it doesn't claim per-user _privacy_, which is a different thing from
per-user content. Licensed under its own terms rather than a standard open-source licence.

<p class="pick"><strong>Pick this if</strong> you want the richest set of automatic collections and a tool that reacts in real time, and you&rsquo;re relaxed about other users seeing each other&rsquo;s rows.</p>

### Curatarr

[OrchestratedChaos/curatarr](https://github.com/OrchestratedChaos/curatarr). Analyses each user's
watch history and scores unwatched library content by keyword, genre, cast and director similarity,
creating per-user collections that update automatically. Also generates external watchlists so you
know what to acquire next, and groups picks by streaming availability.

By default it hides each user's collection from other users with Plex label restrictions. Its own FAQ
calls this UI-level separation rather than access control: the server owner sees every collection,
and someone who knows a collection's ID can still fetch it through the Plex API.

Distributed as standalone binaries for Windows, macOS (Apple Silicon) and Linux, as a container image,
and from source. The binaries need no Docker, which is genuinely the easiest install here. Licensed
AGPL-3.0 since version 2.17.0 (MIT before that).

<p class="pick"><strong>Pick this if</strong> you want per-user recommendations without running Docker, and care about what to acquire next as much as what to watch.</p>

### Diskovarr

[Lebbitheplow/diskovarr](https://github.com/Lebbitheplow/diskovarr). A different shape: a separate
discovery app your users sign into, for Plex and Jellyfin, rather than rows on Plex Home. Personalised
carousels with reason tags, watchlist sync, a request queue that routes to Overseerr, Radarr and
Sonarr, reviews and a yearly "Wrapped". Its feature surface is much broader than Shortlist's.

It does write Plex collections from its lists, with a promotion setting per collection, but it makes
no claim to hide them from other accounts, so they are not private per user. Node and SQLite. The
README says MIT; the repository has no licence file.

<p class="pick"><strong>Pick this if</strong> you want a full discovery and request portal your users open themselves, or you run Jellyfin as well as Plex.</p>

### Conjurr

[yungsnuzzy/conjurr](https://github.com/yungsnuzzy/conjurr). Reads Plex watch history through
Tautulli and asks Google Gemini what each person should watch next. The results appear on its own web
page: an admin view with a user picker, and a user mode where someone enters their Plex email or
username. Overseerr supplies availability and request links.
[newsletterr](https://github.com/jma1ice/newsletterr) can pull its picks into the email it sends each
user.

It writes nothing to Plex. No licence file in the repository.

<p class="pick"><strong>Pick this if</strong> you already run Tautulli, are happy to use Gemini, and want per-user suggestions on a web page or in an email rather than on Plex Home.</p>

### Nextt

[WhiskeyCoder/Nextt](https://github.com/WhiskeyCoder/Nextt). A self-hosted what-to-watch dashboard
for Plex or Jellyfin. It reads your 4&ndash;5 star ratings, or recent watch history if you don't
rate, fetches matching titles from TMDB, and sends requests to Overseerr or Jellyseerr. No AI. Ships
as a Docker image.

One profile per install: a single Plex token or Jellyfin user. MIT.

<p class="pick"><strong>Pick this if</strong> you want a dashboard to browse suggestions and request what&rsquo;s missing, with no AI and no changes to your Plex library.</p>

### SeekAndWatch

[softerfish/seekandwatch](https://github.com/softerfish/seekandwatch). A "what should we watch?"
dashboard first. Connects Plex, Tautulli, TMDB, Radarr and Sonarr in one place, with Smart Discovery
from your watch history, a Kometa config builder that saves you writing YAML, and Tautulli trending.
It also builds preset collections, each with its own Home, Recommended and Friends visibility.
There's a hosted Cloud beta so friends can request without access to your apps.

<p class="pick"><strong>Pick this if</strong> the real problem is your household staring at the library unable to choose, or you want a Kometa config builder; that feature has little competition.</p>

### Kometa

[kometa.wiki](https://kometa.wiki/). Formerly Plex Meta Manager. A metadata and collection builder: it
creates collections from rules you write in YAML (by genre, by decade, by Trakt list, by almost
anything) and manages artwork and metadata beautifully. It is not a recommender. It doesn't read
anyone's watch history to decide what to suggest, and its collections are library-wide. Shortlist is
built to run alongside it.

<p class="pick"><strong>Pick this if</strong> you want to shape and organise the library you already have.</p>

### SuggestArr

[giuseppe99barchetta/SuggestArr](https://github.com/giuseppe99barchetta/SuggestArr). Watches what
you recently played and requests similar content through Seerr, so your library keeps growing with
things you'll probably like. Works with Plex, Jellyfin and Emby. It's about acquisition, not
presentation: its output is new files on your disk, not a row on anyone's home screen.

<p class="pick"><strong>Pick this if</strong> your complaint is &ldquo;my library never has anything new&rdquo;.</p>

### TV-Show-Recommendations-for-Plex

[netplexflix/TV-Show-Recommendations-for-Plex](https://github.com/netplexflix/TV-Show-Recommendations-for-Plex).
A well-documented Python script rather than a service. Builds a taste profile from watch history,
scores unwatched shows, and can label them in Plex or push new titles to Sonarr via Trakt. Reads
other users' history through Tautulli. A companion
[Movie Recommendations](https://github.com/netplexflix/Movie-Recommendations-for-Plex) script covers
films. Last updated March 2025, and no licence file.

<p class="pick"><strong>Pick this if</strong> you want something small you can read end to end and drive from cron, and you like approving picks by hand.</p>

### plex-recommendations-ai

[rocstack/plex-recommendations-ai](https://github.com/rocstack/plex-recommendations-ai). Creates a
single collection of recommendations using OpenAI over your watch history. One of the earliest tools
in this space; not per-user, and unchanged since May 2023. Listed because it still ranks well in
search results. No licence file.

<p class="note"><strong>A note on Recommendarr:</strong> there are several unrelated repositories under that name and near-spellings, so check what you&rsquo;re installing. Like SuggestArr, it is oriented toward what to <em>add</em>, not toward per-user rows.</p>

## The three questions that actually narrow the list

**1. Do you want content acquired, or content surfaced?** SuggestArr and Recommendarr add files to
your library. Kometa organises the files you have. Neither changes what any individual sees on their
home screen.

**2. Do you need rows to be private, or just personal?** _Personal_ means the titles are chosen for
one person. _Private_ means nobody else can see the row. Most tools do the first. Doing the second
requires Plex's label restrictions on share filters, a minimum server version, a Plex Pass, and
careful write ordering.

**3. Does it need to coexist with what you already run?** Check that whatever you pick scopes its
writes to its own collections, and if you run Kometa, that the two won't overwrite each other.

## Related

- [How to improve Plex recommendations](improve-plex-recommendations.md), before installing anything
- [AI recommendations for Plex](plex-ai-recommendations.md): which tools need an API key and why
- [Per-user collections](plex-per-user-collections.md): the privacy mechanism, and how to do it by hand
- [Getting started](getting-started.md): installing Shortlist
