# Row editor cleanup — design (2026-09-27)

Status: **approved by the owner, 2026-09-27 ("build it"). Being implemented.**
Brief: [row-editor-cleanup-brief.md](row-editor-cleanup-brief.md). Mockup (private canvas, interactive):
https://claude.ai/artifact/69KJGefNd3hmhJ2Ekf1JAT

## 1. Why

Editing row 2 ("🎯 Because you watched {top_seed}") on production, the owner found the editor unclear:
Seasons showed on a row that isn't seasonal, "Make this a 'watch it again' row" showed on every row,
request settings used checkboxes where everything else uses toggles, the rotation setting could not be
found (it only renders when "Watches every source builds from" is 1 or 2), and the "What this row will
do" panel left settings out. The ask: rows have a kind, the editor shows only that kind's settings,
every setting explains itself to someone who has never used Shortlist, and the summary is complete.

## 2. The rule this design keeps

**Nothing that works today is removed, and no existing row changes behaviour by being opened.** Every
field keeps its value; the editor changes only what it shows and how it's worded. Every combination the
engine supports today stays reachable in the editor (seasonal + shared, seasonal + rewatch, seasonal +
a `{top_seed}` name, a `{top_seed}` row blending several watches, rewatch fill-up settings). Existing
controls keep their current UX unless a change below says why.

No backend, engine or schema change. No migration. The kind is worked out from fields the row already
has.

## 3. Row kinds

A new "What kind of row is this?" group sits directly under "How it looks on Plex". It is a radio list
of five kinds, each with a one-line description of what viewers see:

- **Picked for You** — Titles they haven't seen yet, matched to everything they like.
- **Because you watched** — More like one thing they watched recently. Named after it, like "Because
  you watched Dune".
- **Watch it again** — Favourites they've already finished, ready to rewatch.
- **Seasonal** — Only appears around the holidays you pick, like Halloween or Christmas. Filled in any
  of the ways above.
- **Popular on this server** — What lots of people here are watching. Everyone sees the same row.

Group description: "Each kind fills the row in a different way. Pick one, and the settings below change
to match it. You can switch later: you'll see exactly what will change before anything is saved."

**Seasonal has a fill.** Its block asks "Which seasons" (the existing season checkboxes and the two
day fields, unchanged) and then "How it's filled": Picked for You / Because you watched / Watch it
again / Popular on this server. The chosen fill's settings then show exactly as they would for that
kind. This is what keeps every seasonal combination reachable.

### 3.1 Working out the kind (no stored column)

Evaluated on the editor's current draft, in this order:

1. `seasons` non-empty → **Seasonal**; its fill is steps 2–5 applied to the rest of the row.
2. `build == "shared"` → **Popular on this server**.
3. `rewatch` → **Watch it again**.
4. The row names a seed (`{top_seed}` in its effective name), or its effective `max_seeds` is 1 or 2
   → **Because you watched**.
5. Otherwise → **Picked for You**.

"Effective" means the inherited global when the row inherits. For the default row (`slug == "picked"`)
the effective name is the global `row.name_template`, not the row's own empty column — today's
`namesASeed` (`row-editor.tsx:343`) reads only the row's own fields, which is wrong for the default row;
the new helper fixes that.

Step 4 checks the name before the watch count on purpose: a `{top_seed}` row blending 3 watches (row 2
today) is a "Because you watched" row to its owner, and the name is what viewers see.

### 3.2 The default row

Same picker. Seasonal is shown disabled with "The default row can't be seasonal" — the server already
refuses seasons on it (`collections.py:1420-1427`), so this only makes an existing rule visible. Every
other kind stays available, as today (nothing stops the default row being shared or rewatch now) —
except when its effective name (the global `row.name_template`) contains `{top_seed}`: then Picked for
You and Popular on this server are disabled with "This row's name (set in Settings) follows one watch.
Change it in Settings first." and a link to Settings › Row defaults (`/settings#defaults`), since the
editor can't rename it — Picked for You would read straight back as Because you watched, and a shared
row has no watch to fill it with. Watch it again stays available: the engine names it after a watch too
(§15.12). Its name and size keep
coming from Settings.

