---
title: "Troubleshooting and backups for Plex recommendation rows"
description: The common failures and what causes them, plus what Shortlist backs up and what it can't restore.
heading: Troubleshooting and backups
nav_order: 7
---

## Start here: "Have an issue?"

Before working through the list below, open **Have an issue?** in the sidebar. It runs twenty-two
read-only checks against your own server and, for most of these problems, tells you the answer
outright — which library refused someone's token, which setting actually applied, why a row is
short, whether Plex matches what Shortlist thinks it delivered.

Nothing on that page changes anything: not your Plex server, not your rows, not your settings.

Three things worth knowing:

- **The checks are off until you switch them on**, and they switch themselves off after 24 hours.
  They read share filters and per-user tokens, so they stay closed on an install that isn't
  currently debugging something. Filing a bug does not need them on — only attaching the
  diagnostics does.
- **Pick the problem, not the check.** Each card at the top runs every check that question needs,
  and several answers stack up on the page rather than replacing each other, so you can copy them
  all into one report.
- **Every check has a "Copy for support" button.** What it copies is shown on screen first, so
  there is nothing to be surprised by after pasting. A check that fails to run offers its failure
  to copy instead.
- **The last section files the report.** It opens a pre-filled GitHub issue and gives you the full
  diagnostic to attach — as a paste, or as a file when a chat app would truncate it.

What leaves your server, and what doesn't:

| Masked                                                                                                                                         | Kept                                                                                                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Passwords, Plex tokens, API keys                                                                                                               | Library and row names, title names, error messages                                                   |
| IP addresses and your server's machine id, in the report and in every log file — a URL keeps only its scheme and port (`https://<host>:32400`) | Counts, timings, settings values, migration state                                                    |
| Nothing else. Everything below is in there                                                                                                     | The Plex usernames themselves. Replace them yourself before posting if you'd rather not publish them |
|                                                                                                                                                | Nicknames and friendly names, **in the log files** — see below                                       |

**Nicknames are not masked.** The report's own text never prints them — it prints Plex usernames and
slugs — but the log files bundled alongside it are not filtered for names, and nothing masks them. If
a row template contains `{user}` it renders as the nickname, and every delivery line in the log then
carries it:

```
delivery:deliver - jsmith: delivered 'Picked for Dad' to 'Movies' (20 items, label shortlist_jsmith)
```

The same is true of **Logs → Download**, which is the same log files with the same filtering — this
server's own address and machine id, and secrets. Not people's names.

**Treat that as a good first pass, not a guarantee.** Logs are free text: they carry whatever a
library, a plugin or an error message decided to print, and something unusual can still get through
a filter written for the shapes Shortlist knows about. Give the report a skim before posting it
anywhere public — and if you find something that should have been masked and wasn't, that is worth
an issue of its own.

If someone is reporting a problem on a server you don't administer, sending them there is usually
faster than a list of questions: _"open /issue, switch the checks on, type the title, press Copy."_

## Troubleshooting

