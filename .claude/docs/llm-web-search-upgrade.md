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

## 5e. Live verification on SFLIX (dry runs, MooHouse — 30 seeds, 49-user install)

Deployed to the live container and run for real. Three dry runs, each after deploying the fix the
previous one exposed.

| run | searches | cached | proposals | already-watched dropped | resolved | unresolved | seed leak | time |
| --- | -------- | ------ | --------- | ----------------------- | -------- | ---------- | --------- | ---- |
| 15  | 20       | 0      | 80        | — (no filter yet)       | 80       | 0          | **6**     | 224s |
| 16  | 23       | 22     | 76        | 4                       | 76       | 0          | **1**     | 23s  |
| 17  | 23       | 23     | 54        | **26**                  | 50       | 4          | **none**  | 13s  |

**The bug the live run found, which nine audit rounds and 3800 tests did not.** The model proposes
already-watched titles even when told not to and even when they are stripped from the list it is
shown — it fills them back in from its own knowledge. Run 15 returned six seeds among 40 proposals.
Filtering on the pool's seeds took it to one; the survivor was watched but seeded in a _different_
pool, which is the pool boundary showing through. Matching the whole history took it to none.

**26 of 80 proposals were already-watched titles** — a third of what the model was asked for. They
never reached a row (the downstream watched-filter has always caught them) but they were spending
the k, and nothing counted them. `already_watched` is now in the run trace, so a prompt that starts
wasting the budget is visible rather than inferred.

The cache behaves as designed: run 15 paid for 20 searches, runs 16 and 17 served 22 and 23 of 23
from cache and billed nothing, and the run dropped from 224s to 13s. `unresolved: 4` on run 17 is
the expected hallucination tail — those titles resolve to nothing and vanish, which is the whole
reason ids were rejected in §3.

## 5f. Live verification at full scale (run 18 — 46 users, REAL run, 2026-09-03)

The first non-dry run on the upgraded code, and the roster-scale measurement §7 listed as missing.
Owner-triggered from the UI. 62 minutes, `status: ok`, **46 of 46 users ok, 0 errors, 0 promotion
blockers**.

| measure                              | run 18            |
| ------------------------------------ | ----------------- |
| billable searches                    | 662               |
| cache hits                           | 380 (**36.5%**)   |
| cost at `deep-lite` ($0.012/search)  | **$7.94**         |
| titles added / removed               | 848 / 822         |
| LLM tokens                           | 350,637           |
| proposals reaching the resolver      | 3,204             |
| already-watched proposals dropped    | **378**           |
| unresolved against TMDB              | **23 (0.7%)**     |
| failed seeds                         | none              |
| rows using structured extraction     | 92 of 92          |

**The already-watched filter is load-bearing at scale, not a MooHouse curiosity.** The curator
proposed 3,582 titles across 46 real histories and 378 of them — **1 in 10** — were things that
person had already watched. §5e measured this on one account with 30 seeds and could not tell
whether that account was unusual. It is not.

**0.7% unresolved** is the year-ranking change in `TmdbClient.search` holding up across 46 libraries:
year now ranks candidates instead of filtering them, so a title whose year the model got wrong still
resolves to the right entry rather than vanishing.

**Cost, which nothing had measured before.** $7.94 a night is ~$238/month at `deep-lite`, and the
36.5% hit rate is steady state rather than a cold start — the `websearch2:` namespace had been
filling for 13 days. The cheap tiers would be ~$139/month, so the depth setting is a real ~$100/month
decision, against the 47-and-36 vs 13-and-8 usable-titles gap in §2.2.

**`shares_updated: 0`** is the expected steady state, not a skipped step: the `label!=shortlist_*`
excludes were already correct from previous runs, so the read-modify-write merge found nothing to
change. Plex-safety rule 1 still ran.

**A false alarm worth recording.** `extracted` read exactly 25 on all 92 rows, which looks like a cap
discarding most of what Exa found. It is `_TRACE_RETURNS_SAMPLE` — a trace display sample. The prompt
still receives `candidates[:_WEB_PICK_CAP]` (300). Anyone reading these traces will notice the
uniform 25 and should not chase it.

## 5g. Two findings RETRACTED, and what replaced them (2026-09-03)

Both were reported to the owner as fact and both were wrong. Recorded here in full, because the way
each was reached is more useful than the conclusion.

### Retraction 1 — "Exa with no AI burns $7.94 a night"

