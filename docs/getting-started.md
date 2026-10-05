---
title: Install Shortlist for Plex with Docker
description: Requirements, Docker install, first login and the setup wizard that connects your Plex server and builds each user's first personalized rows.
heading: Getting started
updated: 2026-10-03
---

## Requirements

- **Somewhere to run a Docker container.** Shortlist ships as a Docker image and is installed by
  running that container — on Windows, macOS, Linux, a NAS (Synology, QNAP, unRAID), or anything
  else that runs Docker. It does **not** have to be the same machine as Plex; it only needs to be
  able to reach your Plex server over the network.
- **Plex Media Server 1.43.2.10687 or newer** — this is the version where Plex started honouring
  the setting that hides each row. On anything older, a private row could show up for other people.
  The wizard checks your version before you begin, so you'll know straight away.
- **Plex Pass** on the server owner's account. The hiding feature is a Pass feature.
- A **TMDB API key** (free: themoviedb.org → Settings → API).

### Optional extras

**Shortlist works fully without any of these.**

- **Tautulli** — only improves the names people are shown by. Watch history comes straight from
  Plex either way, with no setup.
- **An AI provider** — Claude, GPT or Gemini, or a local server you run yourself (Ollama,
  llama.cpp, LM Studio, vLLM, LocalAI). This unlocks one extra source: a live web search for what
  to watch next.
