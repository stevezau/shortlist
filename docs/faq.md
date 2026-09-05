---
title: "FAQ: per-user Plex collections and privacy"
description: How Shortlist makes a Plex collection visible to only one user, what the server owner can see, whether it conflicts with Kometa, and how to uninstall cleanly.
heading: FAQ
---

## How is this private? Plex doesn't have per-user collections.

It does, indirectly. Plex lets you hide things from someone by **label**, so Shortlist gives each
person's row a label of its own and tells every _other_ account to hide that label. The result is a
row only its owner can see.

The order those steps happen in is what makes it safe, and it is the same every run:

{% include privacy-order.html %}

Your existing sharing settings are saved beforehand, and **Uninstall** puts them back exactly.

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

So it adds hide-this-label rules to every account your server is shared with — unless you've asked
it to leave one alone, which you can do per person if their own Plex restrictions clash with
Shortlist's. Nothing else in their settings is touched. Shortlist reads what's there, adds only its
own entries, and leaves the rest exactly as they were. The original is saved first, and Uninstall
restores all of them.

## Do I get a row myself?

Yes. Plex's user list never includes the server's own owner, so Shortlist adds you separately,
badged `owner`. Switch yourself on and you get a row built from your own history, which is the
whole point on a one-person server.

## What can the server owner see?

Everyone's rows — but **not** on your Home screen. Plex keeps "on the owner's Home" and "on a
friend's Home" as two separate switches, and Shortlist only ever sets a person's row on their own
side, so nobody else's row lands on your Home.

Where you do see them all is the library's **Collections tab**, and its **Recommended shelf** if you
leave _Everyone else → Recommended shelf_ on for a row. Rows are hidden from other people through
the share each of them has with your server, and you have no share with yourself, so there is
nothing for Plex to hide them behind. There is no Plex setting for this.

Shortlist walks you through the three options under **Users → You see everyone's rows**: take the
rows off the library shelf, leave it alone, or move your own watching to a separate Plex Home
account. That last one copies your watch history across exactly — the same episodes of each show,
your rewatch counts, and anything you are part-way through, back where you left it. See
[the reference](reference/concepts.md#why-you-see-everyones-rows-and-the-watching-account).

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

The two external backends are the only options a local model can use, because an Ollama or LM Studio
server on your own hardware cannot reach the internet by itself. They also keep your results the same
when you switch AI providers. [AI and cost](guides/ai.md#exa-or-searxng) compares the two and
covers the one SearXNG setting you have to turn on.

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
exactly what changed. Your server ends up exactly as it was before you installed Shortlist.

The one exception is an account that has since left your server. Shortlist can no longer reach a
departed account's settings on plex.tv, so there is nothing there to put back — the report names
those accounts rather than quietly counting them as restored, and the uninstall finishes regardless.

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

{% comment %}
FAQPage structured data, generated from the same _data/faq.yml the home page's teaser renders, so
this page's structured data and that teaser can never disagree. This page carries more questions
than faq.yml on purpose (faq.yml is a deliberate short subset, see its own header): the reused set
is accurate, just partial.

It produces no Google rich result. Google retired the FAQ rich result and removed the feature: its
own documentation page for FAQPage now 301s to /search/updates#removing-faq-rich-result (checked
2026-09-05), and FAQPage is absent from the current structured-data gallery. It stays here for the
same reason as the SoftwareApplication block in head.html: AI crawlers and other indexes read
schema.org types to work out what this software is. Do not describe it as ranking work.
{% endcomment %}
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {%- for q in site.data.faq -%}
    {
      "@type": "Question",
      "name": {{ q.q | jsonify }},
      "acceptedAnswer": { "@type": "Answer", "text": {{ q.a | markdownify | strip_html | normalize_whitespace | strip | jsonify }} }
    }{%- unless forloop.last -%},{%- endunless -%}
    {%- endfor -%}
  ]
}
</script>