**Claimed:** with an Exa key and the provider set to None, every run paid for every search, extracted
the titles, then discarded them. Reported as a live production bug on a 46-user server.

**Actually:** `gather_candidates` gated the whole source on a separate `llm_ready` check —
`curator is not None and not isinstance(curator, NullCurator)` — which ran BEFORE
`_web_search_capable` was consulted. With no AI provider the source never ran at all. **Zero searches
fired. No money was ever spent.**

**How the error was made:** the probe called `web_recommendations` directly instead of
`gather_candidates`. The inner function does bill and discard when reached that way; production never
reaches it that way. This is the exact failure mode `.claude/rules/testing.md` and the "confirm a fix
by running the real code" note both warn about — a reimplementation of the call path is not the call
path, and here even calling the *real* function at the wrong level was enough to invent a bug.

**What was true underneath:** Exa really does extract titles unaided, so keyless Exa is a genuine
capability — it was simply never reachable. Turning it on (owner decision, 2026-09-03) meant removing
`llm_ready` so `_web_search_capable` is the single authority, and matching `hasWebSearch()` in
`sources.ts`. Pinned by `test_candidates.py::TestWebSearchWithoutAnLlm::
test_gather_candidates_really_runs_the_source_with_no_ai`, which goes through the front door.

**Verified live** through `gather_candidates` with real keys: `none + exa` at `deep-lite` → 1 billable
search, **39 candidates**, no AI provider present. Eight further provider x backend cells behaved
exactly as designed against the live Exa, Anthropic, OpenAI and Google APIs.

### Retraction 2 — "Gemini's picks are stale training data"

**Claimed:** Gemini attaches the grounding tool, never searches, and therefore returns stale titles —
so the native option should be disabled for it.

**Actually:** the first half holds and the second does not. Re-measured under the year-anchored
prompt (§5h): `web_search_queries` is still empty, and a control question that cannot be answered
from memory still makes the same client issue three real queries. But the titles it returns from
memory were **12 of 12 from 2024 or later** — The Pitt, Pluribus, Adolescence, Task — overlapping what
the searching control found. Its training data is simply recent enough.

**How the error was made:** the original measurement used the OLD prompt, which never named the year.
That prompt was later found to be the whole problem for OpenAI too (§5h). Repeating a finding without
re-running it after changing the thing it depended on is how a stale measurement becomes a false
claim.

**What replaced it:** Gemini stays selectable and unflagged as broken. The log line dropped from
WARNING to INFO and now states the real cost — it cannot refresh itself as its cutoff recedes, where
Claude and GPT can.

## 5h. The prompt fix: make native search look forward (2026-09-03)

`_WEB_SYSTEM` asked for "current, well-reviewed titles". A model has no reliable idea what today is,
so *current* anchors to its training cutoff — the wrong end — and the tool being attached is not an
instruction to use it. Both are now explicit: the prompt carries the current year and last year, and
says the model's own knowledge is out of date.

Same models, same seeds, same day; only the prompt changed:

| model              | 2024+ before | 2024+ after | note                                    |
| ------------------ | ------------ | ----------- | --------------------------------------- |
| `gpt-4o-mini`      | 0 / 12       | **12 / 12** | 8,384 tokens, 5.9s                      |
| `gpt-5-mini`       | 0 / 12       | **9 / 12**  | 67,612 tokens, 100s — searches far harder |
| `claude-haiku-4-5` | not measured | 12 / 12     | no control run; not evidence of a gain  |

**The over-correction, and the guard.** The year anchor worked too well at first: `gpt-4o-mini` began
returning UNRELEASED 2026 titles and "Season 3" entries. Two rules fixed it — only already-released
titles, and never name a season. The numbers above are the second measurement.

**A cost worth knowing.** `gpt-5-mini` now spends 8x the tokens and 17x the wall-clock of
`gpt-4o-mini` for a slightly worse result, which is an argument for changing the OpenAI default back.
Left as an owner decision rather than flipping it twice in one session.

## 5i. The Settings merge (2026-09-03, owner decision)

The AI provider card and the Web search card became one, titled **"AI & Web search"**. This reverses
the position taken earlier in the same session, and the reason it was wrong is worth keeping.

**The objection:** the AI key has a second consumer — `poster_service.make_studio` builds the
image backend from `curator.provider` + `curator.api_key` (OpenAI and Google only) — so it cannot
live inside a box called "Web search". **Why it did not hold:** the owner's name for the card owns
both things explicitly, and the provider picker having exactly ONE home is what prevents the
duplication that `ai-web-search-card.tsx` records ("no way to tell which one was authoritative").

