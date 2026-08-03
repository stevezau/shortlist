---
title: AI and cost — what AI does, and how to keep it cheap
description: Shortlist works with no AI at all. What AI adds when you turn it on, which search backend to pick, and how to control what it costs.
heading: AI and cost
nav_order: 5
---

## How Shortlist uses AI (and how to control the cost)

**AI is off by default** (the AI provider is set to "None" out of the box, and the AI web-search
source is off) — Shortlist works fully with **no AI at all**. AI now has exactly one job: the
**AI web-search source**, which finds acclaimed "what to watch next" titles the TMDB lists miss.
Everything else — gathering candidates, ranking them, and writing the "why" under each pick — is done
in code, with no AI and no per-token cost.

**Building a row happens in four steps:**

1. **Find candidates.** Every source you enabled goes looking for titles. Most use **no AI**: the two
   TMDB sources (similar + discover) and Trakt are plain lookups against those services — free, no API
   key beyond the ones you already set up. Only the **AI web-search** source uses your AI provider —
   see below.
2. **Keep only what you own.** Everything found is matched against your actual library and against what
   the person has already watched. Anything you don't have, or they've already seen, is dropped. **This
   is why a pick can never be a title you don't own** — every source only ever contributes real, owned,
   unwatched titles.
3. **Balance and rank.** Shortlist takes a fair share from each source (so one chatty source can't
   crowd out the rest) and scores them. **No AI here** — it's a simple ranking.
4. **Deliver + explain.** The top-ranked titles fill the row, each with a one-line "why" written from
   the seed behind it ("Because you liked sci-fi like Dune", "Because you watched Fargo"). **No AI
   here either** — the reasons are generated in code, so they cost nothing and read the same whether or
   not you use AI.

### The one AI-powered source

- **AI — web search** (the "AI web search" toggle): searches the live web for acclaimed, current
  "what to watch next" titles, then keeps the ones you own. In our own testing this was a strong extra
  source — it surfaces well-reviewed titles the TMDB lists simply don't return. It's the only place AI
  spends anything, and it's off by default.

**How it actually works.** Shortlist takes each person's recent watches, turns them into real web
searches ("what to watch if you liked X"), and hands the results to your model — which then picks
from what was found. The model never browses; it reads.

That ordering is the whole trick. Because **Shortlist** runs the search rather than the model, the
source works with **any** provider — including a local Ollama, llama.cpp or LM Studio server with no
internet access of its own. A local model that could never search the web still gets to recommend
from current web results.

You choose the search backend on the "AI web search" card:

| Backend                        | Works with                         | Trade-off                                               |
| ------------------------------ | ---------------------------------- | ------------------------------------------------------- |
| Your provider's own web search | Claude, GPT, Gemini only           | no extra signup; unavailable on local models            |
| [Exa](https://exa.ai) key      | **every provider, local included** | one extra free-tier signup                              |
| **Auto** (default)             | both, unioned when both are set up | widest coverage — they find noticeably different titles |

**Why we suggest adding an Exa key**, even when your provider can already search:

[Exa](https://exa.ai) is a search engine built for AI to read rather than for people to browse — it
returns ranked results with the relevant text already pulled out, so the model spends its effort
judging films instead of wading through web pages. In practice that buys you four things:

1. **It's the only option that works with a local model.** An Ollama or LM Studio server on your own
   hardware has no way to reach the internet. With Exa, Shortlist does the searching and hands over
   the findings, so a fully offline model still recommends current titles.
2. **Your results stop depending on which AI you picked.** Switch from Claude to a cheap local model
   and the search half stays identical — only the choosing changes.
3. **The cost is predictable.** Exa bills per search rather than per word, and those searches are
   reported separately from AI tokens. Results are reused for 14 days and shared across everyone on
   the server, so a popular film is looked up once, not once per person.
4. **Auto gives you both.** Left on the default with both configured, Shortlist unions the two —
   they reliably surface different films, so coverage is wider than either alone.

Entirely optional: leave it empty and everything still works, you're just limited to your provider's
own search (or to none at all).

### If you don't want to use AI

Leave the AI provider on **None** (Settings → Connections — this is the default) and the AI web-search
source off. You still get full, per-person private rows: candidates come from TMDB/Trakt, ranked by
score with plain "Because you watched…" reasons. Everything about privacy, scheduling and requests
works exactly the same. The only thing you lose is the AI web-search source — nothing else changes,
because nothing else used AI.

### Tuning AI cost

Cost comes entirely from the AI web-search source (Anthropic/OpenAI/Google charge per token; a local
Ollama/OpenAI-compatible server is free but runs on your own hardware). Roughly cheapest-to-priciest
levers:

1. **Turn AI web search off, or limit which rows use it.** A row can override the global sources
   (Rows → Edit) — keep AI web search only on the rows that benefit, and let the rest run on the free
   TMDB/Trakt sources.
2. **Search fewer recent watches.** The source runs one web search per person's recent watch; lower
   `recommendations.recent_count` (Settings → Finding titles) to cut searches. Results are cached 14
   days and shared across users, so a popular title is searched once server-wide.
3. **Use a small, cheap model.** A fast/mini model (e.g. Claude Haiku, GPT-mini, Gemini Flash) is
   plenty; you don't need a flagship model to read a few search results.
4. **Run less often.** Nightly is the default; a longer schedule means fewer runs and fewer searches.

5. **Use a local model.** An Ollama or LM Studio server on your own hardware costs nothing per run.
   This needs an Exa key, since a local model can't search the web itself — see
   [the backend comparison above](#the-one-ai-powered-source).

**Seeing where the tokens go.** Every run records its AI cost so there's no guessing. Open a run
(Runs → click a run) and you'll see the **total AI tokens** for the run, then per person a breakdown
by _what the AI did_ — `web search` — plus any **Exa searches** (counted separately, since Exa bills
per search, not per token). The runs list shows each run's token total at a glance. Use it to spot
which people cost the most, then tune with the levers above.