## 4. Switching kind

Picking a different kind (or a different Seasonal fill) applies a patch to the draft:

- **→ Picked for You:** `build=per_person`, `rewatch=false`, `seed_window=1`, `max_seeds=null`
  (inherit). If the global `max_seeds` is 1 or 2, set `max_seeds=3` instead, or step 4 would read the
  row straight back as "Because you watched".
- **→ Because you watched:** `build=per_person`, `rewatch=false`,
  `max_seeds=namedRowSeeds(media)` (1 for a single-media row, 2 for both), `seed_window` unchanged.
  Already a Because you watched row (e.g. switching only the Seasonal fill): nothing changes.
- **→ Watch it again:** `build=per_person`, `rewatch=true`, `unstarted_only=false` (the API rejects
  the pair), `seed_window=1`. `watched_pct` is **not** raised (it was, until review 2026-09-27): the
  slider is hidden on this kind and the engine ignores the cap on a rewatch row, so the raise did
  nothing but lose the value the row goes back to.
- **→ Popular on this server:** `build=shared`, then today's shared patch (`request_tag` cleared,
  `row-editor.tsx:697`).
- **→ Seasonal:** `seasons` = every season in the catalogue (what "Follow the calendar" writes today,
  `row-seasons-field.tsx:78-88`), or the baseline's seasons if it was loaded seasonal; lead/after days
  unchanged; the fill is the kind on screen — the owner makes what they're looking at seasonal (§15.3).
- **Leaving Seasonal:** `seasons=[]`.

The fields these patches can write are `KIND_FIELDS` (`row-kinds.ts`: build, rewatch, max_seeds,
seed_window, unstarted_only, request_tag, seasons; a test holds the list to exactly what `applyRowKind`
writes).

**Switches are relative to a baseline, never to the previous switch** (review 2026-09-27). The editor
keeps `kindBaseline`: the `KIND_FIELDS` and name as loaded (saved row) or as prefilled (new row /
template). Every switch is `applyRowKind(kindSwitchBase(draft, kindBaseline, target), target)`, which
is `{...draft, ...kindBaseline}` except that entering Seasonal keeps the row on screen and staying
seasonal keeps the seasons on screen (§15.3). So switching back to the loaded kind restores the loaded
values exactly. A hand edit writes through to the baseline only when the baseline's own kind shows that
setting (§15.2); a switch never does. That keeps a new row's typed name, and an edit made in the
loaded kind, across switches, while an edit made only in a switched-to kind goes back with it.

Fields the new kind hides but the engine ignores are left untouched, so switching back restores them.
Fields the new kind hides **and the engine still reads** are reset by the patches above — today that is
only `seed_window` (the engine rotates the lead of any seed list, `history.py:492-506`).

**If a loaded row already holds such a value** (e.g. a Picked for You row with `seed_window > 1`), the
editor shows that setting anyway, with a note and a Reset button, until it is back at its default. A
hidden setting can never quietly change what a row does.

### 4.1 Confirm dialog

On a **saved** row, picking a kind opens a dialog before anything changes: "Change this row to X?",
then "This will:" and one plain sentence per consequence, generated from the patch (fields changed,
settings that appear, settings that disappear, forced behaviour). "Picks new titles every night, so it
keeps up with their latest watch." is added whenever the row the switch ends up with follows a watch
(`followsAWatch` on the result, under the name it ends up with — so turning the optional `{top_seed}`
rename on adds it, and the dialog recomputes its lines as that choice changes). Buttons: Cancel /
Change it; closing it by either returns focus to the kind radio it opened from. On a **new, unsaved**
row the switch applies with no dialog.

The footer says what saving does on Plex, compared with the row **as saved** (not with an unsaved
earlier switch). Traced 2026-09-27 (`row_changes.py:110-200`, `jobs.py` `rows.visibility`,
`pipeline.promote_user_rows` / `_promote_one`):

- The build changes (to or from Popular on this server): the §11 sentence, and only that one.
  `plan_row_changes` returns straight after planning the build flip, so no visibility pass runs at
  that save even when the same switch also changes the seasons.
