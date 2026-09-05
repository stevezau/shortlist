---
title: AI and cost
description: Shortlist works with no AI at all. What AI adds when you turn it on, which search backend to pick, and how to control what it costs.
heading: AI and cost
nav_order: 5
---

## What AI does, and what it costs

**AI is off by default.** The provider is set to "None" out of the box and the AI web-search source
is off, so Shortlist works fully with no AI at all.

AI has exactly one job: the **AI web-search source**, which finds acclaimed "what to watch next"
titles that the TMDB lists miss. Everything else is done in code, with no AI and no per-token cost.
That includes gathering candidates, ranking them, and writing the "why" under each pick.

**Building a row happens in four steps:**

1. **Find candidates.** Every source you enabled goes looking for titles. Most use **no AI**. The two
   TMDB sources (similar and discover) and Trakt are plain lookups against those services, free, and
   need no API key beyond the ones you already set up. Only the **AI web-search** source uses your AI
   provider.
2. **Keep only what you own.** Everything found is matched against your actual library and against
   what the person has already watched. Anything you don't have, or they've already seen, is dropped.
   **This is why a pick can never be a title you don't own.** Every source only ever contributes
   real, owned, unwatched titles.
3. **Balance and rank.** Shortlist takes a fair share from each source, so one chatty source can't
   crowd out the rest, and scores them. **No AI here**. It's a simple ranking.
4. **Deliver and explain.** The top-ranked titles fill the row, each with a one-line "why" written
   from the seed behind it, like "Because you watched sci-fi like Dune" or "Because you watched Fargo".
   **No AI here either.** The reasons are generated in code, so they cost nothing and read the same
   whether or not you use AI.

### The source that uses AI

**AI web search** searches the live web for acclaimed, current "what to watch next" titles, then
keeps the ones you own. In our own testing this was a strong extra source, surfacing well-reviewed
titles the TMDB lists simply don't return. It is the only place AI spends anything, and it is off by
default.

**How it works.** Shortlist takes each person's recent watches and turns them into real web searches
("what to watch if you liked X"), then hands the results to your model, which picks from what was
found. The model never browses. It reads.

That order is what makes it work. Because Shortlist runs the search rather than the model, the source
works with **any** provider, including a local Ollama, llama.cpp or LM Studio server with no internet
access of its own. A local model that could never search the web still gets to recommend from current
web results.

You choose the search backend in **Settings → Connections → Web search**, which is also where
that backend's credentials live:

