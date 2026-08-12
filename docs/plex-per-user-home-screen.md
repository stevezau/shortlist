---
title: A different Plex home screen for each user
description: Plex home screen rows are shared by everyone with library access. What managed users, pinned sources and published collections change — and how to give each account rows only they see.
heading: How to give each Plex user their own home screen rows
---

**Short answer:** Plex has no setting for per-user home screen rows. Everyone with access to a
library sees the same shelves. You can get there, but it's built out of label restrictions on share
filters rather than any recommendation feature, and it needs Plex Media Server 1.43.2.10687 or newer.

## What each account can already change for itself

Some of the home screen is per-account, which is why this question gets confusing answers.

**Pinned sources and their order.** Each user chooses which servers and libraries appear in their
sidebar and can reorder them. This is stored per account, so two people genuinely can have different
home screens in that sense — but they're picking from the same set of rows.

**Continue Watching and Up Next.** Genuinely personal, and the only rows on the server whose
_contents_ differ per viewer.

**Hiding a row locally.** In some clients a user can dismiss individual shelves. Client-side,
inconsistent between apps, and it doesn't survive much.

What none of that does is give someone a row of _different titles_ chosen for them.

## What the admin controls, and why it doesn't help

**Manage Recommendations** (library → **Manage Recommendations**) lets the admin choose which rows
appear on the Recommended shelf and in what order. It's a server-wide setting. Change it and you've
changed it for everybody.

**Publishing Collections** lets the admin promote a collection to Home or Recommended for shared
users. This is the closest thing Plex has to "put a curated row on people's home screens", and it's
worth knowing about — but a published collection goes to _everyone_ who can see that library. There
is no per-account targeting in the publishing UI.

**Managed users** (Settings → Users & Sharing → add a managed user) create separate profiles under
your account with their own watch state and their own content restrictions. People reach for these
expecting Netflix profiles. They do give separate watch history, which is real and useful. They do
not give separate recommendation rows — a managed user still sees the library's shelves.

So: the admin can decide what rows exist, and each user can decide which libraries they look at.
Nobody can make a row that contains different titles for different people. Not through the UI.

## The mechanism that does work

Plex evaluates one thing per account: the **share filter**. When you share a library, that share
carries restrictions — by rating, by genre, and by **label**. Labels are the useful one, because you
assign them yourself and Plex checks them against the specific account doing the looking.

You can't say "show this collection to Alice". You _can_ say "hide this collection from everyone who
isn't Alice", and the visible result is the same.

The full mechanism, the manual steps and the version requirements are on
[Make a Plex collection visible to one user](plex-per-user-collections.md). Two things from that
page matter enough to repeat here:

**Order matters, and getting it wrong leaks.** Create the collection, promote it to Home, then add
the exclusions, and there's a window where every user on the server has someone else's private row
on their home screen. Create it unpromoted, label it, merge the exclusions into every other account's
share filter, and only then promote.

**Merge share filters, never rebuild them.** A share filter is one string per library holding
everything — parental controls, genre restrictions, other tools' rules. Overwrite it with just your
own exclusion and you've silently removed someone's content restrictions.

## What it costs at your server's size

The mechanism is per-pair, which is where hand-rolling this dies.

Each private row needs an exclusion on every _other_ account. For **n** users with one row each,
that's **n × (n−1)** share-filter entries:

| Users | Filter entries to maintain |
| ----- | -------------------------- |
| 3     | 6                          |
| 10    | 90                         |
| 20    | 380                        |
| 40    | 1,560                      |

And it isn't a one-time cost. Add a user and you touch every existing share. Add a second row type —
films and shows are separate collections, because label restrictions are evaluated per library — and
it doubles. Rebuild rows nightly and every run walks the whole matrix again, each entry a
read-modify-write against a string you must not corrupt.

Two or three collections by hand is fine. Past that you want something maintaining the matrix for
you.

## Two limits you can't engineer around

**The server owner sees everything.** Plex doesn't apply share filters to the admin account, because
there's no share to filter. If you're the owner you will see every labelled collection on your
server no matter what you do. Nothing is broken; there is no fix. Plan your QA around a non-owner
test account, because checking privacy from the admin session will always show you everything.

**Films and shows need separate rows.** A collection lives in one library, and Plex tracks
`filterMovies` and `filterTelevision` separately. A collection holding the wrong type for its library
matches neither restriction — which means it cannot be hidden from anyone, from any account, ever.

## The automated version

[**Shortlist**](https://github.com/stevezau/shortlist) maintains the whole matrix on a schedule. Each
user gets their own rows built from their own watch history; every run sweeps rows Plex can't hide,
delivers rows unpromoted, merges the `label!=` exclusions into every other account's share filter,
and only then promotes anything onto Home. In that order, every time.

It snapshots your share filters before its first write and restores them exactly on uninstall, merges
rather than rebuilds, skips the owner, and never touches a collection it didn't create — so Kometa
and anything else managing collections on the same server keep working.

```bash
docker run -d --name shortlist -p 5959:5959 \
  -v /path/to/config:/config \
  ghcr.io/stevezau/shortlist:latest
```

## Related

- [Per-user collections](plex-per-user-collections.md) — the label + share-filter mechanism in full
- [Recommendations from watch history](plex-recommendations-watch-history.md) — where the titles come from
- [Rows and templates](guides/rows.md) — row types, placement and posters in Shortlist
- [FAQ — what can the owner see?](faq.md)