- **An [Exa](https://exa.ai) key _or_ your own [SearXNG](https://docs.searxng.org)** — either makes
  that web search work with _any_ AI provider, including local ones that have no internet access.
  Exa needs a free-tier signup; SearXNG is free and runs on your own hardware. See
  [Which web-search backend should I use?](faq.md#which-web-search-backend-should-i-use).

## Release channels

The commands below install the current release, using the `:latest` image tag. This is the default
for a new installation.

The `:dev` tag follows the development branch and can include changes that are not released yet. To
choose it deliberately, replace `:latest` with `:dev` in your image configuration and recreate the
container. Back up `/config` before changing channels; do not downgrade a migrated database in place.

## Install (Docker)

{% include install.html %}

Add `SHORTLIST_DRY_RUN=1` under `environment:` to see every change it would make while writing
nothing to Plex (see [Trying it safely](#trying-it-safely)).

## Other ways to install

With one `docker run` command instead of a compose file:

{% include install.html variant="run" %}

**From Docker Hub.** The identical image, with the same tags, is on Docker Hub as
`stevezzau/shortlist`. The doubled **z** is deliberate: that is the project's Docker Hub account,
even though the source lives at `github.com/stevezau/shortlist` with one. Don't "correct" it or the
pull fails. GHCR is the default here because it has no anonymous pull limits.

**With the optional seeds.** The repository's
[`docker-compose.example.yml`](https://github.com/stevezau/shortlist/blob/master/docker-compose.example.yml)
is a longer version of the same file, with the optional one-time settings (Plex URL and token, Tautulli, TMDB key)
written in as comments. Every one of them can be entered in the wizard instead.

## The setup wizard

Open `http://your-host:5959`. A fresh install goes straight into the wizard. There is
nothing to sign in to yet. Step 1 connects your Plex account (that's the sign-in, and it's
what claims the instance for you); from then on Shortlist only opens for that account.

<figure class="shot">
  <img src="{{ '/images/wizard.webp' | relative_url }}" width="1440" height="1000"
       alt="The Shortlist setup wizard on its Welcome step, with a seven-segment progress bar and a Get started button">
  <figcaption>The wizard's Welcome step.</figcaption>
</figure>

Every screenshot on this page is of a throwaway test server, so no real account, address or
library appears in one.

> Set Shortlist up on your own network first. Until you sign in with Plex and link a server,
> anyone who can open the page could claim it as theirs, so don't put it on the public internet
> until you've finished the wizard. Once you've claimed it, it's yours.

The wizard has **7 steps**:

1. **Welcome** — a short intro screen. Read it and continue.
2. **Connect Plex** — sign in with a PIN, then pick your server. Shortlist checks your Plex
   version, Plex Pass, and libraries, and tells you in plain English whether each one is OK.

   <img src="{{ '/images/wizard-connect.webp' | relative_url }}" width="1440" height="783"
        alt="The Connect Plex step after running checks: the discovered server with its reachable and unreachable addresses, and a checklist confirming the Plex version, Plex Pass and two libraries">

   Every address Plex advertises for your server is tried from where Shortlist actually runs, and
   the one that answered is preselected. You can always type a different one.

3. **Recommendations & history**. Choose where picks come from and save the required TMDB key.
   Watch history comes straight from Plex with no setup. Tautulli is optional, and only improves
   the names people are shown by.
4. **Choose your AI provider** — Claude / GPT / Gemini / a local server / **None**. Keys stay
   yours: stored encrypted, and hidden again once saved. Picking None is a perfectly good choice.
5. **Pick your users** — everyone you share with, with badges showing how much history each
   person has.
6. **Make it yours** — choose the row's name, how many titles it holds and its refresh cadence.
   Each row keeps its own schedule. A live title preview shows the name as you type, and
   **Save & continue** saves the name and size before moving on.

   The name can be plain text, or use a placeholder that fills itself in per person, such as
   `{library_name}`, `{user}` or `{top_seed}`. See [Naming a row](guides/rows.md#naming-a-row)
   for what each one becomes.

7. **First run** — watch it build, person by person. Reloading resumes the same run. Results
   distinguish built, skipped and failed users. You can finish setup while the run continues,
   or skip building until later.

## Trying it safely

Shortlist is new and it changes real Plex sharing settings, so you may well want to watch it work
before you trust it. Two ways to do that:

- **Safe mode** — start the container with `-e SHORTLIST_DRY_RUN=1`. Every run then logs exactly what
  it _would_ change and writes **nothing** to Plex. Walk the whole flow, read the run activity, and
  only remove the flag (and recreate the container) once you're happy.
- **One user first** — on the Users page, disable everyone except a test account, run, then sign in
  as that account (not the owner. The owner sees every row) and confirm they see only their own row.

The **first real run is the slowest**: it builds every enabled user's rows and merges every account's
share filter. Later runs are much faster. Most rows are unchanged and skipped.

Every row is hidden from every other account before it is ever put on a home screen, so nobody finds
a row that was built for someone else. (You are the exception: Plex cannot hide anything from the
server owner — see below.) Your share filters are copied before the first change, so **Uninstall**
(Settings → System → Danger zone) restores that saved copy and reports any accounts it cannot restore. The hiding relies on Plex Media Server
1.43.2.10687 or newer — older builds ignore it, which is why the wizard surfaces your version before
you begin.

## One thing you should know

You're in the user list too, so you can give yourself a row like anyone else. On a one-person
server that's the whole point.

What Plex cannot do is hide collections from the **server owner**. Your own Home screen is fine —
Shortlist puts each person's row only on their side — but the library's **Collections** tab shows you
everyone's, and so does the Recommended shelf if you leave that on for a row. If that bothers you,
take the rows off the Recommended shelf, or watch on a Plex Home user and keep the admin account for
administration.

## You're set up. What now?

Everyone has a row and it will refresh on its own. Worth doing next:

- **Check it landed.** Sign in as somebody who isn't you and confirm they see their row, and only
  theirs. The owner account sees everybody's, so it can't tell you this.
- **Add another kind of row.** There are ten templates, including "Your requests".
  See [Rows and templates](guides/rows.md).
- **Decide how often rows change.** Each row keeps its own schedule.
  See [Schedules](guides/schedules.md).
- **Let it fill gaps in your library.** Shortlist can ask Radarr or Sonarr for titles your people
  want but you don't have. See [Requests](guides/requests.md).

If a row doesn't turn up, [Troubleshooting](guides/troubleshooting.md) lists what usually causes it.