| Backend                             | Works with                                 | Trade-off                                              |
| ----------------------------------- | ------------------------------------------ | ------------------------------------------------------ |
| Your provider's own web search      | Claude, GPT, Gemini only                   | No extra signup, but unavailable on local models       |
| [Exa](https://exa.ai) key           | **Every provider, local included**         | One extra free-tier signup; billed per search          |
| [SearXNG](https://docs.searxng.org) | **Every provider, local included**         | Free and fully self-hosted; you run and maintain it    |

**Why adding an external backend is worth it**, even when your provider can already search:

Exa is a search engine built for AI to read rather than for people to browse, and SearXNG is a
metasearch engine you host yourself that forwards queries to ordinary search engines. Either way
Shortlist does the searching and hands the findings to your model, which buys you four things:

1. **It's the only kind of backend that works with a local model.** An Ollama or LM Studio server on your own
   hardware has no way to reach the internet. With Exa or SearXNG, Shortlist does the searching and
   hands over the findings, so a fully offline model still recommends current titles.
2. **Your results stop depending on which AI you picked.** Switch from Claude to a cheap local model
   and the search half stays identical. Only the choosing changes.
3. **The cost is predictable.** Exa bills per search rather than per word, and those searches are
   reported separately from AI tokens. SearXNG costs nothing at all. Results are reused for 7 days
   and shared across everyone on the server, so a popular film is looked up once, not once per person.
4. **It is one clear choice.** Shortlist searches with exactly the backend you pick and no other, so
   a title is never searched — or billed — twice.

### Exa or SearXNG?

Both work with every provider, and both were measured end-to-end against the same watch history.

- **Exa** returns extracted page text — roughly 800 characters per result — so the model reads more
  about each title. It needs no infrastructure, and the free tier covers about 1,000 searches a
  month. It bills per search beyond that.
- **SearXNG** returns the underlying search engines' snippets, a couple of hundred characters each,
  so Shortlist pulls twice as many results per search to compensate. Nothing leaves your server
  except the queries SearXNG itself forwards, there is no account or key, and it costs nothing. In
  testing it was also *faster*, and its smaller results made the AI call noticeably cheaper.

Pick SearXNG if you already self-host and want no third-party account; pick Exa if you'd rather not
run another service. Only the backend you pick is used, so configuring one never quietly starts
the other.

**Setting SearXNG up.** Shortlist talks to its JSON API, which is **off in a stock install**. Add
`json` to `search.formats` in SearXNG's `settings.yml` and restart it:

```yaml
search:
  formats:
    - html
    - json
```

Without that, SearXNG answers Shortlist with a `403` and the source finds nothing. Shortlist's
**Test** button says exactly this if it happens. If your instance also has its bot `limiter` on,
allow Shortlist's address through, or it may be rate-limited.

**What SearXNG actually does.** It has no index of its own — it is a metasearch proxy. Each query is
forwarded to real search engines, and their results are merged, deduplicated and re-ranked. So your
queries do leave your network; what you avoid is a third-party account, an API key tying searches to
your identity, and a per-search bill.

That also means its reliability is only as good as those upstream engines' tolerance for a
self-hosted instance. On a test instance, one search returned 20 results from Google while Brave was
rate-limiting and DuckDuckGo and Startpage both served CAPTCHAs — normal, and fine as long as at
least one engine answers. If none do, **Test** reports which engines failed rather than a blank
"no results", so you can enable different ones in SearXNG's own settings.
   They reliably surface different films, so coverage is wider than either alone.

It is entirely optional. Leave it empty and everything still works. You are just limited to your
provider's own search, or to no web search at all.

### If you don't want to use AI

Leave the AI provider on **None** in Settings → Connections, which is the default, and the AI
web-search source off.

You still get full, per-person private rows. Candidates come from TMDB and Trakt, ranked by score,
with plain "Because you watched…" reasons. Everything about privacy, scheduling and requests works
exactly the same. The only thing you lose is the AI web-search source. Nothing else changes, because
nothing else used AI.

### Tuning AI cost

Cost comes entirely from the AI web-search source. Anthropic, OpenAI and Google charge per token; a
local Ollama or OpenAI-compatible server is free, but runs on your own hardware. Roughly
cheapest-to-priciest levers:

1. **Turn AI web search off, or limit which rows use it.** A row can override the global sources
   (Rows → Edit). Keep AI web search only on the rows that benefit and let the rest run on the free
   TMDB and Trakt sources.
2. **Search fewer recent watches.** The source runs one web search per person's recent watch, so
   lowering `recommendations.recent_count` (Settings → Finding titles) cuts searches. Results are
   cached for 7 days and shared across users, so a popular title is searched once server-wide.
3. **Use a small, cheap model.** A fast or mini model such as Claude Haiku, GPT-mini or Gemini Flash
   is plenty. You don't need a flagship model to read a few search results.
4. **Run less often.** Nightly is the default. A longer schedule means fewer runs and fewer searches.
5. **Use a local model.** An Ollama or LM Studio server on your own hardware costs nothing per run.
   This needs Exa or SearXNG, since a local model can't search the web itself. See
   [the backend table above](#the-source-that-uses-ai).

**Seeing where the tokens go.** Every run records its AI cost, so there is no guessing. Open a run
(Runs → click a run) and you'll see the **total AI tokens** for that run, then a per-person breakdown
by what the AI did, plus any **web searches**. Those are counted separately, since a search is
billed (or rate-limited) per request rather than per token. The runs list shows each run's token total at a glance. Use it to spot
which people cost the most, then tune with the levers above.
