# `llm_web` search upgrade — measured design

**Status:** design agreed, implementation not started.
**Date:** 2026-09-02.
**Scope:** `shortlist/engine/clients/search.py`, `shortlist/engine/curator/*`, `shortlist/engine/candidates.py`,
`shortlist/engine/clients/tmdb.py`, settings + web UI, docs.

Every number below came from probing the LIVE APIs with the maintainer's own keys (read from the
running container's settings, never printed). Nothing here is from documentation alone. Total probe
spend ≈ $1.20, most of it the Anthropic matrix.

---

## 1. Decisions

| Decision                   | Choice                                       | Why                                                                                                            |
| -------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Exa endpoint               | `/search` (owner's call)                     | `/answer` was cheaper and steadier, but `/search` carries the depth/result knobs and the owner wants them      |
| Exa `type`                 | user-selectable setting, default `deep-lite` | `auto` measurably breaks with `outputSchema` (0 titles on a 26k-char page set)                                 |
| Exa structured output      | `outputSchema` + `systemPrompt`              | Free — synthesis added $0 to the bill on every probe                                                           |
| LLM-proposed tmdb/imdb ids | **rejected**                                 | 4/10 tmdb ids correct; every wrong one resolved to a real, unrelated title                                     |
| ids from citation URLs     | accepted                                     | `imdb.com/title/tt…` can't be hallucinated                                                                     |
| Anthropic tool version     | stays `web_search_20250305`                  | Newer versions need `allowed_callers:["direct"]` on our default model, which is the mode where they do nothing |
| Anthropic `max_uses`       | 3 → 5                                        | Year coverage goes 4/12 → 10-12/12; the resolver needs the year                                                |
| Anthropic domain filters   | rejected                                     | `allowed_domains` 400s on common review sites                                                                  |

## 2. Exa: measured

### 2.1 Response shape (recorded for a fixture — rule 11)

`POST https://api.exa.ai/search` with `outputSchema` returns:

```
requestId, resolvedSearchType, searchTime, results[], costDollars{total, search{neural}},
output {
  content:   { <matches outputSchema> }        <- the structured extraction
  grounding: [ { field: "titles[0].title", citations: [{url, title}], confidence: "high" } ]
}
```

`results[]` items keep today's keys: `favicon, id, image, publishedDate, text, title, url`.

`POST /answer` with `outputSchema` returns `{requestId, answer, citations[], costDollars}` where
`answer` is the schema-shaped object and citations carry full page `text` when `text: true`.

### 2.2 Search type comparison

Two seeds (Severance, The Bear), `numResults=10`, 3000-char text, identical schema and system
prompt. "Usable" = title resolves to a real TMDB entry, so the metric doesn't reward hallucination.

| type             | usable (Severance) | usable (The Bear) | cost       | latency | notes                                               |
| ---------------- | ------------------ | ----------------- | ---------- | ------- | --------------------------------------------------- |
| `instant`        | 9                  | 10                | $0.007     | 2.5s    | year on only 1 of 9 — unusable for the resolver     |
| `fast`           | 24                 | 9                 | $0.007     | 3.5s    | erratic                                             |
| `auto`           | 13                 | 8                 | $0.007     | 4.0s    | erratic; returned **0 titles** on one Severance run |
| **`deep-lite`**  | **47**             | **36**            | **$0.012** | **11s** | best usable-per-dollar ($0.0003)                    |
| `deep`           | 19                 | 30                | $0.012     | 6.5s    | steadier year coverage, fewer titles                |
| `deep-reasoning` | 21                 | 26                | $0.015     | 30s     | dearer, slower, worse                               |

`numResults` barely moves the yield: n=5, 10 and 20 all landed in the same range, and n=20 costs
$0.017 (base + $1/1k for results past 10). **Stay at 10.**

### 2.3 Consistency

Three identical `deep-lite` calls on Severance: 36, 45 and 38 usable titles, sharing only **45%**
(25 of 55 union). The stable core is strong — Counterpart, Devs, Mr. Robot, Dark Matter, Mrs. Davis,
Playtime, Silo, Upload, Westworld. Consequence: a thin draw would be cached for 14 days, so **do not
cache a result below a floor of usable titles**.

One call returned non-JSON. A parse failure must degrade to "this seed contributed nothing", never
fail the run.

## 3. The ids finding (why the original ask was dropped)

Asked the live curator (haiku-4-5, web search on) for 10 titles WITH ids, then verified every id
against TMDB:

```
Black Mirror   → tmdb 20574 → "Wild China"
Homecoming     → tmdb 77507 → a Japanese adult film
Mr. Robot      → tmdb 44856 → "Wentworth"
Devs           → tmdb 93053 → a Thai film
Orphan Black   → tmdb 45825 → "The Battery"
Maniac         → tmdb 74662 → "Limit"
Lost, Westworld, Silo, Dark → correct

tmdb_id 4/10 right.  imdb_id 6/10 right (Westworld's imdb id → "Fast X").
```

Today a hallucinated title resolves to nothing and vanishes. A hallucinated id resolves to a real,
wrong title and reaches someone's row. The titles it got wrong are ones `tmdb.search(title, year)`
already resolves correctly, so ids add risk and an API call for no gain.

**The real fix is the resolver**: `tmdb.search` (`clients/tmdb.py:110`) returns `results[0]` blindly,
with no year or title check.

## 4. Anthropic native path: measured

Five seeds, k=12, same TMDB-resolution metric.

| config                          | usable    | with year | searches | tokens in | cost   | latency |
| ------------------------------- | --------- | --------- | -------- | --------- | ------ | ------- |
| `max_uses=1`                    | 11/12     | 4         | 1        | 11,502    | $0.024 | 5.4s    |
| `max_uses=3` **(shipping)**     | 10/12     | 4         | 3        | 22,865    | $0.055 | 5.9s    |
| **`max_uses=5`**                | **12/12** | **10**    | 5        | 52,095    | $0.106 | 9.1s    |
| `max_uses=10`                   | 11/12     | 11        | 8        | 100,497   | $0.185 | 11.5s   |
| strict prompt, 5                | 12/12     | 12        | 5        | 63,961    | $0.118 | 12.7s   |
| `blocked_domains`               | 12/12     | 12        | 5        | 91,828    | $0.146 | 10.7s   |
| `user_location AU`              | 12/12     | 12        | 5        | 59,706    | $0.114 | 14.0s   |
| new tool + `response_inclusion` | 12/12     | 12        | 5        | 60,271    | $0.114 | 12.6s   |

- `allowed_domains` with review sites returns **HTTP 400**: "The following domains are not accessible
  to our user agent: ['reddit.com', 'vulture.com']". Any future domain-filter feature must handle it.
- `web_search_20260209` / `web_search_20260318` both 400 on `claude-haiku-4-5` without
  `allowed_callers: ["direct"]` ("does not support programmatic tool calling"). With `direct` they
  work but produce identical output at identical cost — dynamic filtering needs Claude 4.6+.
- **Cost shape:** native is ~$0.10 per user per run and never amortises. Exa `deep-lite` is $0.012
  per _seed_, cached 14 days and shared across the roster. On 40 users: ~$126/mo native vs $30–90 Exa.

## 5. SearXNG

Measured against a real instance — a throwaway `searxng/searxng` container on the plex host,
removed afterwards along with its image. Metric is end-to-end so it compares with the Exa numbers:
fetch the page, hand the top 20 snippets to the same haiku curator the pipeline uses, TMDB-verify
what it extracts.

First matrix (Severance, one run each, `categories=general`):

| config                            | results | snippet chars | usable titles |
| --------------------------------- | ------- | ------------- | ------------- |
| baseline (what we ship)           | 20      | 3,148         | 12            |
| `safesearch=1`                    | 20      | 3,159         | 12            |
| `safesearch=2`                    | 20      | 3,148         | 11            |
| `language=en`                     | 20      | 3,212         | 12            |
| `time_range=year`                 | 20      | 2,977         | **20**        |
| 2 pages                           | 40      | 3,148         | 11            |
| no `categories`                   | 20      | 3,148         | 12            |
| `engines=google,duckduckgo,brave` | 10      | 1,668         | 14            |

`time_range=year` nearly doubling the yield contradicted the prediction, so it was re-run across
three seeds, twice each:

| seed        | baseline | `time_range=year` | `time_range=month` |
| ----------- | -------- | ----------------- | ------------------ |
| Severance   | 12, 11   | 22, 20            | 28, 26             |
| The Bear    | 28, 28   | 23, 23            | 27, 27             |
| Poor Things | 18, 19   | 15, 15            | **9, 8**           |

**So `time_range` is seed-dependent and must NOT be set globally.** It helps a current show with
heavy recent coverage (Severance) and badly hurts an older film (Poor Things, 2023) by cutting out
the evergreen articles that are its only coverage. The single-seed result was misleading; the
original prediction was right.

Also worth noting: SearXNG is far more reproducible than Exa — run 1 and run 2 agree almost exactly,
against Exa `deep-lite`'s 45% overlap between identical calls.

**Settings to adopt:** keep `categories=general`, add `safesearch=1` (free — identical yield, and a
Plex recommender should not surface adult results). Skip `language` (no effect), skip an `engines`
override (fewer results for denser ones, and it hardcodes engine names that instances may not have),
skip `time_range`, and leave `pageno` at 1 — page 2 adds nothing under the snippet cap the prompt
applies anyway.

## 5a. OpenAI and Google: measured (2026-09-02, owner's keys)

Both keys authenticate. Neither could be fully exercised, but the attempts produced three findings
that change the code regardless.

**LIVE BUG — `curator/google.py:14` `DEFAULT_MODEL = "gemini-2.5-flash"` is dead for new users.**

```
404 NOT_FOUND: This model models/gemini-2.5-flash is no longer available to new users.
               Please update your code to use models/gemini-3...
```

`gemini-2.5-flash-lite` returns the same. Any new install choosing Google today gets a 404 out of
the box. This is independent of this whole feature and should ship as its own fix. The API key's
model list carries `gemini-flash-latest` and `gemini-flash-lite-latest` aliases — an alias is the
right default precisely because it can't rot the way a pinned retired model just did.

**Gemini structured output works, but not with a JSON-Schema union type.** `{"type": ["integer",
"null"]}` is rejected by google-genai's own `Schema` validator before any request is sent; Gemini
wants a single `type` plus `"nullable": true`. Schema-only calls (no grounding) scored 11/12 and
10/12 resolving titles on `gemini-3-flash-preview` and `gemini-3.1-flash-lite`.

**Grounding is unavailable on a free-tier key.** Every grounded request returned
`429 RESOURCE_EXHAUSTED`, on preview and GA models alike. Billing must be enabled on the key's
Google Cloud project. Re-measured below on a billed key.

### The Google finding that matters: Gemini does not search for this task

On a billed key, grounding + `response_schema` works fine — no error, 11–12/12 usable titles on
`gemini-flash-latest`, `gemini-3.8-flash`, `gemini-flash-lite-latest` and `gemini-pro-latest`. But
`grounding_metadata.web_search_queries` came back **empty on every recommendation run**. Gemini
attached the tool and answered from training data.

Verified two independent ways, because proving "no search happened" from the same metadata field
under suspicion would be circular:

1. **The tool demonstrably works.** Asked for today's date and a headline from the last 48 hours,
   `gemini-flash-latest` returned both correctly with two `grounding_chunks` citing cbc.ca and a
   populated `web_search_queries`. The identical call with no tool attached replied "I do not have
   access to real-time information". So the field reports correctly when a search runs.
2. **The recommendations are visibly stale.** The plain prompt returned Ripley (2024) and Blue Eye
   Samurai (2023); 0 of 12 titles were from 2025 or later.

Three attempts to force a search, all failed:

| attempt                                                                                             | result                                                                         |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| "You MUST use Google Search… do NOT answer from memory" in the system prompt                        | `searches=0`                                                                   |
| `tool_config` with `FunctionCallingConfig(mode="ANY")`                                              | `searches=0`, and returned an **empty response** after 47s                     |
| Reframed as retrieval ("search for articles titled 'shows like Severance' and list what they name") | `searches=0`, returned titles from 2019–2020                                   |
| Legacy `google_search_retrieval` with `dynamic_threshold=0`                                         | `400: google_search_retrieval is not supported. Please use google_search tool` |

**Consequence:** on Google, `llm_web` is not a web source — it is the model's memory, and it
silently returns years-old titles. Anthropic always searched (5 of 5) and OpenAI searched every run.
This has to be surfaced, not hidden: read `web_search_queries`, record "answered without searching"
in the run trace, and say so on the Test button. Never send `mode="ANY"` — it breaks the response.

## 5b. OpenAI: measured (second key, credited)

Five seeds, k=12, same TMDB-resolution metric as every other provider.

| config                               | usable    | with year | searches | tokens                 | latency |
| ------------------------------------ | --------- | --------- | -------- | ---------------------- | ------- |
| `gpt-4o-mini`                        | 12/12     | 12        | 1        | 8,174 in / 297 out     | 7.1s    |
| `gpt-4o`                             | 3/4       | 3         | 1        | 32,555 in / 96 out     | 7.3s    |
| `gpt-4.1-mini`                       | 12/12     | 12        | 1        | 8,174 in / 308 out     | 12.7s   |
| `gpt-5-mini`                         | 12/12     | 12        | 1        | 10,334 in / 2,280 out  | 27.8s   |
| `gpt-5`                              | 12/12     | 12        | 3        | 20,106 in / 3,737 out  | 47.7s   |
| `search_context_size: low`           | 10/12     | 10        | 1        | 8,174 in               | 4.4s    |
| `search_context_size: medium`        | 11/12     | 11        | 1        | 8,174 in               | 5.5s    |
| `search_context_size: high`          | 12/12     | 12        | 1        | 8,174 in               | 7.8s    |
| **web_search + `json_schema`**       | **12/12** | **12**    | 1        | 8,174 in / **212 out** | 5.8s    |
| `json_schema`, no web_search         | 11/12     | 11        | 0        | 233 in / 189 out       | 2.9s    |
| `gpt-4.1-mini` + web_search + schema | 12/12     | 12        | 1        | 8,174 in / 214 out     | 6.8s    |
| `user_location AU`                   | 10/12     | 10        | 1        | 8,174 in               | 4.4s    |

**The unknown is answered: `web_search` and Structured Outputs DO combine.** Confirmed on both
`gpt-4o-mini` and `gpt-4.1-mini`, and output tokens fell from ~300 to ~212 because the model stops
narrating. No fallback dance needed on these models — keep one anyway for older ones.

**`filters` is a GPT-5-family feature.** 400 on `gpt-4o-mini` and `gpt-4.1-mini` ("Parameter
'filters' not supported with model 'X'"), works on `gpt-5-mini`. Same shape as Anthropic's
`allowed_domains` 400: **domain filtering is model-gated on both providers and must never be sent
blind.**

**`search_context_size` — use `high`, but the evidence is thin.** Resolve rate went 10 → 11 → 12 of
12 across low/medium/high while input tokens stayed _identical_ at 8,174, which suggests the
difference is run-to-run variance rather than a real effect. `high` costs nothing measurable, so
take it, but don't treat the gain as established.

**Cost — OpenAI native is ~10× cheaper than Anthropic native.** Web search is $10/1k calls on every
model with no premium for context size, plus search content tokens at model rates. `gpt-4o-mini` at
one search: $0.010 + 8,174 in @ $0.15/M + 297 out @ $0.60/M = **$0.0113 per user per run**, for the
same 12/12 that Anthropic charges **$0.106** for (5 searches, 52k input tokens). `gpt-4o` is the one
model to avoid: it returned 4 titles from 32k input tokens.

## 5c. Two findings from building it

**A fourth schema field breaks Exa entirely.** The design called for `{title, year, media, why}`,
the `why` being a one-line reason per title that would give the curator something to match a taste
profile against. Asking for it made every `deep-lite` search exceed Cloudflare's 100s limit and
return an HTML **524** — 5 attempts out of 5, ~125s each — while the identical request without it
answered 200 in 10.3s with 44 titles. Exa's synthesis time scales with what you ask it to write and
the ceiling is the CDN's, not a timeout we can raise. `why` is dropped, and
`_EXA_TITLE_SCHEMA` carries a warning that any new field needs the same live check.

This is also why `ExaClient`'s default timeout went from 20s to 60s: the old value was below what
the mode that works actually takes.

**Ids from citation URLs don't exist.** The plan was to take imdb ids from result URLs, since those
can't be hallucinated. Checked against the recorded response: every citation and result URL is an
_article_ — screenrant.com, digitaltrends.com, tvguide.com — and not one is an `imdb.com/title/tt…`
page. An article URL identifies the article, not the titles inside it. So there are no free ids, and
the ids work is entirely the resolver hardening.

That matters more than expected: the recorded extraction carries a year for only **15 of 31**
titles, because a source article often doesn't print one. So `tmdb.search` was changed to use the
year to RANK rather than to filter — filtering server-side loses a title outright when the year is
one out (common: sources date a series by its premiere and a film by its festival run), and does
nothing at all for the half that have no year.

## 5d. Audit rounds — six defects found after "it works"

Every test passed and the suite was green before any of these were found. They came from two things
tests could not do: running the SHIPPED classes against the live APIs (every earlier number came
from probe scripts that reimplemented the request shape), and re-reading the diff for costs that no
assertion checks.

| #   | Defect                                                                                                                                                                                                                                       | How it surfaced                                   |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| 1   | **The seed was recommended back to its own watcher.** An article headed "shows like Severance" names Severance, the extraction listed it, and the curator proposed it.                                                                       | Live run — visible in the proposals               |
| 2   | **Thin results were never cached, so they re-billed every user every night.** The floor rejected the cache entry outright; a seed with little written about it would be a fresh search forever. Now cached for a day instead of a fortnight. | Reading the diff                                  |
| 3   | **One seed's failure killed the whole source for that user.** A single raised search discarded every seed that had already succeeded. Now isolated per seed — and if EVERY seed fails it still raises, so a dead backend stays loud.         | Reading the diff, then confirmed by #6            |
| 4   | **A rejected schema was retried but not remembered**, so a model that refuses it pays a doubled call for every user, every night, visible only at DEBUG. Now learned once per process.                                                       | Comparing the design doc against the code         |
| 5   | **A malformed cache row would take down the source** for that user. Cache rows outlive the process and survive upgrades. Now guarded.                                                                                                        | Reading the diff                                  |
| 6   | **A flat 60s timeout wasted a minute per stall.** Exa hangs rather than answers on roughly 1 search in 6; successes run 6.5-18s. Now per mode: 20s cheap, 45s deep, 90s `deep-reasoning`.                                                    | Live measurement after a timeout killed the audit |

**Operational note from #6:** `deep-lite` failed 1 of 6 live searches (a hang, not an error). That is
survivable — 10 seeds per user, shared cache, and defect #3 is now fixed so the rest still land — but
it means some seeds contribute nothing on any given night, and the run log will say so.

## 6. Implementation plan

1. **`ExaClient`** — `type` from settings (default `deep-lite`), `numResults=10`, `outputSchema` for
   `{title, year, media, why}`, `systemPrompt`, keep `contents.text` for the trace. Parse
   `output.content.titles`; tolerate a missing `output` and fall back to prose results.
2. **`WebSearchProvider`** — optional extraction method. Exa implements it; SearXNG keeps the prose
   path unchanged, so both shapes flow through `_web_via_search`.
3. **Cache** — versioned key, store titles + snippets together, skip caching a thin result.
4. **Resolver** — `tmdb.search` gains year proximity and title similarity instead of `results[0]`;
   imdb ids parsed from citation URLs only, verified before use.
5. **RAG cap** — raise it. It exists because prose blocks are expensive; a title list is ~10 tokens
   each, so the curator can finally see everything we found.
6. **Anthropic** — `max_uses` 3 → 5, strict prompt (exact year required, no sequels of seed titles).
   Tool version unchanged.
7. **OpenAI** — `search_context_size: "high"`, and send `text.format: json_schema` alongside
   `web_search` (measured working; keep the tolerant parser as the fallback for older models). Send
   `filters` only on a `gpt-5*` model, never blind. Skip `user_location` — it scored worse.
8. **Google** — `DEFAULT_MODEL` → `gemini-flash-latest` (the 2.5 default 404s for new users; ship
   this as its own fix). Send `response_schema` alongside grounding (measured working), built
   Gemini's way: single `type` + `nullable`, never a union. Fix the stale "grounding is incompatible
   with a response schema" comment at `curator/google.py:52`. **Read
   `grounding_metadata.web_search_queries` and record it** — an empty list means the model answered
   from memory, which is the normal case for this task and must show in the run trace and on the
   Test button. Never send `tool_config mode="ANY"`.
9. **Settings + UI** — `exa.search_type` select beside the Exa key, helper text naming each mode's
   real cost.
10. **Tests + docs** — recorded fixture for the `/search` + `outputSchema` shape, resolver tests
    including the wrong-id case, `docs/reference.md` and `docs/guides.md` updated.

## 7. Status

All five backends measured against live APIs. Implemented; not yet committed.

**Verification:** `ruff check` clean · **3811 unit + integration tests pass, 1 skipped** · `tsc -b`
clean · **1395 web tests pass** · a 28-check live audit of the shipped classes against the real Exa,
Anthropic, OpenAI, Google and TMDB APIs, passing end to end including the cache round-trip.

**`pytest -m e2e` was NOT run** — it needs Playwright browsers, which are in neither the runtime
image nor this machine. A settings select was added, so CI's e2e job is the first thing that would
catch a wizard regression.

Also unverified: behaviour at roster scale over a real nightly run, and whether the measured
settings hold for a library unlike the test seeds (all prestige TV — see §8).

## 8. Open items

- Google grounding costs, if anyone turns it on: Gemini 3.x gets **5,000 free grounded searches per
  month**, then $14/1k (Gemini 2.5: 1,500/day free, then $35/1k). Academic while Gemini declines to
  search for this task.
- The OpenAI and Google keys used for the 2026-09-02 probes were pasted into a chat transcript —
  **rotate them**.
- Not built, deliberately: **resolving a blank `curator.model` from the provider's live model list**
  rather than a hardcoded constant. That is the general fix for the bug class the retired
  `gemini-2.5-flash` default exposed, and it would prevent the same thing happening to
  `claude-haiku-4-5-20251001` and `gpt-4o-mini` later. An alias default fixes Google today without
  the refactor; the refactor is still worth doing.
- Not measured: whether these settings hold on a **library unlike the test seeds**. Everything was
  measured on Severance, The Bear, Andor, Poor Things and Shogun — prestige TV. The large effects
  (`auto` returning nothing, `max_uses=3` halving year coverage, the `why` 524) are structural
  enough that seed choice should not flip them, but that has not been shown.
- Single run per config in most matrices, so small deltas are noise — OpenAI's `search_context_size`
  going 10 → 11 → 12 of 12 with identical input tokens most likely is.