- Entering Seasonal (confirmed): "Saving applies the season dates on Plex straight away. If today is
  outside them, the row is hidden now." The seasons change queues `rows.visibility` with the row's
  slug, drained before the PATCH returns; it merges every account's excludes, then promotes each
  collection it can identify (delivery ledger, or a static title), and `_promote_one` demotes a dormant
  spec's collection off every surface.
- Leaving Seasonal: "If it's hidden between seasons right now, saving puts it back on Plex straight
  away, still holding the titles from its last season." The same pass runs; the spec is no longer
  dormant, so the collections kept (unbuilt) between seasons are promoted per the row's placement.
  Nothing is rebuilt at save.
- Every other switch: "Nothing changes on Plex until you save and the row next runs."

A switch to per-person can be refused at save (HTTP 422) when another per-person row already uses the
same name in a shared library (`collections.py:1453-1483`); the editor shows that error as it does
today.

### 4.2 Names

A row whose name contains `{top_seed}` switched to a fill other than Because you watched would be read
straight back as Because you watched (§3.1 step 4). So the dialog asks for a new name, prefilled with
that kind's template name (e.g. "✨ {library_name} Picks"). A row whose name uses `{season}` or
`{season_emoji}` that stops being seasonal needs one too — the API refuses such a name on a row with no
seasons (`collections._reject_season_name_without_seasons`) — prefilled the same way, whether or not
the fill changes. A name with both drops whichever the target can't fill (to Because you watched it
keeps `{top_seed}`). The new name is refused while it still carries a placeholder that made the old one
impossible. Switching **to** Because you watched with no `{top_seed}` in the name offers an optional
rename to "🎯 Because you watched {top_seed}". On a new row the proposed name goes straight into the
Name box, and switching back gives back what was typed.

**Saving a switch that renames the row** (saved, non-default rows; decision 2026-09-27). Each confirmed
switch sets the pending rename to exactly its own rename, or clears it, so a later switch back never
carries an earlier one's name. A name merely typed in the Name box is still never sent by Save — only a
rename confirmed in the kind dialog is.

- **The build stays, the row is on, and neither name has `{top_seed}`.** Save sends the settings and
  the new name (`name`, `name_template`) in ONE PATCH, with `defer_rename: true`, then opens the rename
  screen with state
  `{ proposedName, oldTemplate, alreadySaved: true }` — `oldTemplate` being the saved row's
  `name_template || name` before this save. One PATCH because the switch can be refused under the old
  name: the API rejects `{season}` on a row with no seasons (`_reject_season_name_without_seasons`),
  so saving the settings first and renaming after could never succeed for a row leaving Seasonal. With
  `alreadySaved`, the rename screen skips its own PATCH and streams the rename straight from
  `oldTemplate` (the title the collections on Plex still carry), auto-starting once as before
  (`row-rename.tsx` starts on arrival: there is no "abandon" step). The Rename… button and a direct URL
  keep the old path (PATCH with `defer_rename`, then stream).
- **The build flips** (per-person ↔ shared). Save sends the new name WITHOUT `defer_rename` and does
  not open the rename screen: `plan_row_changes` deletes the old build's collections at save, so
  there is nothing on Plex to rename; the row is rebuilt under the new name. The dialog says "The new
  name is used when the row is rebuilt.", and the note under the Name box says the same.
