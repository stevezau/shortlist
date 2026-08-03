---
title: Make a Plex collection visible to one user
description: Plex has no per-user collections, but label restrictions on share filters get you there. The mechanism, the manual steps, the ordering mistake that leaks, and the version requirements.
heading: How to make a Plex collection visible to only one user
---

**Short answer:** Plex has no per-user collections, but it does have **label restrictions**. Give the
collection a label, then tell every _other_ account to exclude that label. What's left is a
collection only one person can see.

It works, it's supported, and it needs **Plex Media Server 1.43.2.10687 or newer** plus a **Plex
Pass** on the admin account. The rest of this page is how to do it, and the one mistake that quietly
leaks the collection to everyone.

## Why there's no direct setting

Plex collections belong to a _library_, not to a _user_. Everyone with access to that library sees
every collection in it. There is no "share this collection with Alice only" checkbox, and there
never has been.

What Plex does have is a per-share content filter. When you share a library, you can restrict what
that person sees by rating, by genre. And by **label**. That last one is the lever, because labels
are something you control and Plex evaluates them per account.

So the trick isn't making a collection visible to one person. It's making it **invisible to everyone
else**.

## Why this only started working in 2026

Label restrictions have existed for years, but they weren't applied everywhere. A collection hidden
by label would still surface on the Home shelf, the Recommended tab, or in "Related" rows — so a
"private" collection wasn't private at all. This is why the technique didn't reliably work before,
and why older forum threads say it can't be done.

Plex closed those holes in 2026:

| Plex Media Server | What it fixed                                                    |
| ----------------- | ---------------------------------------------------------------- |
| **1.43.1**        | Label hiding applied to the **Home** and **Recommended** shelves |
| **1.43.2.10687**  | Label hiding applied to **Related** rows                         |

Below 1.43.2.10687 the exclusion is ignored in at least one surface, and the collection leaks. Check
**Settings → General** on your server before relying on any of this.

## The manual method

For a single collection shared with a single person:

1. **Label the collection.** In Plex Web, open the collection → **Edit** → **Tags** → add a label,
   e.g. `picks_alice`. Use a prefix you'd never use for anything else, so you can always tell your
   labels apart from ones Kometa or another tool manages.
2. **Exclude that label from everyone else.** Go to **Settings → Users & Sharing**, and for **every
   other account with access to that library**: edit their library access, find the per-library
   restrictions, and add `picks_alice` to **Exclude Labels**. (The exact wording shifts between Plex
   versions; you're looking for the label-exclusion field under the shared library's restrictions.)
3. **Leave Alice's own share untouched.** She's the one person who should _not_ have the exclusion.

That's it. Alice sees the collection; nobody else does.

### Two things to watch out for

**The server owner can't be restricted.** Plex doesn't apply share filters to the admin account —
there's no share to filter. If you're the owner, you will see every labelled collection on the
server no matter what you do. That's a Plex limitation, not something to debug.

**Movies and TV need separate rows.** A collection lives in one library, and Plex applies label
restrictions per library (`filterMovies` and `filterTelevision` are distinct). If you want someone to
have a private row of films _and_ one of shows, that's two collections with the same label. A
collection holding the wrong type for its library matches neither restriction — which makes it
impossible to hide from anyone.

## Do it in the wrong order and it leaks

This is the part people get wrong, and it's worth being blunt about it.

The obvious order is: **create the collection, then add the exclusions.** Don't. Between those two
steps the collection exists, is unlabelled or unexcluded, and is visible on the Home shelf of every
single person you share with. On a server with 40 users that's a window where 39 people can see a row
built from someone else's viewing habits — and Plex clients cache shelves aggressively, so "I fixed
it a minute later" doesn't necessarily un-show it.

The safe order is:

1. Create the collection **unpromoted**. Not on any shelf yet.
2. Label it.
3. Merge the `label!=` exclusion into **every other account's** share filter.
4. **Only then** promote it to Home / Recommended.

The collection is never simultaneously visible and unexcluded. Do it in this order every time, even
when you're "just testing".

### Merge the filter, never rebuild it

When you edit a share filter, **read what's already there and add to it**. Plex stores these as a
single string per library, like:

```
contentRating!=R,label!=picks_bob,label!=picks_carol
```

If you overwrite that string with just your own exclusion, you have silently removed someone's
parental-control restriction or another tool's rules. Parse it, union your label into the existing
`label!=` values, and leave every other condition exactly as they were.

**Snapshot the original values before your first change.** It's the only way to put a server back
the way you found it.

## Why this doesn't scale by hand

The mechanism is sound. The arithmetic isn't.

Every private collection needs an exclusion on every _other_ account. For **n** users each with their
own row, that's **n × (n−1)** share-filter entries — 20 users is 380 of them, and each one is a
read-modify-write against a filter string you must not corrupt. Add a user and you touch every
existing share. Add a row and you touch them all again. Rebuild the rows nightly and it's a
non-starter.

Doing it by hand is realistic for one or two collections. Past that you want it automated.

## The automated version

[**Shortlist**](https://github.com/stevezau/shortlist) is a self-hosted container that does exactly
this, on a schedule. It builds a personalized "Picked for You" collection for each user from their
own Plex watch history, labels it `shortlist_<user>`, merges the exclusions into every other
account's share filter, and only then promotes the rows to Home. In that order, every run.

It also handles the parts this page warns you about: it snapshots your share filters before the first
change and restores them exactly on uninstall, it merges rather than rebuilds, it skips the owner, and
it never modifies a collection it didn't create (so Kometa keeps working alongside it).

```bash
docker run -d --name shortlist -p 5959:5959 \
  -v /path/to/config:/config \
  ghcr.io/stevezau/shortlist:latest
```

Set `-e SHORTLIST_DRY_RUN=1` to see every change it _would_ make to your server without writing one.

## Related

- [FAQ — How is this private?](faq.md#how-is-this-private-plex-doesnt-have-per-user-collections)
- [Getting started](getting-started.md) — install and the setup wizard
- [Reference](reference.md) — settings, API, environment variables
