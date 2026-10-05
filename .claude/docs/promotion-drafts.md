# Promotion drafts (October 2026)

Venue rules were checked on 2026-08-02 (memory note "Promotion channels gate on project age"). Re-read
each venue's rules on the day before posting; they change. Every draft links the comparison page
because it is the page that answers "why this one" for a reader who has never heard of Shortlist.

## r/selfhosted — standalone post from ~12 Oct 2026 (project passes 3 months old)

Title:

```text
Shortlist: a private "Picked for You" row for every person on your Plex server
```

Body:

```markdown
I run a Plex server for about 45 friends and family, and Plex's own recommendations are the same shelf for
everyone. Shortlist is a self-hosted tool I built that gives each person their own row on their Plex
Home, built from what *they* watched, and hidden from everyone else.

- One Docker container (FastAPI + SQLite), MIT licensed
- Each row is an ordinary Plex collection; every other account's share filter excludes it before it is
  ever promoted, so nobody sees someone else's row
- Works with no AI key (TMDB similar titles). Optionally Claude, GPT, Gemini or a local model re-ranks
  and writes the "Because you watched…" line
- Seasonal rows, "Watch it again", "Popular on this server", and an optional Radarr/Sonarr/Overseerr
  hand-off for picks you don't have yet
- Dry run, a full audit of every change it made to Plex, and an uninstall that restores everyone's
  sharing settings

Limits, up front: Plex only (it relies on Plex's label-based share filters), needs Plex Pass, and the
server owner's own account sees every row because Plex cannot filter the owner.

Comparison with Kometa, SuggestArr, Immaculaterr and the rest:
https://shortlistapp.dev/plex-recommendation-tools/

Site and install: https://shortlistapp.dev · Source: https://github.com/stevezau/shortlist

Built with AI assistance (Claude Code); every change is reviewed and tested before release.
```

## awesome-selfhosted — PR from ~21 Nov 2026 (first release passes 4 months old)

Check the current CONTRIBUTING rules first: awesome-selfhosted marks software that depends on a
proprietary service with ⚠; Plex Media Server is proprietary, so expect to add that marker.
Category: `Media Management` (or whichever category currently lists Plex add-ons).

```markdown
- [Shortlist](https://shortlistapp.dev/) - Private per-user recommendation rows for Plex, built from each person's watch history. ([Source Code](https://github.com/stevezau/shortlist)) `MIT` `Python/Docker`
```

## Plex forums — Apps & Creations (any time; reply in the existing Shortlist thread)

```markdown
**Shortlist: a redesigned interface**

The app has been redesigned around one question: did last night's run work, and is every row still
private? The dashboard now leads with last run, next run, privacy and Plex connection; a run that
built fine but found an account Plex will not hide is reported as "OK with warnings" instead of a clean
OK; and a new "Changes on Plex" tab lists every write Shortlist made to your server.

What changed and how it works: https://shortlistapp.dev/
```

(Post this one after the redesign reaches the stable `:latest` image, not while it is `:dev` only.)

## AlternativeTo — weekday only (submissions are paused Sat/Sun CET)

Submit as an alternative to "Plex recommendations" / "Netflix recommendations" style entries.
Description: use the definition sentence from the landing page.

## r/homelab — allowed route

"Project: Software" flair, AI usage disclosed in the flair AND the body, answer the posting prompt,
and meet the subreddit karma minimum. Reuse the r/selfhosted body above (it already carries the
disclosure line). Do not crosspost: a crosspost has no body, so the disclosure rule cannot be met.