- **Either name has `{top_seed}`, or the row is switched off:** the same PATCH with `defer_rename: true`,
  and no rename screen (it can't render a `{top_seed}` title; an off row has nothing on Plex). "The new
  name appears on Plex the next time the row runs." / "…when you turn it back on and it runs." (§15.1)
- **A failed save** (e.g. 422) navigates nowhere and keeps the rename pending — in the Name box, which
  edits it (§15.4) — so it can be fixed and saved again.

For the default row the name lives in Settings, so the dialog links there instead of offering a rename.

## 5. What each kind shows

Unchanged groups on every kind: How it looks on Plex, Who gets it, the Libraries / Row size / Pick
order part of What goes in it, When it updates → Cadence, Where and when people see it, Remove this row.

**Picked for You** — kind block "How picks are chosen": How many recent watches to match
(`max_seeds`; its "Use the global default" switch is disabled, with the reason, when the global is 1 or
2, since following it would make the row Because you watched), When someone hasn't watched enough
(`cold_start`). What goes in it adds Sources (+ Recent
watches for AI web search when that source is on), Already-watched titles, Only series they haven't
started (not on a movies-only row), Recent releases. When it updates adds How often it changes, Hold
when they aren't watching. Requests.

**Because you watched** — kind block "Which watch it's based on":
- **Based on** (radio, replaces the `max_seeds` number on this kind). Rows with films and shows: "Their
  latest film and their latest show" (2) / "Only the very last thing they watched" (1) / "A blend of
  their last [N] watches" (N ≥ 3, number field). Single-media rows: "Their latest film" (or show) (1) /
  "A blend of their last [N] films" (N ≥ 2). On a row whose name has `{top_seed}`, the blend option
  carries a **Use the global default (N)** switch (writes `max_seeds=null`; the number box shows the
  global and is disabled while it's on). Only there: without `{top_seed}`, inheriting a global of 3 or
  more reads as Picked for You. It restores what a named row could do before (inherit the watch count).
- **Take turns between their last [N] watches** (`seed_window`, today's 1–20 field). Always rendered.
  Enabled when `max_seeds ≤ 2` (today's rule, `row-editor.tsx:1197`); otherwise disabled with "Doesn't
  work with a blend of 3 or more. Choose one of the other Based on options to use it." — and when
  disabled it shows only that reason, not the sentence describing what the number does.
- When someone hasn't watched enough, and directly under it **Name for someone who's new**
  (`fallback_name`, moved here from How it looks on Plex — it is only used in that case).
- A fact line: "Picks new titles every night, so it keeps up with their latest watch." — only while
  it follows a watch (a `{top_seed}` name, or taking turns); otherwise How often it changes applies.
- What goes in it and When it updates as Picked for You, minus How often it changes (forced nightly,
  shown as a fact line) and minus Hold when rotation is above 1 (engine forces it off,
  `rows.py:149-176`).

**Watch it again** — kind block "Which titles come back": Skip titles finished in the last [N] days
(`rewatch_cooldown_days`), When someone hasn't watched enough, and "If they run out of finished titles,
the rest of the row is filled with new picks. How those are found is under What goes in it." What goes
in it shows a "When their finished titles run out" sub-heading with How many recent watches to match,
Sources, Recent releases (all read by the engine for the fill-up, `rows.py:2640,2700`). Hidden:
Already-watched titles (engine overrides it, `rows.py:1990`) and Only series they haven't started (API
rejects the pair, `collections.py:740-743`). When it updates: How often it changes, Hold. Requests
(the fill-up can request, `rows.py:2442-2460`).

**Popular on this server** — kind block "What counts as popular": Only titles watched by at least [N]
people (`min_watchers`), plus today's reach warning. Hidden, as today: Sources, Already-watched, Recent
releases, cold start, How often it changes, Hold, Requests (replaced by "A shared row never asks for
missing titles"). The Recommended "Just me" cell stays disabled as today.

**Seasonal** — its block (seasons + fill) followed by the fill kind's settings above. The existing
"add {season} to the name" and "doesn't run nightly" hints move into the block.

## 6. Wording and layout changes (each with its reason)

- "Watches every source builds from" → **How many recent watches to match**, with "More gives a broader
  mix of everything they like; fewer stays close to what they've watched lately." The old label
  assumed the reader knew what a source and a seed are.
- "Recent watches to choose from" → **Take turns between their last [N] watches**. The owner could not
  find or decode it. Disabled, it shows only why.
- The amber "Set it to 2" warning under Based on (the old `seedAdvice`) → neutral help under a
  selected blend on a `{top_seed}` row (owner-approved mockup): "Picks mix all of these watches, but the
  name only mentions the latest one, and it can't take turns while blending." (the last clause only
  for a blend above 2, which is when rotation is off). The one engine-truthful caveat stays, as neutral
  text with no instruction: "Only the very last thing they watched" on a films-and-shows row — "A watch
  is either a film or a show, never both, so one of your two libraries would get nothing to build
  from."
- Label-plus-sentence duplicates collapse into one sentence with the number inline (Take turns, Skip
  titles finished…, Only titles watched by at least…).
- When someone hasn't watched enough: description "Someone counts as new until they've watched 10
  titles. The 10 applies to every row: change it in Settings." (10 = the live `min_history`; it is
  per-person in the engine, `rows.py:3202`, so it cannot be per-row.)
- "Where people see it" → **Where and when people see it** — it holds Show this row (days).
- Rated by keeps its place under Highest rated, with "Shared by every row and by requests: changing it
  here changes it everywhere." It writes the global setting today (`row-editor.tsx:872-879`) and
  said nothing.
- Pick order: unchanged (the per-option sentence under it already exists). No tooltips.
- Removed as controls, replaced by the kind picker: Per person / Shared, "Make this a 'watch it again'
  row", the Seasons group's "Follow the calendar" switch. Their fields are all still written — by the
  kind patches.

Every other control keeps today's UX: Use the global default switch with "Currently X. Change the
global default", Cadence with Run at, the How often / Hold presets, the placement grid and its
explainer, per-library shelf position, Show this row, Sort prefix, Poster modes, Libraries chips,
Everyone / Choose people, Sources, Rename…, the header (On switch, Run now, Runs, Remove or delete),
the effectiveness tiles, Remove from Plex / Delete, the sticky Save bar.

## 7. Requests

- Every "use the default" toggle and both on/off settings become the shared `InheritableField` +
  `Switch` (today: raw checkboxes, `row-request-settings.tsx:100-107, 247-284`). Rule going forward: a
  switch for one on/off setting; checkboxes only for picking items from a list.
- Hidden when they cannot take effect (`requests.py:806-816`, `clients/seerr.py:421`):
  - Radarr root folder and quality profile: when `requests.target` is Overseerr/Seerr, Radarr isn't
    set up, or the row has no movie library.
  - Sonarr root folder, quality profile and "How much of a show": same for Sonarr, or no show library.
  - Request tag and Tag with who it's for: when the target is Overseerr/Seerr.
  - Requests turned off: only the existing "requests are off" note, with a link to Settings.
  "Set up" uses the pattern already in `requests-settings.tsx:560-565` (URL present, key saved).
- How many people must want it: counted per row (`rows.py:2426-2429`). When exactly one person gets
  the row and the value is above 1, a warning: "Only one person gets this row, so any value above 1
  means it never asks for anything."
- Order, most-used first: How many this row may ask for, Send automatically, Minimum rating, How many
  people must want it, Release years, Language, Tags, Radarr, Sonarr.
- The group stays collapsed by default; its closed summary line states what the row will ask for.
- Radarr/Sonarr folder and profile stay typed values, as today.
- Four request fields have no control today (`req_min_votes`, `req_auto_min_demand`,
  `req_auto_min_rating`, `req_min_rating_other`) and still don't; they follow Settings.

## 8. "What this row will do"

Rebuilt so that **every setting the editor shows for the row's kind has a line**, and nothing hidden
does, driven by the same kind→settings map the editor uses. New lines on top of today's: What it is
(kind, and fill for Seasonal), Based on / Takes turns, Someone new (with the fallback name), Skips and
When they run out (Watch it again), Counts (Popular), Recent releases, Sources in full, Rated by with
Highest rated, Rebuilds (cadence), Changes (with the reason when forced), Not watching (hold), Shelf
position per library, Days, Requests in one line, and Status when the row is off. (No Poster line:
the Plex card beside the name shows the poster, so it is in `NO_FACT_LINE` with the name and
description.) Inherited
values say "(global default)". Existing lines keep their wording where it is accurate.

Two lines corrected in review (2026-09-27): Status says switching a row off takes it off Plex straight
away and nothing is built until it's back on (saving `enabled: false` plans RECONCILE
`collection.disable`, `row_changes.py`; the header toggle's confirm says the same). Someone new says the
person gets no row when the fallback name itself uses `{top_seed}`, `{season}` or `{season_emoji}` —
`delivery.render_row_name` drops such a fallback.

Verify during implementation: the "How many" line reads `input.size`, but the default row's size comes
from the global `row.size` — check whether the default row shows the wrong number today, and fix it
if so.

## 9. New-row gallery

Templates are grouped under the five kinds, each group with the kind's description. Picked for You
holds Picked for You, Fresh finds, From the vault, Movie night and More TV to watch, each with a short
note of how it differs (e.g. "movies only, 10 titles, weekly"). The "Happy to see again" template is
renamed **Watch it again** (title only; its preset name and id `seen-it-already` stay; nothing keys off
the title). Templates remain starting points: every field stays editable, including the kind.

## 10. Structure

- `web/src/lib/row-kinds.ts` (new, pure): `rowKindOf(input, globals)`, `applyRowKind(input, kind,
  fill, globals)`, the kind→visible-settings map, and `describeKindChange(before, after)` for the
  dialog. Everything that decides visibility, the dialog text or the summary reads this one module.
- `row-kind-picker.tsx`, `row-kind-change-dialog.tsx`, and one component per kind block, extracted
  from `row-editor.tsx` (1,477 lines) rather than grown inside it.
- `row-request-settings.tsx` gains `target`, Radarr/Sonarr readiness, the row's media, and audience
  size as props.
- `row-preview.tsx` reads the kind map.

## 11. Per-person ↔ shared on Plex

Traced 2026-09-27 (read-only). A `build` switch is allowed on any row, including the default row
(`collections.py:1207, 573`), and the editor already offers it today (Per person / Shared). This
change only moves that control into the kind picker; it adds no new Plex behaviour.

- **Per person → shared.** At save, every person's copy is deleted (RECONCILE on the old build, then a
  share-filter pass, `row_changes.py:129-136`, `collection_reconcile.py:551-594`; the save waits for
  it, `collections.py:1854`). The next unscoped run builds one collection labelled
  `shortlist__shared_<slug>` (`engine/models.py:360`), delivered unpromoted and promoted after the
  merge (`pipeline.py:1343`). If too few people have watched titles in common, nothing is built
  (`rows.py:3512`).
- **Shared → per person.** At save, the shared collection is deleted by its label
  (`collection_reconcile.py:538-550`) and its excludes are dropped after a complete read
  (`privacy.py:893-914`). The next run gives each person a private copy, with the first-row early
  exclude (`pipeline.py:495-546`).
- Covered by `test_api_row_changes.py::TestBuildFlip` and three integration tests in
  `test_api_collections.py`; nothing runs a flip through to the next run.

Dialog sentences:

- Per person → shared: "Saving removes everyone's own copy of this row from Plex right away. One
  shared row, the same for everyone who gets it, is built the next time the row runs, if enough people
  have watched titles in common. Until then, nobody has this row."
- Shared → per person: "Saving removes the shared row from Plex right away. Each person gets their own
  private row the next time the row runs. Until then, nobody has this row."

Not claimed, because the trace found exceptions: that a subset shared row is never visible outside its
audience (a brand-new subset shared row sits in outsiders' Collections tab until the end-of-run merge —
the early first-row exclude covers per-person rows only, `pipeline.py:495-546`), or that every old copy
is always cleaned up (a copy the removal misses stays under its owner's label, private, until removed
by hand, `collection_reconcile.py:352-362`). Both are pre-existing and independent of this change;
they go to the review backlog, not into this work.

## 12. Testing

Test-first, one file at a time (project rule), full suites once at the end.

- `row-kinds` (vitest): derivation for every kind × media (movie / show / both) × default row ×
  inherited vs explicit `max_seeds`, including row 2's exact state (`{top_seed}`, `max_seeds=3`,
  `seed_window=1`) → Because you watched / blend. `applyRowKind` round-trips: for every from → to pair,
  `rowKindOf(applyRowKind(x, k)) == k`. Hidden-and-read fields are reset. The global `max_seeds ≤ 2`
  edge.