The provider list is now a function of the chosen backend, because the branches genuinely differ:

| Search with          | AI provider | Selectable providers                                    |
| -------------------- | ----------- | ------------------------------------------------------- |
| Exa                  | optional    | all, None included — Exa extracts titles itself          |
| My AI's own search   | required    | Claude, OpenAI, Gemini — **not local**, no search tool   |
| My SearXNG           | required    | all except None — raw snippets need reading              |

Two bugs were introduced by the merge and caught before it shipped: `offers()` defaulted an unset
backend to `native` while the new helpers defaulted to `exa` (a fresh install would have shown the
wrong provider list), and the single Test button had to learn to probe the external backend when one
is configured and the AI provider otherwise — which is what keeps heuristic mode's "no AI, nothing to
test" answer working.

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

All five backends measured against live APIs. **Shipped to `dev`** (77dcd27, 46924a1, cb15267,
01ff07f) and verified in production by run 18 — see §5f. The settings-UI polish and the model-alias
work that followed are a later change.

**Verification:** `ruff check` + `ruff format --check` clean · **3,833 unit + integration tests pass,
2 skipped** · `tsc -b` clean · eslint 0 errors · `vite build` ok · **1,404 web tests pass** · a
28-check live audit of the shipped classes against the real Exa, Anthropic, OpenAI, Google and TMDB
APIs, passing end to end including the cache round-trip · **run 18: a real 46-user run, `ok`**.

**`pytest -m e2e` was NOT run** — it needs Playwright browsers, which are in neither the runtime
image nor this machine. CI's e2e job covers it. Two settings selects were touched; nothing in
`tests/e2e/` asserts on the web-search card, and the one test naming a model
(`test_curator_model_e2e.py`) already stubs the undated `claude-haiku-4-5` that is now the default.

**Roster scale is now verified** — see §5f: run 18 was a real (non-dry) run over all 46 users,
`status: ok`, no errors, no promotion blockers, and it is where the cost and already-watched figures
come from. Still unverified: whether the measured *settings* hold for a library unlike the test seeds
(all prestige TV — see §8).

## 8. Open items

- Google grounding costs, if anyone turns it on: Gemini 3.x gets **5,000 free grounded searches per
  month**, then $14/1k (Gemini 2.5: 1,500/day free, then $35/1k). Academic while Gemini declines to
  search for this task.
- The OpenAI and Google keys used for the 2026-09-02 probes were pasted into a chat transcript —
  **rotate them**.
- **Model rot — handled by aliases plus a drift test, not by live resolution.** Every default is now
  an undated alias (`claude-haiku-4-5`, `gpt-5-mini`, `gemini-flash-latest`), probed live on
  2026-09-03: the undated Anthropic alias answers 200, and Anthropic's own `/v1/models` listing
  carries undated ids for everything newer — `claude-haiku-4-5-20251001` was the oldest id still
  listed, so it was next to be retired. `tests/unit/test_curator_defaults.py` reads
  `web/src/lib/providers.ts` and fails if the wizard and the engine name different models, or if any
  default carries an 8-digit date.

  This also fixed a live split nobody had noticed: the wizard WRITES its `defaultModel` into
  `curator.model`, so `gpt-5-mini` was what installs ran while the engine's blank-field fallback was
  `gpt-4o-mini`. Two defaults for one decision, silently. **`gpt-5-mini` is unmeasured** — the
  measurements in §5b were on `gpt-4o-mini`, and the keys were rotated before it could be retested.

  Still not built, and now deliberately declined: **resolving a blank model from the provider's live
  model list**. It requires a heuristic that guesses model naming conventions ("prefer the newest
  `gpt-N-mini`"), which is the same rot one level up. Aliases cannot guess wrong.
- Not measured: whether these settings hold on a **library unlike the test seeds**. Everything was
  measured on Severance, The Bear, Andor, Poor Things and Shogun — prestige TV. The large effects
  (`auto` returning nothing, `max_uses=3` halving year coverage, the `why` 524) are structural
  enough that seed choice should not flip them, but that has not been shown.
- Single run per config in most matrices, so small deltas are noise — OpenAI's `search_context_size`
  going 10 → 11 → 12 of 12 with identical input tokens most likely is.
