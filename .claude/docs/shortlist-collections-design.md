# Shortlist Rows & Prompt Tuning — Design

**Status: proposal, for review. No code until approved.**

Shortlist today builds exactly one kind of row — a private "Picked for You" per user. This design turns
that into one flexible concept — a **Row** — so an owner can define any number of curated rows, choose
how each is built, who gets it, and how the AI writes it. The earlier "tune the LLM prompt" idea folds
in as the Row's _recipe_, so nothing is wasted.

Written against today's code (file:line refs throughout) so it can be built in phases without
surprises.

> **Terminology:** user-facing we call it a **Row** (it's what a person sees on their Plex Home). In
> code and the DB it maps to a Plex **collection** (table `collections`). "Row" and "collection" are
> the same thing at two layers.

---

## 1. The model — one Row, four choices

A **Row** is a named curated row. For each Row the owner picks four independent things:

1. **How it's built**
   - **Per‑person** — each chosen person gets _their own_ version, from _their own_ watch history
     (today's model: "Picked for You", plus "Because You Watched", "Hidden Gems", …).
   - **Shared** — _one_ version built from _everyone's_ history in aggregate, identical for whoever can
     see it ("Popular on this server").
2. **Who gets it (audience / access control)** — everyone, or a chosen subset of people.
3. **The recipe** — name, size, which libraries (movies/shows/both), and the AI instructions
   (tone + guidance, or a full custom prompt). Set once when the Row is created.
4. **Per‑person tweaks (optional)** — for a per‑person Row, override the recipe for one person (e.g.
   Sarah gets a "cinephile" tone, Mike gets "concise").

The old single row is just the special case: _per‑person · everyone · default recipe._ Upgrade is
byte‑for‑byte identical (§7).

### 1.1 The examples, mapped

| Owner wants                           | build      | audience            | recipe                               |
| ------------------------------------- | ---------- | ------------------- | ------------------------------------ |
| "Picked for You" (today)              | per‑person | everyone            | default                              |
| "Because You Watched"                 | per‑person | everyone (or a few) | reason style = "because you watched" |
| "Popular on this server"              | shared     | everyone            | aggregate framing                    |
| A custom row for the kids             | per‑person | {Jamie, Alex}       | family‑friendly guidance             |
| "Staff picks" seen by housemates only | shared     | {Sarah, Mike}       | curated guidance                     |

### 1.2 The four combinations, by difficulty

The two axes (build × audience) give four cases. Three are easy; one is the privacy‑sensitive one:

|                | audience = everyone                              | audience = a subset                                                                                          |
| -------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| **per‑person** | easy — build for all (today)                     | **easy** — build only for chosen people; nobody else touched                                                 |
| **shared**     | **easy** — one row, shown to all, nothing hidden | **hard** — must actively _hide_ the shared row from non‑audience users (uses the private‑row hide machinery) |

Only the bottom‑right cell touches the privacy write‑path in a new way. It ships last and is tested
hardest (§6, §11).

## 2. Locked decisions (from review)

- **Popular row source:** everyone's history feeds it, protected by the "≥2 watchers" rule (§6.1).
- **Shared captions:** generic ("Popular on this server"), never "Because you watched X" (§6.3).
- **Rows per person:** soft cap ~5 (bounds LLM cost/run time — one call per person per per‑person row).
- **Name on screen:** "Rows".

## 3. The recipe = prompt tuning (folded in)

### 3.1 Fixed vs tunable — why free editing is safe

Every LLM response already passes `validate_picks()` (`base.py:87-122`), which **drops any title not in
the candidate list, caps reasons at 90 chars, and preserves the privacy‑critical media_type**. That
validator is the guardrail: a user can edit the prompt freely and still never get an unavailable title,
a leaked history, or a broken privacy label.

- **Fixed (stays in code, appended to every prompt):** the JSON output schema (`picks_schema()`),
  "return only tmdb_ids from the candidate list / never invent titles", the reason‑length cap.
- **Tunable (`PromptConfig`, part of the recipe):**
  - `tone` ∈ {balanced, warm, concise, cinephile, playful} → a tone sentence.
  - `guidance` — free text injected into the system prompt.
  - `template` — optional full custom system prompt with documented `$`‑variables
    (`$k`, `$max_reason_len`, `$guidance`, `$tone`, `$username`), rendered with
    `string.Template.safe_substitute`. Empty = built‑in skeleton. Unknown `$vars` are left as‑is and
    the `$` grammar has no attribute/subscript access, so a template can never crash a run or read
    Python internals.

```
PromptConfig { tone: str = "balanced"; guidance: str = ""; template: str = "" }
```

### 3.2 Precedence (per‑person tweak > Row recipe > built‑in)

Effective config for a `(row, person)` pair, resolved in the server adapter and set on `UserProfile`:

- **tone:** person override → Row recipe → "balanced".
- **guidance:** _additive_ — Row guidance + person guidance (both, newline‑joined). House rule + a
  per‑person note is the common case.
- **template:** person override → Row template → built‑in skeleton.
- Empty string = "inherit" everywhere (works with the existing non‑None prefs merge, `users.py:83-86`).

### 3.3 Engine plumbing (no provider churn)

`build_prompts(profile, candidates, k)` (`base.py:64-84`) is the single shared builder; all four
providers call it (`anthropic.py:39`, `openai.py:36`, `google.py:30`, `ollama.py:30`). We add a
resolved `prompt: PromptConfig` field to `UserProfile` (`models.py:96-116`); `build_prompts` reads it
plus a scope flag (per‑person vs shared, which selects the reason framing). **No change to `curate()`
or any provider.**

## 4. Delivery & labels

|              | per‑person Row                                                                        | shared Row                                                 |
| ------------ | ------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Plex label   | `shortlist_<userslug>` (today; **one per user**, shared by all their per‑person rows) | `shortlist_shared_<rowslug>` (**one per shared Row**)      |
| how many     | 1 per audience‑member per library                                                     | 1 per library, server‑wide                                 |
| title marker | `row_marker(account_id)` (`delivery.py:138`) — unique per person                      | fixed `row_marker(0)` — one stable membership              |
| promoted     | `promote(shared=True)` after filters merged                                           | `promote(shared=True)`                                     |
| hidden from  | everyone except that person                                                           | everyone **not** in the audience (none, if audience = all) |

A person's multiple per‑person rows all carry the **same** `shortlist_<userslug>` label (the label is the
privacy primitive — all of a person's private rows must be hidden from others as one set); they're told
apart by **title** + the per‑account zero‑width marker (`delivery.py:16-34`). Each _shared_ Row gets its
**own** label because shared Rows can have different audiences.

## 5. What "collection type" is NOT

A per‑person run is a single LLM call over mixed movie+show candidates (`pipeline.py:370-373`);
"movies vs shows" is the Row's `media` field (which libraries it writes to), not a second prompt. A
`media: both` Row makes one curate call and splits picks by media type at delivery
(`delivery.py:88-92`).

## 6. Privacy — the generalized rule (the crux)

Everything below is one idea that already exists, generalized:

> **A Row's label is hidden from every person who is NOT in that Row's audience.**

Today this is hard‑coded to the special case "each per‑user label is hidden from everyone but its
owner." The single chokepoint is `desired_excludes(own_label, stored_labels)` (`privacy.py:123-142`) —
a pure set‑difference over every `shortlist_*` label. The generalization replaces "except your own" with
"except the ones whose audience includes you":

```python
# today
def desired_excludes(own_label, stored_labels):
    return {lbl for lbl in stored_labels.values() if lbl != own_label}

# generalized: audience-aware. `visible_to(user)` = labels this user is allowed to see
def desired_excludes(user, all_labels, visible_to_user):
    return {lbl for lbl in all_labels if lbl not in visible_to_user}
```

The audience of each label:

- `shortlist_<userslug>` → `{that user}` (per‑person; excluded from everyone else — identical to today).
- `shortlist_shared_<rowslug>` with audience = **all** → visible to everyone → excluded from **nobody**
  (this is the easy "shared to all" case; a strict _reduction_ of exclusions).
- `shortlist_shared_<rowslug>` with audience = **subset** → excluded from everyone not in the subset
  (the hard case; new privacy surface).

Everything else in the privacy path is untouched: per‑slug targeting, the read‑modify‑write merge that
byte‑preserves foreign filter conditions (`merge_label_excludes`, `privacy.py:70-87`), snapshots
(`privacy.py:190-201`), and the leak‑safe write ordering.

### 6.1 Aggregate source with a watcher threshold (shared rows)

The shared profile is built from all users' histories, but a title only qualifies if **≥ `min_watchers`
distinct users watched it** (default 2). This stops "watched by exactly one housemate" — identifying on
a small server — from ever reaching a public row.

### 6.2 Ownership & the sweep

`shortlist_shared_<slug>` carries the `shortlist_` prefix, so ownership checks already treat it as ours
(`clients/plex.py:320-325`, sweep `delivery.py:293-297`); the sweep won't delete a well‑typed shared row
(marker lookup → `None` → not a colliding shared‑tag). Kometa coexistence unaffected.

### 6.3 Aggregate‑framed reasons

Shared Rows use a distinct built‑in skeleton — no "Because you watched X" (there's no single "you").
Reasons come from server‑wide signal ("Popular on this server", "A lot of you are watching this"); tone
and guidance still apply; `validate_picks` still caps length and drops hallucinations.

### 6.4 Checked against the plex‑safety rules

- **Rule 1 (leak‑safe ordering):** shared delivery is a create+promote → delivered UNPROMOTED, then
  promoted only after the excludes are merged. The remedy pass (`_remedy_only`,
  `run_service.py:278-292`) still only removes/merges excludes, never creates a row.
- **Rule 2 (snapshot):** shared‑to‑all writes no share filters (nothing hidden) → no snapshot needed.
  Shared‑to‑subset _does_ write excludes onto non‑audience users → snapshots first, like any user.
- **Rule 3 (merge, never rebuild):** unchanged — still union/remove single labels in place.
- **Rule 4 (touch only what we own):** shared label is `shortlist_`‑prefixed → already "ours".
- **Rule 5 (owner/managed):** the shared row is promoted to all with library access; owner sees it like
  anyone; managed users get excludes written exactly like shared users (no special branch today,
  `privacy.py:169-171` only special‑cases the owner).
- **Rule 7 (clean up test artifacts):** the shared‑visibility check is a fake_plex e2e assertion — a
  throwaway shared‑labelled row IS visible to a canary in the audience and NOT to one outside it —
  with all test artifacts torn down afterward.

## 7. Data model & migration

New `collections` table (Alembic `0003`; base is `0002`, `db/models.py`):

```
collections(
  id PK, slug str unique+index, name str, build str16 ("per_person"|"shared"),
  audience str16 ("everyone"|"subset"), enabled bool, size int, media str16, "order" int,
  name_template str null,               -- per_person
  source str16 null, min_watchers int null,   -- shared
  prompt JSON,                          -- PromptConfig recipe
  created_at, updated_at
)
collection_audience(collection_id FK, user_id FK)   -- only rows for audience = "subset"
```

- **Per‑person tweaks** live in `users.prefs` JSON (no migration): `collection_prefs: { <slug>:
{ size?, name_template?, prompt?, enabled? } }`. Current flat `row_size`/`row_name_tpl` prefs are read
  under the seeded default row on first read (back‑compat shim).
- **Seed migration** inserts one default `picked` row (`build: per_person`, `audience: everyone`,
  name/size from current `row.name_template`/`row.size` settings) so upgrade behaviour is identical.

## 8. Engine, pipeline & server changes

- **`models.py`:** add `PromptConfig`; add `UserProfile.prompt`; add an engine‑pure `RowSpec`
  dataclass (built by the adapter from DB); reserved shared label helper.
- **`base.py`:** `build_prompts` reads `profile.prompt` + scope; add `TONE_PRESETS`, a shared‑scope
  skeleton, and a safe‑format helper for custom templates.
- **`pipeline.py`:** the per‑user loop (`:137-159`) runs per **(enabled per‑person Row × audience
  member)**; a new shared pass builds each shared Row's aggregate profile once and delivers one public
  row; `desired_excludes` becomes audience‑aware (§6); promotion loop covers shared rows.
- **`delivery.py`:** parametrise `_deliver_one` by Row (label, title template, marker); add
  `deliver_shared()` (fixed marker, audience‑scoped hiding).
- **`run_service.py`:** load Rows from DB; resolve per‑(Row,person) `PromptConfig`; build aggregate
  profiles; run report/SSE gain a `collection_slug` dimension.
- **Reports/SSE:** `UserRunReport`/`RunUser`/`CollectionDiff` gain `collection_slug`; the run page
  groups diffs by Row; `run.user.stage` carries the Row.

## 9. Testing

- **Engine unit:** `build_prompts` matrix — tone × guidance × custom/malformed template × scope
  (per‑person vs shared framing). Assert no PII, strict schema, fixed contract line always present.
- **Privacy property tests (hypothesis):** the generalized exclusion — for arbitrary audiences and
  pre‑existing/foreign filters, every label is excluded from exactly the non‑audience users and never
  from audience users; the round‑trip stays byte‑exact.
- **Aggregate privacy:** a title watched by `< min_watchers` never appears in a shared row; shared
  reasons never contain "Because you watched".
- **Delivery:** shared row created once with fixed marker; not swept; audience members resolve it as
  visible, non‑members as hidden.
- **Shared‑visibility test (fake_plex):** a throwaway shared‑labelled row is visible to an in‑audience
  canary and hidden from an out‑of‑audience one; test artifacts torn down afterward.
- **e2e (fake_plex):** create each Row type; a run delivers them; the fake canary sees the right rows
  and not the wrong ones. **Migration:** `0002`→`0003` yields one `picked` row and identical delivery.

## 10. API & UI

**API**

- `GET/POST/PATCH/DELETE /api/collections` (owner‑only); `PATCH` includes audience; reserved slugs
  (`shared`, `probe`) validated.
- `PATCH /api/users/{id}` prefs gains `collection_prefs`.
- `POST /api/curator/prompt-preview` `{scope, prompt, sample_user_id?}` → the assembled system prompt,
  so the owner sees the effect before saving.

**UI** (non‑tech‑first, consistent with the recent UX pass)

- **Settings → Rows:** list with add/edit/reorder/enable. The editor is a short wizard‑ish form:
  _(1) built how_ (per‑person / shared) → _(2) who gets it_ (everyone / pick people) → _(3) recipe_
  (name, size, libraries, **Curation style** = tone dropdown + guidance textarea + collapsible Advanced
  template with Restore default + live Preview).
- Shared Rows show a plain‑language note ("visible to everyone who can see it; built from what several
  people watched, never one person") and the `min_watchers` control framed as "only titles at least N
  people watched".
- **User detail → Rows:** per‑person override with "Use default" toggles (tone, guidance, size, name,
  on/off); advanced editor collapsed.

## 11. Implementation order (each a shippable PR + deploy)

- **Phase A — Recipe/prompt tuning on today's row.** `PromptConfig`, tone/guidance/template, global +
  per‑person, preview endpoint, UI. No new tables (settings + prefs), no privacy change. _Lowest risk,
  immediate value; establishes the recipe._
- **Phase B — Multiple per‑person Rows + audience.** `collections` table + migration, per‑Row pipeline
  loop, delivery parametrised, reports/SSE gain the Row dimension, Rows UI. Reuses the existing
  per‑person privacy model unchanged (audience just picks _who gets built for_).
- **Phase C — Shared Row to everyone.** Aggregate profile + `min_watchers`, shared delivery, the
  "never exclude an all‑audience shared label" reduction, aggregate reason framing, Probe extension.
- **Phase D — Audience/access control for shared Rows.** The audience‑aware `desired_excludes`, hiding
  a shared Row from non‑audience users, the full property‑test suite. _Only phase that adds privacy
  write surface; ships last, most tests, verified `--dry-run` against SFLIX before any real write._

## 12. Remaining open questions

1. **Shared reasons wording** — exact phrases ("Popular on this server" / "Trending here"); cosmetic.
2. **Row order on Home** — Plex gives limited control; treat `order` as best‑effort?
3. **Per‑person Row audience default** — new per‑person Rows default to "everyone", or "no one until you
   pick"? (Proposed: everyone, matching today.)