- Editor (testing-library, `row-editor.test.tsx`): each kind shows exactly its settings; the dialog
  appears on saved rows and not on new ones and lists the patch; a `{top_seed}` rename routes to the
  rename screen after save; Seasonal is disabled on the default row; a non-default hidden-and-read
  value shows with Reset.
- Requests (`row-request-settings.test.tsx`): the matrix target (arr / overseerr) × Radarr set up ×
  Sonarr set up × media (movie / show / both), requests off, and the one-person warning.
- Summary: for every kind, each visible setting has a line (a structural test over the kind map), and
  the default-row size line.
- Gallery (`row-templates.test.tsx`): grouping, the renamed title.
- End: `pytest`, `pnpm test`, `tsc -b --force`, `eslint .`, `pytest -m e2e` (the row editor is a UI
  flow; `tests/e2e/test_collections_e2e.py` drives it), and a browser check at 320 / 1024 / 1280 against
  a fake-Plex-seeded dev server.

## 13. Docs

`docs/guides/rows.md`: a Row kinds section (the five kinds, Seasonal fills, switching), Because you
watched's Based on and Take turns, and the per-row Requests rules; rebuild `docs/llms-full.txt`.
Screenshots in docs that show the editor are retaken if the e2e screenshot suite produces them.

## 14. Out of scope

