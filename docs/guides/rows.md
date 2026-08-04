---
title: Rows and templates
description: Start a row from a template, name it, choose how its titles are ordered, and pick which Plex screens it shows on.
heading: Rows and templates
nav_order: 2
---

## Starting from a template

**Rows → Add a row** opens a gallery rather than a blank form: _Picked for You_, _Because you
watched…_, _Happy to see again_, _Fresh finds_, _From the vault_, _Popular on this server_, _Movie
night_, _More TV to watch_, and _Start from scratch_. Each tile names the two or three settings it
changes, so picking one also shows you which knobs matter. Nothing is locked in. Every field is
editable afterwards, and the template is not stored on the row.

## Naming a row

A row's name can be plain text ("Hidden Gems") or use a placeholder that fills in per person when
the row is built:

- `{library_name}` — the library the row is built in. `✨ {library_name} Picked for You` becomes
  "✨ Movies Picked for You" in your Movies library and "✨ TV Shows Picked for You" in your TV
  library. This is the default row name, so a server with several libraries gets distinct titles
  instead of two identical "Picked for You" rows.
- `{user}` — the person's name. `{user}'s picks` becomes "Sarah's picks". That name is their
  **nickname** if you've set one (Users → open someone → "What to call them"), otherwise whatever
  Tautulli calls them, otherwise their Plex username. Which is often a handle nobody uses. Changing
  a nickname renames their existing rows on Plex; it never changes their label, so their privacy is
  unaffected.
- `{top_seed}` — the title that most drove their recommendations. `Because you watched {top_seed}`
  becomes "Because you watched The Bear".

If a `{top_seed}` row is built for someone with too little history to have a favourite, it falls
back to a clean default ("✨ Picked for You") rather than a half-finished sentence. You can rename
any row at any time in the **Row editor**, and the collection on Plex is renamed in place, so its
place in the shelf and its privacy are preserved.

**A `{top_seed}` row needs one more setting to be honest.** By default every row is built from a
person's 30 most recent watches blended together, so a row titled "Because you watched The Bear" is
really "because you watched these thirty things, one of which was The Bear". Set **Row editor →
Watches every source builds from** to `1` and the row genuinely is what that one title led to. The
editor prompts you for this as soon as a row's name uses `{top_seed}`.

One catch, which the editor also tells you: seeds are shared out across the media types a row
covers, and a single watch is either a film or a show. Never both. So a row set to **Movies and
TV** with a budget of 1 seeds only one of them, and the other library's collection never builds.
For a row covering both, use `2` (one of each), or set the row to Movies only or TV only.

**Watches every source builds from** (1–100, default 30) matters on its own: it
decides how many watched titles every discovery source searches from. Fewer means a tighter, more
coherent row about a couple of things; more means broader coverage of someone's taste. **Watches the
AI web search looks up** is a slice off the front of that same list, and caps the AI web-search
source alone.

There is a server-wide default for it in **Settings → Finding titles**, and any row can override it.
The global stops at 5 while a row goes down to 1, because a single seed is a choice worth making for one row
rather than imposing on every row at once.

### Which watch a one-title row follows

A row built from one watch normally follows the most recent one, and stays on it until that person
finishes something else. If they watch little for a fortnight, the row says the same thing for a
fortnight.

**Row editor → Which watch it follows** changes that. Raise **Recent watches to choose from** above
`1` and the row cycles instead: still one watch per row, but a different one each day, working
through their last few and then coming round again. It cycles rather than picking at random, because
a random pick repeats, and a repeat is indistinguishable from a row that has stopped working. Two
people's rows cycle out of step, so a whole server doesn't rebuild on the same night.

The setting only appears on rows built from one or two watches. Above that a row is blending a whole
history and has no single watch to follow. It is also not the same as raising **Watches to build
from**, which is the setting people reach for first and the wrong one: that blends more watches into
one row, diluting the very claim a `{top_seed}` title makes. Cycling keeps the row about one watch
and moves which one.

**Rows that follow a watch always refresh nightly**, whether they follow it by name (`{top_seed}`)
or by cycling. A row whose title claims a recent watch can't be allowed to lag behind it: at the
usual pace it would go on naming last week's film for a week after the person moved on. So
**How often it changes** is not offered on those rows at all; the row simply refreshes nightly.
Every other row keeps the setting.

There is a modest cost to that. Refreshing nightly does not only mean "a write when the watch
changes". On the nights it hasn't changed the row still swaps its weakest third for new titles, so
it writes to Plex most nights, per person, per library, where an ordinary row on the default pace
writes about weekly. It does not cost any extra AI usage: candidates are gathered once per run
however often a row refreshes, so how often a row refreshes has no bearing on it.

## The order titles appear in

**Row editor → Order** decides how a row's titles are arranged in Plex:

| Order               | What you get                                                                 |
| ------------------- | ---------------------------------------------------------------------------- |
| **Best match**      | Strongest suggestions first, by how well each title matches their viewing    |
| **Highest rated**   | Highest score first, from whichever service you configured                   |
| **Newest released** | Most recently released first                                                 |
| **Shuffled**        | A different order every day, from the same titles                            |
| **Just added**      | Whatever is new to the row goes to the front, the rest follow in match order |
| **Taking turns**    | The front moves along by one title a day, so every pick gets a turn there    |

Plex itself only sorts a collection by release date, alphabetically, or by a custom order, so every
one of these is applied by Shortlist and delivered as that custom order, which is what the Home row
displays.

**Newest released** and **Just added** are different things, and the difference matters: the first is
about when a film or show came out, the second about when it joined this row.

**Highest rated** uses TMDB by default, which needs no setup. To sort on IMDb, Trakt, Rotten Tomatoes
or Metacritic instead, set **Settings → Finding titles → Rate titles using**. Those come from MDBList
and need its API key (the same one the Requests feature uses). Without a key, or once MDBList's daily
quota is spent, the row falls back to TMDB for its _whole_ ordering rather than sorting half the row
on one scale and half on another.

**Shuffled** and **Taking turns** are the two with a cost worth knowing about. The other four are
applied while the row is being written anyway, so they are free; these two reorder the row on Plex
every day, including days when nothing about the row has changed. It is the front of the row that
moves — the first 15 titles, which is what shows on the Home shelf before "see all" — so the cost is
bounded per row, but it is one Plex write per title moved, per person, per night. On a server with
many people that is real write volume, so neither is on by default.

Both are stable within a day. Re-running a row the same night reproduces the same order, and two
people's copies of one row shuffle differently.

**Just added** only moves on the nights a row actually refreshes — on the other nights nothing has
arrived, so there is nothing to put in front. How often that happens is **Freshness**, not this
setting. If the front of a row feels stuck, freshness is usually the dial you want, and **Taking
turns** is the one that moves the front every night regardless.

## Where a row shows

The **Row editor** → **Where it shows** grid picks which Plex screens a row appears on. Two
surfaces, two audiences, and every one of the four switches is independent:

|                       | You | Everyone else |
| --------------------- | --- | ------------- |
| **Recommended shelf** | ☑   | ☑             |
| **Home screen**       | ☑   | ☑             |

The columns are real, not cosmetic: every person gets their **own** Plex collection, so each switch
is set on a different collection. **You** is your own row, and Plex's Home shelf applies to the server
owner alone. **Everyone else** covers the people you've shared with plus Plex Home members, whom
Plex groups together under Shared Users' Home. Each of them only ever sees their own row; everyone
else's is excluded from their share filter.

Turn all four off and the row still gets built and kept private; it claims no Recommended slot, and
you'll find it under the library's **Collections** tab. One caveat: on a run where Shortlist can't
match an existing collection back to its row (a `{top_seed}` row that produced no picks, say) that
row keeps its own Home flag for that run. It stays off the Recommended shelf, and it is only ever
visible to the person it belongs to.

**What this can't do:** hide friends' rows from _your_ Recommended shelf while leaving them
on theirs. Share filters are what hide a row from someone, and you own the server, so there is no
share with yourself to attach one to. So with **Everyone else → Recommended shelf** on, every friend's
row is on your shelf too. Turn it off (leaving **Everyone else → Home screen** on) and each friend still
gets their row on their own Home, while your shelf stays yours. Shortlist shows this warning at the
switch itself.

A common setup: **You** both on, **Everyone else** Home only. You get your row on your Home and your
shelf, everyone else gets theirs on their Home, and nobody's row clutters anybody else's view.

## Row placement (Recommended shelf)

By default Plex adds new collections at the **end** of a library's _Recommended_ shelf, so if another
tool (like **Kometa**) manages collections on the same server, Shortlist's rows can end up buried at
the bottom. Settings → **Row placement** sets a server-wide default; you get three choices per library:

- **Wherever Plex puts them**. Leave the order alone (the default).
- **Top of the shelf** — put Shortlist's rows at the very top. No anchor needed. (This replaces the
  old "pin to top" switch.)
- **Right before / after a collection**. Pick an existing collection and sit the rows next to it.

Any individual row can override the default in the **Row editor** ("Position in the Recommended
shelf"), per library, so "Picked for You" can sit at the top while another row sits right after New
Series. Since each person only sees their own row, moving rows up lifts everyone's at once.

Behind the scenes Shortlist re-applies your choice at the end of every run (so a co-managing tool
can't re-bury the rows), only ever moves its own rows, and never touches the collection you anchored
to. It works with or without Kometa. Kometa is only _why_ this matters, because it fills the shelf, not
_how_ it works; the anchor can be any collection, Kometa's or one of Plex's own.

## Row posters

Each row can have its own artwork on Plex. In the **Row editor** → **Artwork**, pick one of:

- **Plex default** — leave Plex's own collection artwork alone (the default). Switching a row _back_
  to this after it had a custom poster reverts the artwork on Plex on save.
- **Upload** — upload your own image (a tall 2:3 poster looks best; up to 8 MB). It's downscaled and
  stored, then applied to the row's collection(s) on the next run.
- **Text** — a clean built-in poster: your **Title** and **Subtitle** over a gradient. No AI needed,
  works on any setup. Use `{user}`, `{library_name}`, and `{top_seed}` to personalise the text.
- **AI image** — an image generated from your text and **Art style**, using your AI provider's image
  model. This reuses your AI provider's key, so it's available when that provider is **OpenAI** or
  **Google** (Anthropic and local servers can't generate images. Use a Text poster or Upload instead).

Hit **Preview** to see a sample before saving. Generated images are made once and reused across
runs (they refresh when you change the text or style), so posters don't slow a run down or cost per
user. Posters are cosmetic. A poster that can't be made never blocks a row from building.
