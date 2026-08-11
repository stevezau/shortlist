---
title: "FAQ: per-user Plex collections and privacy"
description: How Shortlist makes a Plex collection visible to only one user, what the server owner can see, whether it conflicts with Kometa, and how to uninstall cleanly.
heading: FAQ
---

## How is this private? Plex doesn't have per-user collections.

It does, indirectly. Plex lets you hide things from someone by **label**, so Shortlist gives each
person's row a label of its own and tells every _other_ account to hide that label. The result is a
row only its owner can see.

The order matters: a row is created **hidden**, and only made visible once the "hide this from
everyone else" rules are already in place, so there's no window where the wrong person could see
it. Your existing sharing settings are saved beforehand, and **Uninstall** puts them back exactly.

This needs Plex Media Server **1.43.2.10687 or newer**, and **Plex Pass** on the admin account,
because the hiding rule is a Pass feature. Older versions ignore it.

## Why do I get two rows, one under Movies and one under TV Shows?

Because a Plex collection can only live in one library, and Plex applies its hiding rules per
library. Someone who watches both films and TV gets one row in each, both carrying the same label.

This isn't cosmetic. A row containing the wrong type for its library matches neither hiding rule,
which would make it impossible to hide from anyone.

Your row size is the budget **across** both: a library with at least one pick gets a row, a library
with none gets nothing.

## Does Shortlist change sharing settings for users I haven't enabled?

Yes, and it has to. Plex shows a collection to anyone who isn't explicitly told to hide it, so if
Shortlist only touched the accounts you gave rows to, **everyone else would see those private
rows**.

So it adds hide-this-label rules to every account your server is shared with. Nothing else in their
settings is touched. Shortlist reads what's there, adds only its own entries, and leaves the rest
exactly as they were. The original is saved first, and Uninstall restores all of them.

## Do I get a row myself?

Yes. Plex's user list never includes the server's own owner, so Shortlist adds you separately,
badged `owner`. Switch yourself on and you get a row built from your own history, which is the
whole point on a one-person server.

## What can the server owner see?

Everyone's rows — but **not** on your Home screen. Plex tracks "on the owner's Home"
(`promotedToOwnHome`) separately from "on a friend's Home" (`promotedToSharedHome`), and Shortlist
puts each person's row on their own side only, so nobody else's row ever lands on your Home.

Where you do see them all is the library's **Collections tab**, and its **Recommended shelf** if you
leave _Everyone else → Recommended shelf_ on for a row. Rows are hidden from other people through
the share each of them has with your server, and you have no share with yourself, so there is
nothing for Plex to hide them behind. There is no Plex setting for this.

Shortlist walks you through the three options under **Users → You see everyone's rows**: take the
rows off the library shelf, leave it alone, or move your own watching to a separate Plex Home
account (it can copy your watch history across). See
[the reference](reference.md#why-you-see-everyones-rows-and-the-watching-account).

## Does the AI invent recommendations I don't have?

It can't. The AI only ever chooses among titles already confirmed to exist in your library and
unwatched by that person. Anything else it suggests is discarded and logged. With the provider set
to **None**, no AI is involved at all.

## What does the AI actually do, and do I have to pay?

**No. Shortlist runs fine with no AI, and no key.** Most titles come from the free TMDB sources,
and the final choosing and ranking is ordinary code with no per-title cost.

The AI has exactly one paid job: an optional **web search** for what to watch next, which finds
well-reviewed titles TMDB simply doesn't return. It's off by default.

See [AI and cost](guides/ai.md) for the full breakdown and cost controls.

## Which web-search backend should I use?

The optional web-search source can search in three ways, and you pick one in
**Settings → Connections → Web search**:

| Option                              | Works with                         | Trade-off                                                 |
| ----------------------------------- | ---------------------------------- | --------------------------------------------------------- |
| Your AI provider's own search (default) | Claude, GPT, Gemini only       | no extra signup; unavailable on local models              |
| **[Exa](https://exa.ai) key**       | **every provider, local included** | one extra free-tier signup; billed per search             |
| **[SearXNG](https://docs.searxng.org)** | **every provider, local included** | free and fully self-hosted; you run and maintain it    |

**Why we suggest adding one of the external backends**, even if your provider can already search:

- **It's the only way a local model can search at all.** (Either backend does this.) An Ollama or LM Studio server on your own
  hardware has no way to search the internet. With Exa or SearXNG, _Shortlist_ does the searching and
  hands the findings over, so a completely offline model can still recommend current titles.
- **Your results stop depending on which AI you picked.** Switch from Claude to a local model to
  save money and the search half stays identical. Only the choosing changes.
- **The cost is predictable.** Exa bills per search, not per word, and Shortlist reports those
  searches separately from AI usage. SearXNG costs nothing. Results are reused for 14 days and shared
  across everyone on your server, so a popular film is looked up once. Not once per person.

**Exa or SearXNG?** Exa returns extracted page text, needs no infrastructure, and its free tier
covers roughly 1,000 searches a month. SearXNG runs on your own hardware, needs no account, costs
nothing, and keeps everything but the forwarded queries on your server — but you maintain it, and its
JSON API must be switched on (`json` added to `search.formats` in its `settings.yml`, or it refuses
Shortlist with a 403). You pick exactly one backend — Shortlist never runs two, so it can never
search (or bill) twice for the same title. See [AI and cost](guides/ai.md#exa-or-searxng).

It's genuinely optional. Leave it empty and everything still works. You would just be limited to your
provider's own search, or to no web search at all.

## Will it fight with Kometa?

No. Shortlist only ever touches collections carrying its own `shortlist_*` label. Kometa overlays
and your own collections are detected and left alone.

They also solve different problems. Kometa's collections are the same for everyone; Shortlist's are
different for each person.

## What information is sent to the AI?

Titles, and nothing else: a short list of what someone recently enjoyed, so it can search for
what to watch next. No usernames, no account IDs, no genres, no viewing times.

## What if I uninstall?

One flow, with a preview first. Every account's sharing settings are restored from the copy taken
before Shortlist's first change, every Shortlist collection is deleted, and you get a report of
exactly what changed. Your server ends up as we found it.

You can also rehearse the whole thing before trusting it: start the container with
`SHORTLIST_DRY_RUN=1` and every run logs exactly what it _would_ change while writing nothing to
Plex.

## Managed users and kids' accounts?

Supported, both as test accounts and as people who get rows. Their **parental controls are never
modified**. Shortlist only ever merges label rules into sharing settings.

## What if a Plex update breaks label hiding?

Shortlist re-applies the hiding rules on every run, but it doesn't watch for Plex itself regressing
the feature, so a broken update wouldn't be caught automatically.

That's why the minimum is Plex Media Server **1.43.2.10687**: older builds ignore the rule
entirely. Stay on that build or newer, and watch the README for advisories.