- **Shortlist can't reach Plex** ("Shortlist could not reach Plex at …" on a run, or on the Plex
  card under Settings → Connections) — the server address is tried from where Shortlist runs, and
  in Docker that is _inside the container_, not your desktop. Change it under Settings →
  Connections, then press **Test**. The usual causes:
  - **`localhost` or `127.0.0.1`.** Inside a container that is the container itself, even when Plex
    is on the same machine. On Docker Desktop (Windows or Mac) use
    `http://host.docker.internal:32400`. On Linux use the machine's own network address, such as
    `http://192.168.1.10:32400`.
  - **An address ending in `.plex.direct` that starts with your public IP.** That is your server's
    internet address. It only works while your internet connection is up and your public IP has
    not changed, so it can stop working after an outage or a router restart. Use an address on
    your own network instead.
  - **`https://` with an IP address or hostname.** Plex's certificate only covers its own
    `.plex.direct` name, so use `http://` for anything else. If Plex has **Settings → Network →
    Secure connections** set to _Required_ it refuses plain `http://`; set it to _Preferred_.
  - **The wrong port.** Plex listens on `32400` unless you mapped it to something else.
  - **"… sent Shortlist on to …".** The address you saved answered with a redirect, and it is the
    second address that failed. The message names both, so you can tell which one to fix.

  To check an address from where Shortlist actually runs, ask the container itself (swap in your
  container's name and the address): `docker exec shortlist curl -s http://192.168.1.10:32400/identity`.
  A line of XML means Shortlist can reach it; an error means the container can't, whatever a
  browser on your desktop says.
- **A run says "skipped" and no collections were made** — a skip is always a configuration
  outcome, and the run page now says which one. The two common ones: _every enabled row is a
  **shared** row_, so there is no per-person row to build for anybody (add one under Rows), or a
  **shared row can't reach its threshold**. A shared row is built only from titles several people
  have watched, so it needs at least 2 enabled users with viewing in common and will skip forever
  below that. Make it a per-person row instead if you want one person to get it.
- **A user says they can see someone else's row** — first check they aren't badged **Sharing
  untouched** in Users: that means you asked Shortlist to leave their Plex account alone, so it
  writes no exclusions for them and re-running will never change it (turn **Manage their Plex
  sharing settings** back on for them). Otherwise run Shortlist again (Run now): every run
  re-merges the `label!=` exclusions into each account's share filters. Check whether the share
  was edited by hand in plex.tv (Shortlist re-merges but never deletes filter conditions it
  didn't add), and confirm Plex Media Server is ≥ 1.43.2.10687 (older builds ignore the exclusion).
  If the account has a restriction of your own (a content rating or label rule) and you're on an
  older Shortlist, update: earlier versions joined the exclusion to your restriction in a way Plex
  ignores.
- **Rows not appearing for anyone** — promoted rows land in Plex's hub order; users may
  need to scroll, or pin the row via "Manage Home Screen" on their client.
- **Rows keep drifting to the bottom of the Recommended shelf** — something else on your server is
  reordering that shelf. It is a shared, server-wide list, and any tool that manages Plex
  recommendations (Kometa, agregarr, Plex-Meta-Manager) will move Shortlist's rows along with
  everything else. Shortlist re-applies your chosen position at the end of every run, on every
  privacy sync, and whenever you press **Check and fix rows on Plex** — but if the other tool runs
  more often than that, or right after, it wins and the rows move back.

  Shortlist notices this on its own: when the same row has to be put back three or more times in a
  day, a **"Something else is reordering your shelf"** alert appears in the bell. You have two ways
  out, and the right one depends on which tool you want in charge:
  1. Tell the other tool to leave Shortlist's collections alone. Look for a per-collection
     "active"/"managed" switch or an exclusion list, and exclude everything labeled `shortlist_*`.
     Worth checking whether it can exclude by _label or pattern_ rather than per collection — a
     per-collection exclusion has to be redone every time you add a Plex user, because that creates
     a new row the other tool will discover and start managing.
  2. Let the other tool own the shelf: Settings → Defaults → Row placement → turn off **Let Shortlist order the
     Recommended shelf**. Shortlist stops touching the order entirely and the two stop fighting.
     Your rows are still built, delivered and kept private exactly as before — only their position
     on the shelf is handed over.

- **A watched title keeps getting recommended** — run the **"They keep seeing something they've
  watched"** check on the Have an issue? page; it names the cause. The three real ones, in order of
  how often they turn out to be it:
  1. _Shortlist has no watched record for that person at all_ — usually a library that has never
     been readable with their token, which looks identical to someone who watches nothing. The check
     says which library.
  2. _You're looking at the wrong account._ Watched state in Plex is per person, so a title ticked
     off on your account says nothing about theirs.
  3. _Timing._ The watched set is read per run, so a title marked watched after the last run stays
     eligible until the next one. **Activity → Jobs → Sync watch history** re-reads everyone's set immediately
     (writes nothing to Plex); any run after that drops it. Note also that a row only re-picks its
     titles on a rebuild night — every 8 days by default — so a change can take
     until then to show. The **"When does each row next rebuild?"** check gives the date.

  Being part-way through a series is not one of them: a show you have only started already counts as
  watched for a row left at 0%, so two episodes in is enough to keep it out. See
  [what "already watched" means for a show](../reference/concepts.md#what-already-watched-means-for-a-show).

- **Everything broke, get me out** — Settings → System → Danger zone → **Uninstall** restores every
  user's share filters from the pre-Shortlist snapshots and deletes every shortlist-labeled
  collection. Kometa and other tools' collections are never touched.
- **Did anything drift out of sync?** — Settings → System → Danger zone → **What Shortlist has on your
  Plex** ("Check Plex") lists every shortlist-labeled collection read straight from the server (not
  the database), flagging any whose user/row no longer exists in the app. Every collection is
  labeled at creation, in one step, so a collection that can't be labeled is deleted rather than left
  as an orphan, so a cleanup always finds them all; this is how you confirm it.

## Backups

Shortlist copies its whole database to `/config/backups` on a schedule (Activity → Jobs → Back up the database; nightly at
3 AM by default), before every upgrade, and before any restore. It keeps the newest 10 by default.

A backup holds everything Shortlist knows: settings and connections, your rows and their audiences,
the people it tracks, run history and each run's picks, the request inbox, and most importantly the
copies of each user's original Plex share filters that an uninstall restores from.

Restoring one takes effect when you restart the container: Shortlist saves a copy of the current
database and swaps the backup in as it starts, before anything else opens the database. Until that
restart it keeps running on the database it has, and Activity → Jobs → Back up the database says a restore is waiting, with a
button to cancel it. A restore still waiting a day later is dropped rather than applied by whatever
restarts the container next.

If the backup taken before an upgrade cannot be written, Shortlist does not start the upgrade and the
container log says why. The database is left unchanged. Free some disk space or fix the permissions on
the config folder, then restart.

Because a backup holds your rows' **audiences**, restoring one also restores who could see which
rows at that moment. If you have narrowed a shared row's audience since the backup was taken,
restoring widens it again and those people will see the row after the next run. Shortlist says so
before you confirm and again afterwards, but it does not undo it for you, so check Rows before
restarting. Those snapshots are the only
record of how your server's sharing looked before Shortlist touched it, and **Uninstall restores
from them**. Everything else is rebuildable by hand; that isn't.

Two things worth knowing:

- Backups sit beside the database in the `/config` volume, so they survive removing and recreating
  the container, but not losing the volume. Copy them off the host if that matters to you.
- `/config/secret.key` is **not** in a backup. It's the key your Plex token and AI keys are
  encrypted with, so restoring a database without that same file leaves those credentials unreadable
  and you'll have to re-enter them. Keep a copy of it alongside your backups.