Engine, API and schema. Per-row `min_history`. Dropdowns for Radarr/Sonarr profiles. Controls for the
four request fields that have none. Row ordering on the Rows page.

## 15. Second review (2026-09-27)

Recorded here because each changes something the sections above describe.

1. **`{top_seed}` renames skip the rename screen.** It renders a `{top_seed}` title as "" without
   picks (`collection_reconcile._renamed_titles`), so it would report "renamed 0" and Plex would keep
   the old title. Where a kind switch's new name reaches Plex is `renameAt`: the row is off →
   "…when you turn it back on and it runs."; the build flips → "…used when the row is rebuilt.";
   either name has `{top_seed}` → saved with `defer_rename: true`, no navigation, "The new name appears
   on Plex the next time the row runs."; otherwise the rename screen (`alreadySaved`, §4.2). Dialog and
   Name-box note share the wording.
2. **The baseline takes a hand edit only through a setting the baseline's own kind shows** (same
   setting key: Based on is not Picked for You's watch count). Picked for You → Because you watched →
   Take turns 5 → back: `seed_window` returns to the loaded 1. The dialog's lines now describe what the
   switch does to the row ON SCREEN (draft → result), so that case lists "Stops taking turns between
   their last 5 watches." and never "Change nothing else" while something changes; restorations have
   their own lines ("Turns Only series they haven't started back on.", "Sets the request tag back
   to …").
3. **Entering Seasonal keeps the kind on screen** (supersedes §4's "fill is the baseline's"): the row
   on screen plus the baseline's seasons if it was seasonal, else every season. Staying seasonal keeps
   the seasons on screen. Everything else starts from the baseline (`kindSwitchBase`).
4. **The Name box shows a pending kind-switch rename** and edits it, with the dialog's checks
   (`renameProblem`: empty, or a placeholder that made the old name impossible — Save and Rename… are
   disabled meanwhile), so a refused save is fixed in place. With no switch pending, a typed name is
   still never sent by Save.
5. **A switched-off row's Plex note:** "This row is switched off, so it isn't on Plex. These settings
   apply when you turn it back on." — build flip included. It says where the row is, not that nothing
   happens at save: an off row has no collections and the visibility pass is planned only when
   `enabled_after`, but a build flip still queues a share-filter pass (`row_changes.py`
   `plan_row_changes`). With `paused_all` on (served in
   `GET /settings`), every note that waits on a run or a Plex pass ends "(Runs are paused in Settings,
   so this waits until they're resumed.)".
6. **A rename stream that fails after `alreadySaved`** says "Your settings and the new name were saved,
   but the rename didn't finish on Plex. It's applied the next time the row runs." above the error.
7. **Watch it again shows Take turns under "When their finished titles run out"** while the fill-up's
   effective `max_seeds` is 1 or 2 — the engine rotates that seed list too, and the editor showed it
   there before this redesign. Above 2 a leftover rotation is shown with Reset (`hiddenButRead`).
8. **Every switch's result obeys the API's pairing rules** (`collections._validate_pairing`):
   `normalizeKindResult` turns `unstarted_only` off when the result has `rewatch` or `media == "movie"`,
   after `kindSwitchBase` + `applyRowKind`, in the editor and in `describeKindChange` alike, so the
   dialog never says "Turns Only series they haven't started back on." for a value that can't be kept.
   The case: a Picked for You row over shows with the flag on → Watch it again or Popular → libraries
   narrowed to movies (the kind on screen doesn't show the flag, so the baseline keeps it) → back.
9. **A hand edit is taken into the baseline whole or not at all.** "Only series they haven't started"
   no longer writes `rewatch: false` — it is shown only where `rewatch` is already false, and `rewatch`
   is controlled by the always-visible kind setting, so `baselineTakes` let that dead write turn a
   loaded Watch it again baseline into Because you watched (its rotation was then reset on the way
   back). Side effects the row needs stay, but go with the setting that made them: a blend chosen under
   Based on still stops taking turns, and that reset belongs to Because you watched, not to a Watch it
   again baseline the owner switched from. The library picker's `unstarted_only: false` on a
   movies-only row stays (the API needs it; `unstarted_only` doesn't decide the kind).
10. **A pending rename never changes the kind on screen.** The kind (and every setting read from it)
    comes from the draft under the name the switch was confirmed with; the Name box edits the rename,
    which `renameProblem` checks and Save refuses while it's wrong. So typing `{top_seed}` back into a
    rename away from Because you watched leaves Picked for You selected, and picking Because you
    watched from there switches back and drops the rename. The Plex card still shows the name as typed.
11. **The header's on/off switch is part of the saved row.** It saves at once; when that save succeeds
    the editor updates the form's `enabled` and its saved snapshot, so the row isn't flagged unsaved and
    Save sends the current value instead of undoing the switch. The dialog's Plex note, where a rename
    lands (`renameAt`), and the preview's Status all read that snapshot.
12. **A new name can't change whether the row follows a watch.** `{top_seed}` is in `mustNotUse` only
    on the way to Picked for You (it would read back as Because you watched) and Popular on this server
    (a shared row's picks carry no seed, `rows._shared_row`, so `delivery.render_row_name` renders no
    name and the row isn't built; the API accepts it regardless). The dialog and the Name box refuse
    it there: "A name with {top_seed} names the watch a row follows, and this kind doesn't follow
    one." Watch it again keeps it — the engine names that row after a watch too (`rows._names_a_seed`
    ignores rewatch) and rebuilds it nightly — so a `{top_seed}` row switched to it keeps its name, and
    one leaving Seasonal is offered the template name (season placeholders dropped) but may type one
    that keeps `{top_seed}`; the dialog's lines follow the name chosen (nightly or not). The dialog's
    reason for a required rename reads `because` (what made the old name impossible), not
    `mustNotUse`. On the way to Because you watched or Watch it again, the Name box keeps the confirmed
    name's `{top_seed}` status either way (`renameProblem`'s `confirmed`): the editor reads the row's
    settings from the confirmed name, so `followsAWatch` never changes silently. So whatever Save sends
    reads back (`rowKindOf`) as the kind on screen. The picker applies the same rule to the default row,
    whose name lives in Settings: with `{top_seed}` there, `kindDisabledReason` disables only Picked for
    You and Popular on this server (plus Seasonal, never allowed on the default row) and links to
    Settings; Watch it again and Because you watched stay available.
13. **The header's switch and page Save never overlap.** Each PATCHes the whole row, so while the
    switch's save is in flight page Save and Rename… are disabled, and while a page Save is in flight
    the switch and its Try again are. Every switch PATCH, a retry included, is built from the row as it
    is when sent with only `enabled` changed — never the failed request replayed.
