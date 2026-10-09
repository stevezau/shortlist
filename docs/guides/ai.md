---
title: "AI and cost: AI-powered Plex recommendations"
description: Shortlist works with no AI at all. What AI adds when you turn it on, which search backend to pick, and how to control what it costs.
heading: AI and cost
---

## What AI does, and what it costs

**AI is off by default.** The provider is set to "None" out of the box and the AI web-search source
is off, so Shortlist works fully with no AI at all.

AI has two jobs: the **AI web-search source** and, if you create one, an [AI row](#an-ai-row) (written once, not nightly). For ordinary rows it has one job, the **AI web-search source**, which finds acclaimed "what to watch next"
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
keeps the ones you own. In testing this was a strong extra source, surfacing well-reviewed titles
the TMDB lists simply don't return. It is the only place AI spends anything, and it is off by
default.

**How it works.** Shortlist takes each person's recent watches and turns them into real web searches
("what to watch if you liked X"), then hands the results to your model, which picks from what was
found. The model never browses. It reads.

That order is what makes it work. Because Shortlist runs the search rather than the model, the source
works with **any** provider, including a local Ollama, llama.cpp or LM Studio server with no internet
access of its own. A local model that could never search the web still gets to recommend from current
web results.

**Telling it what to look for.** You can add your own guidance for the AI, such as "prefer
award-winning dramas" or "no horror". Set a server-wide default in **Settings → Defaults → Title
sources**, or write it per row in the row editor under **What goes in**. Shortlist still checks every
suggestion against your library and what each person may see. The
[full details](rows/what-goes-in.md#ai-instructions) cover the modes and what each backend does with it.

You choose the search backend in **Settings → Connections → AI & Web search**, which is also where
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

It is entirely optional. Leave it empty and everything still works. You are just limited to your
provider's own search, or to no web search at all.

### An AI row

An **AI row** is a row you describe in your own words. It is the one place AI writes a list for you,
and it does so once, not every night.

**To make one:**

1. In **Add a row**, choose the AI filter, then the **Describe a row** template.
2. Under **What should this row be?**, write what you want, for example "slow-burn heist films". It is used
   once, to write the list, and is not shown on Plex. Pick movies, shows or both.
3. Click **Write the list**. The AI proposes a theme: a name, rules, TMDB tags and genres, and about 60
   named titles with a one-line reason each.
4. Read the result. Shortlist shows how many titles it found on TMDB, how many are in your library,
   and how many survive the row's limits. Click **Adjust the list** and say what to change; your own
   description stays as you wrote it, and you see what was added and removed (titles, tags and genres)
   before you keep anything. Tags and genres you added by hand stay unless the AI says to drop them.
5. Click **Try it** and choose a person. It runs the row for them without writing anything to Plex.
   You can try a row that is switched off.
6. Save. An AI row goes live like any other row. If you would rather look it over first, switch it off
   before you save.

**Every night after that uses no AI.** Each person's row is filled from the saved theme (its tags,
genres and named titles), kept to titles in your library they have not watched, and ranked by their
own taste. Two people get different rows from the same theme. An AI row only ever picks from what you
already have: a title the AI named that your server lacks is left out and never requested, so the row
has no request settings.

**An AI row only fills the kinds of title its AI named.** If the list is films only, the row stays out of
your TV libraries even when the row covers both. If **Adjust the list**, or an Explore theme, drops a kind, the
row's existing collection in those libraries is removed on its next run.

**A list is topped up once.** When someone has watched most of the titles the AI named, their row starts
filling with tag and genre matches. The nightly **Pick new row themes** job then asks the AI once for about 40
more titles and adds them to the list, keeping everything already there. It never does this twice for the
same theme, a paused row or a missing provider is skipped, and nothing else about a night uses AI.

**Controls in the editor:**

- **Advanced: how the AI is instructed** (closed until you open it) shows what the AI is told. You can
  edit the guidance. The mechanics that keep the answer machine-readable are locked.
- **Usage** shows the tokens each row has spent building and changing its theme.
- **Pause AI for this row** stops the row spending tokens. It keeps its theme and keeps filling from
  it every night. Resume it when you want to edit the theme again.

**Needs an AI provider** (Settings → Connections). With none, the AI half of the editor is hidden and
you can still edit the tags, genres and limits by hand. AI rows are per-person rows only.

### Explore: a new theme every few days

By default an AI row keeps the one theme you built. Switch it to **Pick a new theme every few days** (Row →
**What goes in**, under the list) and each person's row gets a fresh theme on a schedule instead.

- **Per person.** Every person gets their own theme, chosen from what they watch. AI rows are never shared
  rows, so two people on the same row can be on different themes.
- **Days each theme lasts.** 7 unless you change it (1 to 90).
- **What kinds of lists should it pick?** Optional. Leave it blank and the AI chooses from each person's watching; write
  "cosy mysteries" and every theme leans that way.
- **Cost.** One call to your AI provider per person per change, counted against the row's usage. Nothing
  else in a night uses AI.
- **Up next.** The next theme is written a day before it starts, so you can look at it. On each person's
  card, **Change it** lets you say what to change (you see what would be added and removed before it is
  saved) and **Pick another** asks the AI for a different one. Both need AI not to be paused. Neither
  touches Plex: the row picks up the new theme the next time it builds.
- **Recent themes.** The last six themes for each person are listed, and Shortlist won't pick those again
  soon.

A background job, **Pick new row themes**, does the switching and writing. It runs once a day by default;
change or switch off its schedule on the **Jobs** page (setting `themes.rotate_cron`). If the AI is
paused or unreachable, the person keeps their current theme and the problem shows in the change log.

Nothing changes when you upgrade: every AI row starts on **Keep the same theme**.

### How a row changes over time

These controls are on AI rows only. Each starts at today's behaviour.

- **How much changes each time.** On each refresh, how much of the row is swapped for new titles: *A
  little (about a fifth)*, *A third (usual)*, *Half*, or *Almost everything*. Default: a third.
- **Don't repeat a title for N days.** Off by default. When on (1 to 365 days, 30 to start), a title that
  has been in the person's row stays out for that long. The days count from the first time the title was
  shown, not the last. It never removes a title the row is keeping tonight; it only stops it coming back
  as a new pick.
- **Keep out titles already in.** Tick other per-person rows to keep their titles out of this one for the
  same person. None by default. It depends on build order: a row built earlier in the same run is kept out
  exactly; a row built later is kept out by what it showed on its previous run.

**My row stopped changing.** The controls can use up the pool: a long no-repeat period or several keep-out
rows can leave too few titles. When that would leave the row with nothing new to pick, Shortlist ignores
the controls for that night and says so in the run, so titles can repeat that night. A small pool also
makes the row shorter than its size, because there are not enough titles to fill it. Shorten the days,
untick a row, or widen the theme's limits.

A theme with only a few named titles keeps rotating among them unless the no-repeat control is on.

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
   lowering how many recent watches it looks at (Settings → Defaults → Refresh & variety) cuts searches. Results are
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
billed (or rate-limited) per request rather than per token.

The token figure is input plus output, as your AI provider reported each call, which is what it
bills on. The tile shows the two apart, because output costs several times more than input. Web search
is the only step that uses the AI, so every token is a web-search token. With Claude nothing is cached, so it is every token sent and received. OpenAI and Gemini
include any input they served from their own prompt cache, which they bill at a discount. The 7-day
web-search cache saves searches, not tokens. The runs list shows each run's token total at a glance. Use it to spot
which people cost the most, then tune with the levers above.
