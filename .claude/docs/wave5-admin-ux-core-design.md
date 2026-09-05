# Wave 5 — Admin UX, core four

**Items covered:** `posters` · `dryrun-preview` · `restriction-status` · `bulk3state`
**Origin:** [`.claude/docs/audit-2026-09-programme.md`](audit-2026-09-programme.md) (Wave 5, Admin UX)
**Written:** 2026-09-05. Everything below was read from the tree on `dev` at `7a189ac`.

Shortlist is **admin-only**. Nobody but the Plex server owner ever opens this UI. Every design
decision here is made for one reader who owns the server, already knows what a share filter is, and
is trying to answer "did the thing I asked for actually happen".

---

## Premise corrections up front

The audit was right about two of these four and wrong about two. Both corrections change the work.

| Item                 | Audit's premise                                                                                                                    | Verified state                                                                                                                                                                                                                                                                             |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `posters`            | "the `posters` extra — investigate what exists already"                                                                            | The `posters` extra (`pyproject.toml:50` → Pillow) is the **row/collection artwork generator**, nothing to do with per-title art. Correct that pick lists are pure text (`web/src/components/pick-list.tsx`).                                                                              |
| `dryrun-preview`     | "dry-run is a MODE you switch on"                                                                                                  | **Half true.** There is no global dry-run setting in the DB; the only global is the `SHORTLIST_DRY_RUN` env var (`shortlist/server/safe_mode.py:15-17`). Preview-then-commit is already the house pattern in three places. `PATCH`/`DELETE /collections/{id}` genuinely expose no preview. |
| `restriction-status` | "nothing verifies hiding after the fact anymore… this screen would be the ONLY way an owner can see for themselves that it worked" | **Substantially false, and the correction makes the item much smaller and much better.** Three independent verifications already exist and run today (§3.1). What does not exist is a _screen_.                                                                                            |
| `bulk3state`         | "bulk edit currently clobbers fields the owner did not touch"                                                                      | **False as stated.** There is no multi-field bulk edit anywhere in the SPA. The one bulk endpoint has a single required field. Every partial-write path already honours "absent means don't touch". §4 says what the real defect is.                                                       |

---

## 1. `posters` — poster art in the pick lists

### 1.1 Current state (verified)

**The pick list is one shared component and it is pure text.**
`web/src/components/pick-list.tsx` (69 lines) renders `<ol><li>` with a `#{rank}` span, the title, an
em-dash reason, an optional `· inspired by {seed}`, and a provenance line. No image element. It has a
`collapseAfter` prop with a "Show all (+N)" toggle. Three call sites:

- `web/src/components/user-detail/user-row-card.tsx:113` — `collapseAfter={5}`
- `web/src/components/runs/run-rows-tab.tsx:87` (shared row, `collapseAfter={10}`) and `:113` (per-user)
- `web/src/components/runs/user-panel.tsx:464`

A fourth renderer at `web/src/components/runs/user-panel.tsx:145-160` deliberately does **not** use
`PickList` (its own comment at `:154` says so). It is out of scope for v1.

**There is already a poster component in this repo, hotlinking TMDB, with a lazy loader and a
fallback.** `web/src/pages/requests.tsx:78-105` — the request inbox's `Poster`. It builds
`https://image.tmdb.org/t/p/w154${item.poster_path}`, sets `loading="lazy"`, `alt=""` (decorative;
the title is beside it as real text), and falls back to a same-size `Clapperboard` placeholder tile
via `onError` so "rows never jump". Its docstring already reasons about bucket choice and bandwidth.
**This is the component to extract, not a new one to write.**

**The `posters` extra is a different feature.** `pyproject.toml:50` — `posters = ["pillow>=10.0"]`,
consumed only by `shortlist/server/services/poster_service.py` (imports guarded by `try/except
ImportError` so the feature degrades when the extra is absent). That is the **row/collection**
artwork feature: text-rendered and AI-generated posters uploaded to Plex collections, stored as bytes
in the SQLite `poster_assets` table (`shortlist/server/db/models.py:318-331`), served by
`GET /api/collections/{id}/poster/image` (`shortlist/server/api/collections.py:1420`) and consumed by
`<img>` at `web/src/components/rows/row-card.tsx:101-122`. It answers no question about per-title art.

**TMDB already carries `poster_path`, but only onto SOME picks.**
`Candidate.poster_path` exists (`shortlist/engine/models.py:181`), populated free from every TMDB list
response (`shortlist/engine/candidates.py:588,596`), with `TmdbClient.poster_path()`
(`shortlist/engine/clients/tmdb.py:193`) as a paid fallback for titles a non-TMDB source surfaced. Its
own comment says: _"Only carried through to the request inbox… a delivered pick uses Plex's copy of
the title."_

`Pick` (`shortlist/engine/models.py:215-260`) has **no** `poster_path`, and `PickRow`
(`shortlist/server/db/models.py:489-534`) has no column for one. Critically, `Pick` is built at
**four** sites and only one of them has a `Candidate` to take a poster path from:

| Site                            | Source                                                   | Has `poster_path`? |
| ------------------------------- | -------------------------------------------------------- | ------------------ |
| `shortlist/engine/picker.py:69` | a ranked `Candidate`                                     | yes, free          |
| `shortlist/engine/rows.py:2017` | cold-start, `ctx.plex.top_rated(section, k)`             | **no**             |
| `shortlist/engine/rows.py:2885` | shared "N people watched it" row, from the library index | **no**             |
| `shortlist/engine/rows.py:3032` | cold-start fallback                                      | **no**             |

So a TMDB-backed design leaves cold-start rows and every shared row poster-less — exactly the rows a
visual treatment helps most — unless we buy a TMDB detail call per title.

**Every pick does have a `rating_key`.** `PickRow.rating_key` is a non-null `Integer`
(`shortlist/server/db/models.py:501`), and a pick is in the library by construction — the pipeline
only ever picks titles the delivery library holds. (`picker.py:70` writes `c.rating_key or 0`, so `0`
is possible and means "not matched"; handle it.)

**Serving image bytes over the session cookie already works.** The collections router carries
`dependencies=[Depends(require_owner)]` (`shortlist/server/api/collections.py:58`), auth is an httpOnly
signed session cookie (`shortlist/server/auth.py:1`), and `row-card.tsx` already loads
`/api/collections/{id}/poster/image` in a plain `<img src>`. No new auth question.

**TMDB attribution already exists** — `web/src/components/settings/connections-section.tsx:516-518`
carries the required "This product uses the TMDB API but is not endorsed or certified by TMDB." So
hotlinking is not a new obligation.

**TMDB's image CDN limits (checked 2026-09-05):** `image.tmdb.org` enforces **20 simultaneous
connections per IP** and the CDN applies roughly **50 req/s** as DDoS protection. A browser on HTTP/2
opens one connection, so per-viewer hotlinking is comfortably inside it. A **server-side proxy that
fetched from TMDB would be the risky shape** — it funnels every install's requests through one IP.
Sources: [Is there a request limit on image.tmdb.org
too?](https://www.themoviedb.org/talk/62edd3ca46aed400917de201) ·
[Limits on images](https://www.themoviedb.org/talk/5c0e747d0e0a2638bc0c15c7)

### 1.2 The design — posters come from the PMS, not TMDB

**Decision: serve pick posters from the Plex Media Server, proxied by the backend, keyed on
`PickRow.rating_key`.** The inbox keeps TMDB, because inbox titles are by definition _not_ in the
library. Picks are.

Why this and not TMDB:

1. **Uniform coverage.** Every one of the four `Pick` construction sites has a `rating_key`. Only one
   has a `poster_path`. TMDB gives partial coverage; the PMS gives complete coverage.
2. **No new column, no migration, no backfill gap.** A TMDB design needs `picks.poster_path`, a
   migration, and leaves every historical pick blank forever (the `RequestCandidate.poster_path`
   precedent, migration `0044`, says so in its own comment).
3. **It shows what the owner actually has.** If the owner runs Kometa or TMM and has custom artwork,
   the admin UI matches Plex. TMDB art would silently differ.
4. **It works on an air-gapped server.** The inbox's own comment
   (`requests.tsx:79-82`) already flags TMDB as "a third-party host this app never checks".
5. It avoids putting every install's image traffic on one IP against TMDB's 20-connection ceiling.

Costs, stated plainly: one backend round-trip per image, and a dependency on a PMS response shape we
must record a fixture for (rule 11).

#### New endpoint

```python
# shortlist/server/api/picks.py  (new module, router mounted like every other under /api)
router = APIRouter(prefix="/picks", tags=["picks"], dependencies=[Depends(require_owner)])

_THUMBS: LRU[int, str] = LRU(maxsize=4096)  # rating_key -> "/library/metadata/123/thumb/1699999999"


@router.get("/{rating_key}/poster")
async def pick_poster(rating_key: int, request: Request) -> Response:
    """One delivered pick's artwork, streamed from the PMS.

    The Plex token authenticates this fetch server-side and never reaches the browser (rule 9): the
    SPA asks for `/api/picks/{rating_key}/poster` over its own session cookie and this handler does
    the PMS read. `rating_key` is not a secret — it identifies an item on a server the owner owns,
    and every other Shortlist endpoint the owner can call already exposes far more.

    404 rather than a placeholder image when the item is gone: a pick's ratingKey can go stale when
    a title is removed and re-added, and the SPA already renders a tile of the right size for that
    case. Answering with a picture would make a missing item indistinguishable from a real one.
    """
```

Behaviour:

1. `rating_key <= 0` → `404` immediately, **without touching Plex**. (`picker.py:70` can write `0`.)
2. Resolve the thumb path: in-process LRU, else one raw `GET {plex.url}/library/metadata/{key}` with
   `Accept: application/json` and `X-Plex-Token` **as a header, never a query string**, reading
   `MediaContainer.Metadata[0].thumb`. Same shape and same client style as the existing raw read at
   `shortlist/engine/clients/plex_pms.py:1579` (`user_hubs`), which already parses
   `MediaContainer.Hub` out of `/hubs` JSON.
3. `GET {plex.url}{thumb}` with the same header; stream the bytes back with the PMS's own
   `Content-Type`.
4. Response headers: `Cache-Control: private, max-age=604800` and `ETag: W/"{rating_key}-{stamp}"`
   where `stamp` is the trailing number Plex puts on the thumb path — it changes when the artwork
   changes, so the ETag invalidates itself for free. Honour `If-None-Match` with `304`.
5. Plex unreachable or 5xx → `502`. A failed read must never be answered with a blank image.

Deliberately **no server-side byte cache.** `poster_assets` exists for row artwork the owner
uploaded — a handful of rows. A per-title cache would grow with the library and has no eviction
story. The browser cache plus the LRU thumb-path memo is enough for a page that renders 5-10 posters.

#### New shared component

Extract `requests.tsx`'s `Poster` into `web/src/components/title-poster.tsx`, generalised over its
source:

```tsx
/**
 * A title's artwork at a fixed size, with a same-size placeholder for everything that can go wrong.
 *
 * Two sources, because the two lists show different kinds of title. An INBOX title is not on the
 * server yet, so its only artwork is TMDB's. A delivered PICK is in the library by construction, so
 * its artwork is the one the owner actually has in Plex — including whatever Kometa or TMM put
 * there. Passing a `ratingKey` uses the PMS; passing a `posterPath` uses TMDB's CDN.
 *
 * The placeholder is the same box as the image, never smaller: a list that swaps a 58x87 poster for
 * a 24px icon reflows every time one title lacks art.
 */
export function TitlePoster({
  ratingKey,
  posterPath,
  className,
}: {
  ratingKey?: number | null;
  posterPath?: string | null;
  className?: string;
}) { … }
```

- `loading="lazy"`, `decoding="async"`, explicit `width`/`height` attributes (no CLS).
- `alt=""` — decorative, the title is right beside it as real text. Carried over from the inbox.
- `onError` → the `Clapperboard` placeholder, byte-identical dimensions.
- `ratingKey` absent/`0` and `posterPath` absent → render the placeholder without a request.

`requests.tsx` swaps its local `Poster` for `<TitlePoster posterPath={item.poster_path} />`, so the
two lists cannot drift.

#### Layout at 320 / 1024 / 1280

One size everywhere except the narrowest phones, so the pick list looks the same as the inbox:

```tsx
// 58x87 = 2:3, the inbox's size. 40x60 below `sm` (640px), which is what makes 320px work.
className =
  "h-[60px] w-[40px] shrink-0 rounded border object-cover sm:h-[87px] sm:w-[58px]";
```

- **320px** — card padding (2×16) + poster 40 + gap 12 leaves a **236px** text column. Title wraps to
  two lines, reason to three. The `#N` rank badge moves from its own 20px column to the poster's
  top-left corner as an absolutely-positioned chip below `sm`, buying that 20px back.
- **1024px** — unchanged from today's layout; poster 58 + gap 12 costs 70px off a column that is
  already ~450px inside the user-detail card. Nothing else moves.
- **1280px** — same as 1024. No new breakpoint.

Per the "test at 320/1024/1280" house rule, all three get measured in a browser before this lands,
and the **old build is measured too**, so "pre-existing" and "I broke it" are distinguishable.

The `<li>` becomes `flex items-start gap-3` (was `items-baseline`) — a poster and a baseline do not
align.

#### Lazy loading and request volume

- `collapseAfter` already keeps hidden picks **out of the DOM entirely**, so their posters are never
  requested. A user-detail card requests 5; a run-rows tab requests 10. Expanding requests the rest.
- `loading="lazy"` handles a long expanded list.
- The browser cache makes the second visit free; the `ETag` makes a stale one a `304`.

### 1.3 Tests, written first

Python — `tests/unit/test_pick_poster.py`:

1. `TestPickPoster::test_a_rating_key_of_zero_is_a_404_and_plex_is_never_called` — `mock_plex`
   asserts zero calls. (`picker.py:70` really can write `0`.)
2. `::test_the_plex_token_is_sent_as_a_header_and_appears_in_no_response_or_url` — rule 9. Assert
   the outbound request kwargs, not just that a call happened (the "assert the kwargs" rule).
3. `::test_an_unknown_rating_key_returns_404_rather_than_an_empty_image`
4. `::test_a_plex_failure_returns_502_rather_than_zero_bytes`
5. `::test_the_thumb_path_is_read_from_a_recorded_pms_metadata_response` — against a new
   `tests/fixtures/pms_item_metadata.json` recorded from a real PMS (rule 11). **Record it before
   writing the handler**, not after.
6. `::test_the_endpoint_refuses_a_request_without_an_owner_session`
7. `::test_a_matching_if_none_match_returns_304_and_reads_no_bytes_from_plex`
8. `::test_the_thumb_path_is_resolved_once_per_rating_key_across_two_requests` — the LRU.

Vitest:

9. `web/src/test/title-poster.test.tsx::"falls back to a placeholder of the same size when the image errors"`
10. `::"renders the placeholder with no network request when there is no rating key and no poster path"`
11. `::"an inbox title uses the TMDB CDN and a pick uses the local endpoint"`
12. `web/src/test/pick-list.test.tsx::"every pick reserves its poster box, so a title with no art does not reflow the list"`
13. `::"collapsed picks are absent from the DOM, so their posters are never requested"`

E2E — `tests/e2e/test_screenshots.py` already builds rows against `fake_plex`; extend
`tests/fakes/fake_plex.py` to serve `/library/metadata/{key}` and a 1×1 PNG thumb so the existing
capture exercises the real path. Per the testing rule, **the fake must be no easier than the real
server**: it serves the thumb only behind the metadata read, exactly as a PMS does.

### 1.4 What could regress, and which test catches it

| Regression                                                    | Caught by                            |
| ------------------------------------------------------------- | ------------------------------------ |
| The Plex token ends up in an `<img src>` the browser can read | test 2                               |
| A missing poster collapses its row and the list reflows       | tests 9, 12                          |
| A PMS outage turns every list into broken-image icons         | test 4 + the `onError` fallback      |
| Opening a user page fires 40 PMS reads                        | test 13 (+ `collapseAfter`)          |
| A stale `rating_key` 500s the endpoint                        | test 3                               |
| The inbox and the pick list drift apart visually              | test 11 (one component, two sources) |

### 1.5 API / settings / migration impact

- **New route** `GET /api/picks/{rating_key}/poster` (binary response, no model).
- **No** new Pydantic response model, **no** new DB column, **no** Alembic migration, **no** settings key.
- `PickOut` / `UserPickOut` are unchanged — `rating_key` is not currently in either payload, so **one
  field must be added**: `rating_key: int` on `UserPickOut`
  (`shortlist/server/api/serializers.py:21-41` + `pick_dict` at `:74-100`) and on `PickOut`
  (`shortlist/server/api/schemas_runs.py:35-44`). Both already read from `PickRow`, which has it.
- **`pnpm gen:api` regeneration: YES.** Regenerate `web/openapi.snapshot.json` then
  `pnpm -C web gen:api`; `tests/unit/test_openapi_snapshot.py` fails otherwise.

### 1.6 Smallest useful version

**v1** = `TitlePoster` + the PMS proxy + `rating_key` on the two pick payloads + posters in
`PickList` only. Three files touched in the SPA, one new backend module, no migration.

**Not v1:** backdrops; hover cards with the synopsis; a TMDB fallback when the PMS has no art; the
separate inline renderer at `user-panel.tsx:145`; any server-side byte cache.

### 1.7 Open questions

- **The `/library/metadata/{key}` JSON shape needs a recorded fixture** before code leans on
  `MediaContainer.Metadata[0].thumb` (rule 11). It is well-trodden and the repo already parses
  `MediaContainer.Hub` the same way, but it has not been recorded here.
- `/photo/:/transcode?url=/library/metadata/{key}/thumb&width=…` would collapse two round-trips into
  one and let the PMS do the resizing. It is what Plex Web does, but this repo has no recorded
  evidence that the transcoder accepts a bare thumb URL. **Take it as an optimisation once a fixture
  exists, not as v1.**
- Does a shared row's pick, delivered into a library the owner has since removed, keep a resolvable
  `rating_key`? Expected `404` → placeholder, but unverified against a real server.

---

## 2. `dryrun-preview` — Preview beside Save

### 2.1 Current state (verified)

**Preview-then-commit is already the house pattern, in three places.**

- `web/src/components/rows/row-destructive-actions.tsx:43-49` — "A dry-run first (what WOULD be
  removed), then the real removal on confirm." Its dialog disables the real button when the preview
  **failed**, with the comment at `:180-184`: _"A dry run that FAILED tells us nothing about what is
  on the server. Leaving the button live here would fire the real removal with nobody having seen the
  diff, which is the one thing plex-safety rule 8 exists to prevent."_
- `web/src/pages/jobs.tsx:344-356` — "Preview first, then act", with a `confirmed: true` sentinel that
  only the human-pressed button carries.
- `web/src/pages/watching-account.tsx:439-462` — preview → commit, and it correctly re-reads the
  **returned** `dry_run` because safe mode can force it server-side (`:449-458`).

**"Dry-run is a mode you switch on" is only half right.** There is no `dry_run` key in
`settings_store`. The only global is the `SHORTLIST_DRY_RUN` env var, read by
`shortlist/server/safe_mode.py:15-17` and OR-ed in at every call site as a floor it can force ON and
never OFF (e.g. `collection_reconcile.py:368`). Per-action `dry_run` body fields already exist on
`POST /runs` (`shortlist/server/api/runs.py:31`), `POST /system/uninstall`
(`shortlist/server/api/system.py:608`), `POST /collections/{id}/rename`
(`collections.py:1248`), `POST /collections/{id}/cleanup` (`collections.py:1338`), and both
watching-account routes.

**The genuine gap, confirmed.** `PATCH /collections/{id}` (`collections.py:942`) and
`DELETE /collections/{id}` (`collections.py:1188`) accept no `dry_run` and no preview. This is the
`dry-run-gap` item in Wave 1; `dryrun-preview` is its UI half. No `wave1-safety-design.md` exists on
disk yet, so this section states the API contract it needs rather than assuming one.

**The decision table a preview must render is already a pure function.**
`shortlist/server/api/row_changes.py` exists precisely because the eleven-flag version was
"untestable without a Plex context". `plan_row_changes(change: RowChange, stranded_sections:
Callable[[], set[str]]) -> list[PlannedWork]` (`:105`) compares a before/after snapshot pair and
returns ordered work of five kinds: `RECONCILE`, `PRIVACY_SYNC`, `RENAME`, `POSTER_RESET`,
`VISIBILITY` (`:20-30`). The handler is already `validate → apply → plan → enqueue → drain`
(`collections.py:943-1105`), with `_snapshot` at `:1108`, `sent = body.model_fields_set` at `:954`,
and `await _apply_plan(state, plan_row_changes(change, stranded), …)` at `:1103`.

**This is the whole reason a preview is cheap here.** The plan needs no Plex write and, in most
cases, no Plex read at all — `stranded_sections` is deliberately a callable _"because answering it
costs a Plex read, and only a row whose media type or library list actually moved needs one"_
(`collections.py:1094-1096`).

### 2.2 The design

#### New endpoint

```python
@router.post("/{collection_id}/preview", response_model=RowPreviewOut)
async def preview_collection(collection_id: int, body: CollectionIn, request: Request) -> dict:
    """What saving this edit would change, here and on Plex — writing nothing.

    Takes the SAME body `PATCH /collections/{collection_id}` takes, so a preview and the save it
    previews cannot describe different edits.

    The plan is computed by running the REAL apply path inside a transaction that is rolled back,
    not by a second copy of the rules. A preview that reimplements the handler is a preview of code
    nobody runs; the first time the two drift, the preview is confidently wrong about a write that
    reaches somebody's Plex account.
    """
```

Implementation: extract the middle of `update_collection` into

```python
def _apply_patch(session, collection: Collection, body: CollectionIn, sent: set[str]) -> None
```

(exactly the field-assignment block currently at `collections.py:1000-1062`, unchanged), then:

- `PATCH` = `validate → _apply_patch → commit → snapshot → plan → enqueue → drain` (as today).
- `preview` = `validate → _apply_patch → snapshot → plan → **rollback** → serialise`.

`stranded_sections` still runs — it is a **read**, and rule 8 wants the preview to include the
destructive half. A preview that omitted the stranded-library removals would repeat the mistake
`privacy.py`'s dry-run enumeration already documents at `pipeline.py:719-721`: _"a preview that is
missing the only destructive half is worse than no preview"_.

#### Response

```python
class FieldChangeOut(PassthroughModel):
    """One setting this edit moves. `before`/`after` are already rendered for display."""
    field: str          # "audience", "media", "name_template", …
    label: str          # "Who gets this row"
    before: str
    after: str


class PlannedWorkOut(PassthroughModel):
    """One thing Shortlist would do on Plex, in the order it would happen."""
    kind: str                       # row_changes.RECONCILE | PRIVACY_SYNC | RENAME | POSTER_RESET | VISIBILITY
    detail: str                     # the plain-English sentence the dialog shows
    affects_users: list[str]        # display names, for a per-user reconcile
    affects_libraries: list[str]    # library names, for a stranded-section reconcile


class RowPreviewOut(PassthroughModel):
    changes: list[FieldChangeOut]
    plex_work: list[PlannedWorkOut]
    #: True when SHORTLIST_DRY_RUN is set, so the page can say the save will write nothing either.
    safe_mode: bool
```

A body that `PATCH` would reject with `422` gets the **same `422`, from the same validators** — so a
duplicate row name is caught before the save, not after it.

#### Interaction

Row editor sticky footer (`web/src/components/rows/row-editor.tsx:1353-1370`) gains a second button:

```
[ Preview changes ]  [ Save changes ]
```

`Preview changes` is `variant="outline"`, sits to the left of Save, and opens a dialog. It is **not**
a gate — Save works exactly as it does today whether or not a preview was run. This is deliberate and
must stay so: the automatic Privacy Check and its write gate were removed at the owner's request on
2026-07-16, and re-introducing a gate under a new name would be re-litigating a settled decision.

Dialog contents, in this order:

1. **Settings that change** — `changes`, one line each. Empty → _"Nothing here changes."_
2. **What happens on Plex** — `plex_work`, in plan order.
   Empty → _"Nothing on Plex changes until the next run."_
3. **Safe mode note**, when `safe_mode` — _"`SHORTLIST_DRY_RUN` is set on this server, so saving
   writes nothing to Plex either."_
4. Footer: `Cancel` · `Save changes` (fires the same mutation the sticky footer does).
   `Save changes` inside the dialog is **disabled when the preview errored** — the
   `row-destructive-actions.tsx:180` rule: a preview that failed tells you nothing, and a Save
   pressed from inside a dialog that failed to load is a Save nobody previewed. The footer's own
   Save is untouched.

#### Copy

Voice: plain English, the control says exactly what happens.

- Button: **Preview changes** (not "Dry run" — the owner is not running anything).
- Dialog title: **Before you save**
- Description: _"What saving this would change here, and what Shortlist would then do on Plex."_
- Per plan kind:
  - `RECONCILE`, whole row → _"Remove this row's collections from Plex for everyone who has it."_
  - `RECONCILE`, `only_user_ids` → _"Remove this row from Plex for 3 people: Sarah, Mike and Tom."_
  - `RECONCILE`, `in_sections` → _"Remove this row from the libraries it no longer covers: Movies 4K."_
  - `PRIVACY_SYNC` → _"Rewrite every Plex account's share filter, because who can see this row changed."_
  - `RENAME` → _"Rename this row's Plex collections from "✨ Movies Picked for You" to "✨ Movies For You"."_
  - `POSTER_RESET` → _"Put this row's Plex artwork back to Plex's own."_
  - `VISIBILITY` → _"Apply this row's day schedule on Plex now."_
- Empty plan → _"Nothing on Plex changes. This only updates settings here; the row rebuilds on its
  next run."_
- Preview failed → _"Couldn't work out what this would change — Shortlist couldn't reach Plex. Try
  again, or save without a preview from the button below the form."_

#### Coordination with `dry-run-gap` (Wave 1)

This design needs nothing from Wave 1 that Wave 1 does not already owe. Two contracts to keep aligned
when `wave1-safety-design.md` is written:

1. If Wave 1 adds `dry_run` to `PATCH /collections/{id}` **as a body field**, the preview endpoint
   here becomes redundant and should collapse into it — `PATCH … {dry_run: true}` returning
   `RowPreviewOut`. Preferable, and this design is written so the SPA changes by one line if so.
2. `DELETE /collections/{id}` needs a preview of the **DB** half (which rows, picks and overrides
   disappear); the **Plex** half is already covered by `POST /{id}/cleanup {dry_run: true}`. Wave 1's
   `oneclick-delete` owns the confirmation dialog; this design does not duplicate it.

### 2.3 Tests, written first

Python — `tests/unit/test_collection_preview.py`:

1. `TestRowPreview::test_a_preview_leaves_every_column_of_the_row_untouched` — snapshot the row,
   preview a body that changes six fields, re-read: byte-identical.
2. `::test_a_preview_plans_exactly_what_the_patch_would_execute` — preview a body, then `PATCH` the
   same body with `_apply_plan` monkeypatched to capture; assert the two `PlannedWork` lists are
   equal. **This is the test that keeps the preview honest**, and it is the one to break the code
   against (change one branch of `plan_row_changes` and confirm it fails).
3. `::test_a_preview_makes_no_write_call_to_plex_or_plex_tv` — `mock_plex`/`mock_plextv` assert no
   write method was called; only the stranded-sections read.
4. `::test_a_body_the_patch_would_reject_is_rejected_by_the_preview_with_the_same_message`
5. `::test_a_settings_only_edit_previews_an_empty_plex_plan`
6. `::test_narrowing_the_audience_previews_a_removal_for_exactly_the_dropped_users`
7. `::test_narrowing_the_libraries_previews_the_stranded_sections_and_nothing_else`
8. `::test_the_preview_reports_safe_mode_when_shortlist_dry_run_is_set` (`monkeypatch.setenv`)

Vitest — `web/src/test/row-preview-dialog.test.tsx`:

9. `"names every planned Plex action in plain English"`
10. `"an edit that owes Plex nothing says so instead of showing an empty list"`
11. `"a failed preview disables Save inside the dialog and says why"`
12. `"Save in the sticky footer still works when no preview was run"` — proves this is not a gate.
13. `"handles all four states: loading, error, empty and success"` (`.claude/rules/frontend.md`)

E2E — `tests/e2e/test_row_preview.py`:

14. `test_previewing_a_row_edit_writes_nothing_to_the_fake_plex_until_save` — assert `fake_plex`
    recorded zero collection mutations between opening the preview and pressing Save, and the
    expected ones after.

### 2.4 What could regress, and which test catches it

| Regression                                                           | Caught by       |
| -------------------------------------------------------------------- | --------------- |
| The preview's rollback leaks and a preview really saves              | test 1, test 14 |
| The preview drifts from what `PATCH` does                            | test 2          |
| The preview itself writes to Plex                                    | test 3, test 14 |
| The preview silently omits the destructive half (stranded libraries) | test 7          |
| Preview becomes a de-facto gate, re-creating the removed write gate  | test 12         |
| An empty plan renders as a blank dialog                              | test 10         |
| A safe-mode server tells the owner the save will write when it won't | test 8          |

### 2.5 API / settings / migration impact

- **New route** `POST /api/collections/{id}/preview` (or a `dry_run` field on the existing `PATCH`, if
  Wave 1 lands that first).
- **New response models** `RowPreviewOut`, `PlannedWorkOut`, `FieldChangeOut` in
  `shortlist/server/api/collections.py`.
- **Refactor only**, no behaviour change, to `update_collection`: extract `_apply_patch`.
- No settings key. No migration.
- **`pnpm gen:api` regeneration: YES.**

### 2.6 Smallest useful version

**v1** = preview for `PATCH /collections/{id}` only, one dialog, the five plan kinds rendered as
sentences. It is a read-only endpoint over a pure function that already exists.

**Not v1:** a preview for `DELETE` (Wave 1's `oneclick-delete` owns that dialog); a preview for
`PUT /api/settings`; a preview for the per-user row override `PUT`; a diff of the resulting _picks_
(that needs a run, not a plan).

### 2.7 Open questions

- Does Wave 1 intend `dry_run` as a **body field on `PATCH`** or a **separate route**? Decide once;
  two shapes for one idea is how the `sync.check` `confirmed` sentinel and the `dry_run` flag came to
  coexist on one job.
- `FieldChangeOut.before/after` are pre-rendered strings. Rendering them server-side keeps one copy
  of "how a `hub_anchor` object reads in English", but puts display text in the API. Alternative:
  return raw values and render in the SPA. **Recommend server-side**, because the plan sentences are
  already server-side and splitting them across the boundary guarantees drift.
- `_stranded_sections` is a Plex read inside a request handler. On a slow PMS this makes the preview
  slow. Acceptable, but if it bites, gate it behind "media or libraries actually moved" — which
  `plan_row_changes` already does, by only invoking the callable then.

---

## 3. `restriction-status` — showing that hiding actually worked

**The most important of the four, and the one where the audit's premise most needs correcting before
anything is designed.**

### 3.1 Current state (verified) — three verifications already exist

The claim that "nothing verifies hiding after the fact" is what `.claude/rules/plex-safety.md` says
about the **removed automatic Privacy Check and its write gate**, and that removal is real. What has
been built since is not a gate, and it is not nothing.

**(a) A batched read-back of every written filter, before any promotion.**
`shortlist/engine/pipeline.py:864-886`. After the privacy phase writes filters, the run does **one**
fresh `ctx.plextv.list_users()` and, for every account it wrote, checks that the `shortlist_*`
excludes it sent are actually present in what plex.tv now reports. A missing exclude sets
`sync_failed`, which **blocks promotion for the whole run**. `privacy.py:596-600` documents why it is
batched rather than per-user. This is a genuine read-back, not a restatement.

**(b) An enforcement spot-check that looks through a real account's eyes.**
`shortlist/engine/pipeline.py:551-638`, `_verify_filters_enforced`. Its own docstring: _"The read-back
above proves plex.tv STORED the filter string; nothing proved Plex acts on it."_ It mints a token for
one account **per `user_type`**, reads that account's `/hubs` as them (`plex_pms.py:1579`), and calls
`privacy.unhidden_rows_on_home` (`privacy.py:876`) to find other people's row ratingKeys on their
Home. Findings land in `report.filters_not_enforced`; `report.filters_enforcement_measured`
(`pipeline.py:638`) records whether the check actually **ran**, so an empty result cannot be
misread as "clean". Persisted at `run_persistence.py:1720-1721`, surfaced as a non-dismissable
notification at `notifications.py:635-680`. Pinned by `tests/unit/test_privacy_hub_enforcement.py`
against a **recorded real PMS `/hubs` response**.

**(c) A live, on-demand audit of every account's actual filters — already written, already correct,
and buried.** `GET /api/support/sharing` (`shortlist/server/api/support.py:2047-2233`). Its own
opening docstring is the audit's complaint, written by whoever built it:

> _"Rows are made private by merging `label!=shortlist_<user>` into each other account's filters, and
> the event log records what CHANGED. Nothing shows what is true right now — so a filter that was
> never written, or was overwritten by another tool, is invisible until someone reports a leak."_

It does, on every call:

- a live `plextv.list_users()` — every account's **current** `filterMovies`/`filterTelevision`;
- `privacy.parse_filter` on each, separating **our** `shortlist_*` values from the owner's own
  conditions, per label rather than per clause;
- `_existing_row_labels` (`support.py:377-402`) — a live PMS read of the per-person rows that
  **actually exist**, cross-checked against the invisible title marker so a label read that returned
  empty is reported as **unknown**, never as "no rows to hide";
- per account: `should_hide`, `missing`, `manage_sharing`, `other_conditions`, `filters`.

It returns that as structured `accounts` rows **plus** a rendered text block. It is gated behind
`require_support_mode` (`support.py:138-155`) — off by default even for the owner, expiring — and
rendered only as a monospace blob on the "Have an issue?" page (`web/src/pages/issue.tsx:135`).

**(d) What is genuinely missing.** The design doc's promise, `.claude/docs/shortlist-design.md:189-190`:

> _"…their restriction status (which labels are excluded on their share, when last synced, with a
> 'view snapshot history' link)."_

Nothing in the SPA delivers it. `RestrictedNote` (`web/src/components/restricted-note.tsx`) and
`RestrictedBadge`/`UnhiddenRowsBadge` (`web/src/components/user-badges.tsx:50,70`) render only for an
account with a **parental restriction profile** — the case Plex refuses a filter for. An ordinary
shared account, which is almost every account, has **no privacy UI at all**.
`shortlist/server/db/models.py:557-568` (`RestrictionSnapshotRow`) is exposed by no API. Filter-write
diffs are audited as `run.privacy_sync` events (`run_persistence.py:1437-1454`) and readable via
`GET /api/events/log?scope=run.privacy_sync` (`events.py:68`), but only as raw JSON.

**One stale comment to fix while here.** `shortlist/engine/models.py:1370-1377` still says
`filters_not_enforced` _"is currently written and read by nobody"_ on `dev`. It is written at
`pipeline.py:619` and read at `notifications.py:654`. The privacy work it was waiting on has landed.

### 3.2 What can honestly be proven — and what cannot

This is the section that decides the design. Three claims, three different kinds of evidence, three
different costs. **They must never be collapsed into one tick.**

| Claim                                                                 | Evidence                                                                         | Where it comes from                                                                  | Cost                                                               |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| **"plex.tv is storing an exclude for row X on account Y right now."** | A live roster read, parsed.                                                      | `PlexTvClient.list_users()` → `privacy.parse_filter` → `privacy.shortlist_labels_in` | **1** plex.tv read, whole roster                                   |
| **"Row X exists on the PMS right now."**                              | A live collections read, cross-checked against the invisible title marker.       | `support._existing_row_labels` → `PlexClient.owned_collections`                      | **1** PMS read                                                     |
| **"Plex is actually applying that exclude for account Y."**           | Read that account's Home **as them** and look for other people's row ratingKeys. | `PlexClient.user_hubs(token)` → `privacy.unhidden_rows_on_home`                      | **1** plex.tv token exchange **+ 1** PMS hub read, **per account** |

**What cannot be proven, at any cost, and must therefore never be claimed:**

1. **That anything outside Home obeys the filter.** `privacy.unhidden_rows_on_home`'s docstring is
   explicit: _"whether a real PMS applies a share `label!=` filter to the library COLLECTIONS listing
   (as opposed to Home) is not something this repo has a recorded answer for"_, and rule 11 forbids
   leaning on an unverified assumption. **The screen may speak only about Home**, and must say so.
   Related hubs, search and the Collections tab are outside what any evidence here covers.
2. **That an account with a parental restriction profile is hidden from anything.** plex.tv rejects
   the filter write outright (422, live-confirmed 2026-07-29 — `privacy.py:398-419`). For those
   accounts the only honest answer is the measurement `unhidden_rows_visible_to` already provides,
   and the remedy is in Plex, not here.
3. **That the owner's own view is private.** Plex has no share for the account that owns the server
   (rule 5). The owner sees every row. That is a Plex limitation, **not a fault**, and rendering it as
   one would train the owner to ignore this screen.
4. **Anything about an account we cannot mint a token for.** `canary_server_token` refuses a
   PIN-protected Home user (`plextv.py:264`, `watching_account.py:130`) — the archetype of exactly the
   account worth checking.
5. **Anything about the moment between checks.** Every answer on this screen is a reading with a
   timestamp, not a standing guarantee. Another tool, or the owner in Plex Web, can rewrite a filter a
   second later.

**The one design rule that follows, and it is non-negotiable:**

> **Never derive a "hidden" state from `report.filter_writes`, from the `run.privacy_sync` events, or
> from anything else that records what Shortlist WROTE.** Those record an intention that reached
> plex.tv. Reading them back as a privacy verdict is the second false-privacy bug, and it would be
> worse than the first because it would be on a page whose entire job is to be believed.

Every cell on this screen is either a live read, or the word **"not checked"**.

### 3.3 The design

#### One computation, two consumers

Move the body of `support.sharing` (`support.py:2047-2140`, everything up to the text rendering) into

```
shortlist/server/services/privacy_status.py :: read_sharing_status(session, store, machine_id) -> SharingStatus
```

unchanged. Then:

- `GET /api/support/sharing` keeps its text block, built from `read_sharing_status(...)`.
- `GET /api/privacy/status` (new, `require_owner`, **not** support-gated) returns the structured
  result as a response model.

Not a reimplementation. That module's correctness is hard-won — the `_existing_row_labels` fail-safe,
the labels-not-clauses counting, the `manage_sharing` split, the username-vs-slug bug — and every one
of those was a real reported failure. A second copy would relearn them.

#### `GET /api/privacy/status`

```python
class AccountPrivacyOut(PassthroughModel):
    """One Plex account's share filter, as plex.tv reports it RIGHT NOW."""

    username: str
    display_name: str
    slug: str
    plex_account_id: int
    user_type: UserType                 # shared | managed | owner
    restriction_profile: str            # "" unless Plex refuses filters for this account
    manage_sharing: bool                # False = the owner asked us to leave this account alone
    #: The `shortlist_*` labels currently in this account's filters, read from plex.tv this second.
    hides: list[str]
    #: Every per-person row that exists on Plex right now, minus this account's own.
    should_hide: list[str]
    #: `should_hide` minus `hides` — rows this account can see that are not theirs. Empty means
    #: plex.tv is STORING every rule. It does not mean Plex is applying them (see `enforcement`).
    missing: list[str]
    #: The account's own filter conditions, untouched by Shortlist — shown so the owner can see we
    #: preserved them (rule 3).
    other_conditions: list[str]


class EnforcementOut(PassthroughModel):
    """The last time a run looked through a real account's eyes at their Home screen."""

    measured: bool                      # report.filters_enforcement_measured
    run_id: int | None
    measured_at: str | None             # ISO, UTC
    #: username -> ratingKeys of other people's rows visible on THEIR Home. Empty + measured = clean.
    not_enforced: dict[str, list[int]]


class PrivacyStatusOut(PassthroughModel):
    read_at: str                        # when THIS response was read from plex.tv
    accounts: list[AccountPrivacyOut]
    #: Labels of the per-person rows that exist on Plex right now — what the verdict was measured
    #: against. Lets a caller tell "everyone is covered" from "there was nothing to cover".
    rows_on_plex: list[str]
    #: Why the PMS row read failed, when it did. Non-null means the whole verdict is UNKNOWN.
    rows_error: str | None
    #: Why the plex.tv read failed, when it did. Non-null means `accounts` is empty and nothing may
    #: be reported as hidden.
    error: str | None
    enforcement: EnforcementOut
```

`enforcement` is read from the **latest run that carries the key**, exactly as
`notifications._filters_not_enforced` (`notifications.py:644-658`) already does — _not_ the latest
run — because an errored run carries no measurement and reading it as "clean" is how a live alert
gets cleared.

#### The screen

New route `/sharing`, linked from Settings and from the Users page header (not buried in a Settings
section — the "don't bury features in Settings" rule). Title: **Sharing and privacy**.

**Top: one honest summary line.** Four possible states, in priority order:

| State                              | Line                                                                                         |
| ---------------------------------- | -------------------------------------------------------------------------------------------- |
| `error` set                        | _"Couldn't read your Plex sharing settings from plex.tv. Nothing below is current."_ + retry |
| `rows_error` set                   | _"Couldn't read your rows from Plex, so there is nothing to check the filters against."_     |
| any `missing` on a managed account | _"2 people can see a row that isn't theirs."_ (destructive-text)                             |
| otherwise                          | _"Every account hides all 12 rows that aren't theirs. Read from plex.tv just now."_          |

**Middle: an accounts table**, one row per plex.tv account. Columns:

- **Person** — avatar + display name, linking to their user detail.
- **Hides** — `12 of 12`. Not a tick. A number, so "12 of 12" and "0 of 0" cannot look alike.
- **Status** — one of:
  - _"Hiding every row — read from plex.tv just now"_
  - _"Missing 2 hide rules"_ + the row names, expandable
  - _"Left alone by choice"_ (when `manage_sharing` is false)
  - _"Plex won't accept hide rules for this account"_ (parental profile) → links to the existing
    `RestrictedNote` copy, which already explains the remedy
  - _"You own the server"_ (owner)
- **Their own filters** — a collapsed disclosure showing `other_conditions`, so the owner can see
  Shortlist preserved them byte-for-byte (rule 3 made visible).

**Bottom: an enforcement panel, kept visually separate**, because it answers a different question:

- `measured` and `not_enforced` empty → _"Checked in run #418, 6 hours ago: Plex was applying the
  hide rules on the accounts checked. Shortlist checks one account of each kind, on the Home screen
  only."_
- `not_enforced` non-empty → the existing `notifications.py:665-678` copy, verbatim. It is already
  right, and it already says to file an issue rather than change a setting.
- `measured` false → _"Not checked recently. The last few runs didn't get as far as looking, so
  nothing here says whether Plex is applying the rules."_ **Never "clean".**

**On the user detail page** (`web/src/pages/user-detail.tsx`, beside `RestrictedNote` at `:51`), a
compact `SharingStatusCard` for that one person, delivering the design doc's line 190 promise:

- which labels are excluded on their share, right now, from plex.tv;
- when their filter was last **written**, from the newest `run.privacy_sync` event for their
  `plex_account_id` (`GET /api/events/log?scope=run.privacy_sync`) — labelled _"last changed"_, never
  _"last verified"_;
- a _"View snapshot history"_ link → their `restriction_snapshots` rows, showing the filters as they
  were **before Shortlist ever touched them** (rule 2 made visible, and the thing uninstall restores).

#### Copy — the exact distinctions that matter

- Never _"Hidden ✓"_. Always **"Hiding every row — read from plex.tv just now"**. The evidence is in
  the sentence.
- Never _"Verified"_ for a written filter. **"Last changed 6 hours ago (run #418)."**
- Never an empty enforcement result as _"All clear"_. **"Not checked recently."**
- Owner: _"You own the server, so Plex has no share to filter for you — your Home shows every row.
  That's Plex, not a fault. To see what your users see, watch on a non-owner account."_
- Left alone: _"You asked Shortlist to leave this account's Plex sharing alone, so it hides nothing.
  That's the setting, not a fault."_
- The scope caveat, stated once at the bottom of the enforcement panel and not repeated:
  _"These checks cover the Home screen. Shortlist has no way to confirm what Plex does on the
  Collections tab or in Related shelves."_

### 3.4 Tests, written first

Python — `tests/unit/test_privacy_status.py`:

1. `TestPrivacyStatus::test_a_verdict_is_never_taken_from_what_we_wrote` — seed a `Run` whose
   `stats["filter_writes"]` records a successful write for account 202, but make the fake plex.tv
   report that account **without** the exclude. The status must report `missing`. **This is the test
   with teeth; break the code by sourcing `hides` from the run stats and confirm it fails.**
2. `::test_a_failed_plextv_read_reports_unknown_and_no_account_as_hiding` — `list_users` raises →
   `error` set, `accounts == []`, and nothing renders as hidden.
3. `::test_a_failed_pms_row_read_withholds_the_verdict` — `rows_error` set; no account may be
   reported clean off zero rows (the `_existing_row_labels` fail-safe, at the API boundary).
4. `::test_an_account_missing_an_exclude_names_the_rows_it_can_see`
5. `::test_the_owner_is_reported_as_a_plex_limitation_not_a_fault`
6. `::test_a_left_alone_account_is_reported_as_a_setting_not_a_fault`
7. `::test_an_account_with_a_parental_profile_is_reported_as_refused_by_plex`
8. `::test_enforcement_reports_not_measured_when_the_latest_measuring_run_did_not_measure` — mirrors
   `test_notifications.py:1028-1055`, which already pins this shape for the notification.
9. `::test_enforcement_reads_the_latest_run_that_MEASURED_not_the_latest_run`
10. `::test_the_support_tool_and_the_status_endpoint_come_from_one_computation` — same fakes, assert
    `/api/support/sharing`'s `accounts` equals `/api/privacy/status`'s `accounts`. Prevents drift.
11. `::test_the_status_endpoint_is_owner_gated_but_not_support_gated`

Vitest — `web/src/test/sharing-page.test.tsx`:

12. `"a failed plex.tv read renders as 'not current', never as hidden"`
13. `"a missing hide rule names the rows and what to do about it"`
14. `"the owner row explains the Plex limitation instead of showing a fault"`
15. `"an unmeasured enforcement check says 'not checked recently', not 'all clear'"`
16. `"the enforcement panel names the run and when it measured"`
17. `"handles all four states: loading, error, empty and success"`

Vitest — `web/src/test/sharing-status-card.test.tsx`:

18. `"shows the labels excluded on this person's share, read from plex.tv"`
19. `"labels the last privacy_sync event 'last changed', never 'last verified'"`

E2E — `tests/e2e/test_privacy_status.py`:

20. `test_the_sharing_page_names_an_exclude_the_fake_plex_tv_really_is_missing` — plant a filter on
    `tests/fakes/fake_plex.py` that omits one `shortlist_*` label; assert the page names that row.
    The fake must serve the same filter shape a real plex.tv does (testing rule: the fake must be no
    easier than the real server).

### 3.5 What could regress, and which test catches it

| Regression                                                                     | Caught by                               |
| ------------------------------------------------------------------------------ | --------------------------------------- |
| **The page sources "hidden" from run stats or events** — the false-privacy bug | test 1                                  |
| A plex.tv outage renders as a green, reassuring page                           | test 2                                  |
| A failed PMS read renders as "nothing to hide"                                 | test 3                                  |
| An unmeasured enforcement check clears a live alert                            | tests 8, 9, 15                          |
| The support tool and the page drift as one is fixed                            | test 10                                 |
| The page claims coverage of the Collections tab                                | copy review; no code path can assert it |
| The new endpoint leaks past `require_owner`                                    | test 11                                 |

### 3.6 API / settings / migration impact

- **New module** `shortlist/server/services/privacy_status.py` (a move, not a rewrite).
- **New routes**: `GET /api/privacy/status`; `GET /api/users/{id}/snapshots` for the snapshot-history
  link (reads `RestrictionSnapshotRow`, which no API exposes today).
- **New response models** `PrivacyStatusOut`, `AccountPrivacyOut`, `EnforcementOut`, plus a snapshot
  model.
- **No migration.** Every field is already stored.
- **No settings key.**
- **`pnpm gen:api` regeneration: YES.**
- **Docs (`.claude/rules/docs.md`)**: `docs/reference.md` gains the two endpoints; `docs/guides.md`
  gains a "How do I check my rows are hidden?" section — currently the honest answer is "turn on
  support mode and read a text blob", which is why this item exists.
- **Also fix while here:** the stale note at `shortlist/engine/models.py:1370-1377`.

### 3.7 Smallest useful version

**v1** = un-bury what exists. `GET /api/privacy/status` over the extracted computation, the
`/sharing` page, and the enforcement panel reading the last measuring run. **No new Plex capability
whatsoever** — two reads the support tool already performs, plus one DB query. Perhaps a day.

**Deliberately v2, not v1:** an on-demand _"Check what Sarah can actually see"_ button that mints a
canary token and reads her Home live. It is a real capability — `run_service.build_context(dry_run=…,
plex_only=True)` (`jobs.py:1467`) already does the token exchange, and `unhidden_rows_on_home` is the
function — but it costs a plex.tv token exchange plus a PMS read **per press**, needs a job because it
is slow, and needs its own thinking about what a refusal (PIN-protected account) renders as. Ship the
free honesty first.

**Also v2:** a "Fix now" button that queues `privacy.sync` from the page (`api.runJob("privacy.sync",
{}, true)` already exists — `jobs.tsx:359-363`). Low cost, but it is a **write** from a screen whose
job is to report, so it belongs in a second pass with its own confirmation.

### 3.8 Open questions

- **Does a real PMS apply a share `label!=` filter to the library Collections listing?** Unrecorded,
  and `privacy.py:882-888` explicitly declines to guess. Until a fixture exists, the screen must
  restrict every claim to Home. **Worth answering** — the answer either widens what this screen can
  say or names a second exposure surface.
- Should `/api/privacy/status` be **rate-limited**? It costs a plex.tv roster read and a PMS
  collections read per call, and TanStack Query will refetch on window focus. Recommend a
  `staleTime` of 60s in the SPA and no server-side limit; revisit if a 48-account server feels it.
- **Snapshot history**: `RestrictionSnapshotRow.filters_before` is the pre-Shortlist filter and the
  only copy that exists (`models.py:561-563`, `ondelete="RESTRICT"`). Showing it read-only is safe.
  Do **not** add a "restore this snapshot" button on this screen — uninstall owns that flow, and a
  per-account restore has no design.
- Does the owner want `/api/support/sharing` to stay support-gated once `/api/privacy/status` exists?
  Recommend **yes**: the text block is for pasting into an issue, and the gate's audited,
  self-expiring nature is a deliberate property (`support.py:141-144`).

---

## 4. `bulk3state` — "no change" in a bulk edit

### 4.1 Current state (verified) — the premise is false

**There is no multi-field bulk edit anywhere in the SPA.** Searched `web/src` for `bulk`, `Bulk`,
`batch`, `Apply`, `selected`, `indeterminate`, `tristate`, `no change`. What exists:

- **`POST /api/users/set-enabled`** (`shortlist/server/api/users.py:356-374`). Body is
  `BulkEnabled { enabled: bool }` — **one required field, no optional fields**, so there is nothing to
  clobber. It applies to **every** user unconditionally; there is no selection. Called from
  `web/src/pages/users.tsx:158-176` ("Enable all" / "Disable all", behind confirms at `:264-330`) and
  from the wizard's auto-select at `web/src/pages/setup/step-users.tsx:55-75`.
- **The requests inbox toolbar** (`web/src/pages/requests.tsx:1577-1650`) is multi-select, but every
  action is a verb — Send, Reject, Delete — not a field edit. Nothing to leave alone.
- **`run-rows-dialog.tsx:102`** is multi-select for "run these rows". Also a verb.

**Every partial-write path already distinguishes "absent" from "null", correctly:**

- `PATCH /api/collections/{id}` — `sent = body.model_fields_set` (`collections.py:954`), with the
  comment _"Only touch fields the request actually sent, so a partial PATCH (e.g. an enable toggle)
  never resets the columns it omitted back to `CollectionIn`'s defaults."_
- `PUT /api/users/{id}/rows/{collection_id}` — `sent = patch.model_fields_set`
  (`user_rows.py:159-167`), and it is already a genuine three-state: `None` on `row_size` /
  `recent_count` means **"use the row's own"**, `RowOverrideOut`'s docstring says so, and the UI
  already exposes a "Default" choice.
- `PUT /api/settings` — a sparse `{values: {...}}` map (`settings.py:462-520`). Only what you send.

**Prior art for a "leave this alone" sentinel already exists**, and it is a string, not a control:
`REDACTED_PLACEHOLDER = "•••••"` (`settings.py:41`), round-tripped by
`web/src/components/ui/secret-input.tsx`, `connection-card.tsx:211` and
`settings/inline-key-field.tsx:64`.

**A three-state control primitive also already exists**: `web/src/components/segmented.tsx` is
generic over `T extends string` and renders N chip buttons with `aria-pressed`. A three-state control
is `Segmented<"keep" | "on" | "off">`, not a new component.

### 4.2 The one real instance of the bug shape

`shortlist/server/api/users.py:431`, in `patch_user`:

```python
prefs.update({k: v for k, v in patch.prefs.model_dump().items() if v is not None})
```

`model_dump()` — **not** `model_dump(exclude_unset=True)`. Every `UserPrefs` field
(`users.py:51-62`: `row_name_tpl`, `excluded_genres`, `blocked_seeds`, `paused`) happens to default to
`None`, so `if v is not None` accidentally does the right thing today. Two consequences:

1. **A pref can never be cleared.** `{"prefs": {"paused": null}}` is silently ignored. The only way to
   unpause is `{"paused": false}`, which stores a `false` rather than removing the key. Minor today;
   it becomes a real bug the moment a pref's absence and its `false` mean different things.
2. **It becomes a genuine clobber the day someone adds a `UserPrefs` field with a non-`None`
   default.** That field would then be written into every user's `prefs` on every unrelated PATCH,
   overwriting whatever was stored. Nothing in the file warns against it.

**Fix, one line, now, whether or not the bulk feature is built:**

```python
# `exclude_unset`, not a None filter: "the client did not mention this field" and "the client set it
# to null" are different instructions, and only the first means "leave it alone". The None filter
# collapsed them, so a pref could never be cleared — and it would silently start CLOBBERING the day
# a UserPrefs field gained a non-None default, writing that default into every user on every
# unrelated PATCH. `model_fields_set` is the same rule PATCH /collections and PUT …/rows already use.
sent = patch.prefs.model_fields_set
for key in sent:
    value = getattr(patch.prefs, key)
    if value is None:
        prefs.pop(key, None)   # explicit null clears the override
    else:
        prefs[key] = value
```

### 4.3 The design — build the bulk edit, three-state from the first line

The item's _intent_ is right even though its premise is wrong: applying one setting to forty accounts
one at a time is the real pain on the Users page, and `POST /users/set-enabled`'s all-or-nothing
answer is the crude version of it. Build the thing that does not exist, and build it three-state,
because building it two-state is precisely how the audit's predicted bug gets created.

#### New endpoint

```python
class UserBulkChanges(BaseModel):
    """Fields a bulk edit may set. A field the request OMITS is left alone on every selected user.

    Read with `model_fields_set`, never with a `is not None` filter: those two say different things,
    and only the first can express "no change" — which is the whole point of this model. An explicit
    `null` is reserved for "clear", so a caller can distinguish it from "don't touch".
    """

    enabled: bool | None = None
    manage_sharing: bool | None = None
    paused: bool | None = None
    request_tag: str | None = Field(default=None, max_length=64)


class UserBulkIn(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)
    changes: UserBulkChanges


class UserBulkOut(PassthroughModel):
    """What the bulk edit did — per FIELD, because the owner asked for several at once."""

    matched: int
    changed_by_field: dict[str, int]   # {"enabled": 12, "paused": 0}
    #: Accounts a change could not apply to, and why: the owner (never restricted, rule 5), an
    #: account with a parental profile, or one plex.tv no longer lists. Named, never silently dropped.
    skipped: list[dict]


@router.post("/bulk", response_model=UserBulkOut)
async def bulk_patch_users(body: UserBulkIn, request: Request) -> dict: ...
```

The handler **reuses `patch_user`'s per-user side-effect logic**, it does not restate it. Turning
someone off must still remove their rows from Plex; turning them back on must still queue a privacy
sync; pausing must still take the row down now (`users.py:395-448`). Extract that block into
`_apply_user_patch(session, user, patch) -> UserSideEffects` and have both routes call it, then
coalesce the side effects across the batch into **one** `remove_users_rows` call and **one**
`queue_privacy_sync`. Forty separate privacy syncs would be forty roster reads.

`manage_sharing` is the field that makes this worth doing carefully: it is the one write in the whole
codebase that **widens** what an account can see (`privacy.clear_our_excludes`, rule 3). A bulk flip of
it is a real privacy action and the confirm dialog must say so in those words.

#### The three-state control

```tsx
/**
 * A field in a bulk edit: leave it alone, turn it on, or turn it off.
 *
 * "No change" is the DEFAULT and the first option, because it is what the owner means about every
 * field they did not come here to change. A two-state control cannot say it — which is how a bulk
 * edit ends up writing a default over forty accounts' worth of settings nobody touched.
 */
export function TriStateField({
  label,
  value,
  onChange,
  hint,
}: {
  label: string;
  value: "keep" | "on" | "off";
  onChange: (v: "keep" | "on" | "off") => void;
  hint?: string;
}) {
  return (
    <Segmented<"keep" | "on" | "off">
      legend={label}
      value={value}
      onChange={onChange}
      options={[
        { value: "keep", label: "No change" },
        { value: "on", label: "On" },
        { value: "off", label: "Off" },
      ]}
    />
  );
}
```

Serialisation is the load-bearing half and belongs in one tested function:

```ts
/** `keep` omits the key entirely — that is what makes the API leave it alone. */
export function bulkChanges(form: BulkForm): Partial<UserBulkChanges> { … }
```

#### Interaction on the Users page

1. A checkbox per row plus a header "select all on this page". Selecting anything reveals a sticky
   toolbar: **"4 selected"** + **Edit selected…** + **Clear**.
2. **Edit selected…** opens a dialog with one `TriStateField` per boolean field, all defaulting to
   **No change**, plus `request_tag` as a text field with its own explicit _"Leave as they are"_
   checkbox (a text field's empty string genuinely means "clear", so it needs its own sentinel).
3. A live summary above the footer, rebuilt from the form, not from the response:
   _"Will change 2 settings on 4 people. Everything else is left as it is."_
   Nothing selected → _"Nothing selected — this will change nothing."_ and the button is disabled.
4. If `manage_sharing` is set to **Off**, an inline warning in the dialog:
   _"Turning Plex sharing management off takes Shortlist's hide rules back off these accounts. They
   will be able to see other people's rows. Everyone else still can't see theirs."_
5. On success, a toast naming what changed **per field**: _"Turned 4 people off. Left everything else
   as it was."_ Never a bare "Saved".

`POST /users/set-enabled` stays as the "Enable all" / "Disable all" shortcut. It is a different
gesture (everyone, no selection) and removing it would break the wizard's auto-select
(`step-users.tsx:70-73`).

### 4.4 Tests, written first

Python — `tests/unit/test_users_bulk.py`:

1. `TestUserBulk::test_a_field_the_request_omits_is_left_alone_on_every_selected_user` — seed three
   users with distinct `request_tag`s, send `{"enabled": false}` only, assert all three tags survive.
   **The test the item exists for.**
2. `::test_an_explicit_null_clears_the_field_rather_than_being_ignored` — the distinction
   `model_fields_set` buys, which a `is not None` filter cannot express.
3. `::test_turning_people_off_removes_their_rows_from_plex_once_not_once_each` — assert
   `remove_users_rows` called **once** with all four slugs.
4. `::test_turning_people_on_queues_exactly_one_privacy_sync`
5. `::test_the_owner_is_skipped_for_manage_sharing_and_named_in_skipped` — rule 5; mirrors
   `users.py:400-406`.
6. `::test_setting_manage_sharing_off_in_bulk_queues_the_same_privacy_sync_the_single_patch_does`
7. `::test_the_response_counts_changes_per_field_not_per_user`
8. `::test_an_empty_user_id_list_is_a_422_not_a_silent_no_op`

Python — `tests/unit/test_users.py` (the one-line fix, independent of the bulk feature):

9. `TestPatchUserPrefs::test_an_explicit_null_clears_a_pref_instead_of_being_ignored`
10. `::test_a_prefs_field_the_request_omits_is_not_written_even_when_its_model_default_is_not_none` —
    add a throwaway `UserPrefs` field with a non-`None` default in the test and prove it is not
    written. **This is the regression the current code will acquire; break the code by reverting to
    `model_dump()` and confirm it fails.**

Vitest:

11. `web/src/test/bulk-user-edit.test.tsx::"every field defaults to No change"`
12. `::"a field left on No change is omitted from the request body entirely"` — assert the body, not
    that a call happened (the "assert the kwargs" rule).
13. `::"the summary names how many settings and how many people, before you press anything"`
14. `::"selecting nothing disables the button and says so"`
15. `::"turning Plex sharing management off warns that those accounts will see other people's rows"`
16. `web/src/test/bulk-changes.test.ts::"keep omits, on sends true, off sends false"` — the pure
    serialiser, exhaustively.

E2E — `tests/e2e/test_bulk_user_edit.py`:

17. `test_a_bulk_edit_does_not_disturb_settings_it_was_not_asked_to_change` — three users through the
    real UI against `fake_plex`.

### 4.5 What could regress, and which test catches it

| Regression                                                                                | Caught by       |
| ----------------------------------------------------------------------------------------- | --------------- |
| A `keep` field is serialised as `false` and clobbers 40 accounts                          | tests 1, 12, 16 |
| The handler goes back to `is not None` and "clear" stops working                          | tests 2, 9      |
| A `UserPrefs` field gains a non-`None` default and starts overwriting                     | test 10         |
| The bulk path forgets a side effect `patch_user` does (rows not removed, no privacy sync) | tests 3, 4, 6   |
| Forty accounts cause forty privacy syncs                                                  | tests 3, 4      |
| The owner gets a `manage_sharing` write Plex cannot honour                                | test 5          |
| A bulk `manage_sharing: off` happens with no warning about what it exposes                | test 15         |

### 4.6 API / settings / migration impact

- **New route** `POST /api/users/bulk`; new models `UserBulkIn`, `UserBulkChanges`, `UserBulkOut`.
- **Refactor**, no behaviour change: extract `_apply_user_patch` from `patch_user`.
- **One-line fix** at `users.py:431` (independent, ship it first).
- No migration. No settings key.
- **`pnpm gen:api` regeneration: YES.**

### 4.7 Smallest useful version

**v1** = the one-line `prefs` fix + `POST /api/users/bulk` with **`enabled` and `paused` only** +
`TriStateField` + the selection toolbar. Two booleans is enough to prove the pattern and covers the
two things an owner actually does in bulk.

**Not v1:** `manage_sharing` in bulk (it is the widening write — do it once the pattern is proven, with
its own confirm); `request_tag` (a text field needs a fourth state and its own sentinel); bulk editing
rows; bulk editing per-user row overrides.

### 4.8 Open questions

- **Should this item be reframed in the programme?** `bulk3state` as written describes a bug that does
  not exist. Its honest description is _"there is no bulk edit; build one, and build it three-state"_.
  Worth correcting in `audit-2026-09-programme.md` so the next reader does not go hunting for the
  clobber.
- Is bulk edit even wanted? The audit inferred the need from Diskovarr rather than from a request.
  On a 5-user server it earns nothing; on a 48-user server it is obvious. **Ask before building**, and
  ship the one-line `prefs` fix regardless — that one is a latent bug either way.
- `manage_sharing` in bulk is the only genuinely dangerous field here. If the owner wants it, it may
  deserve a typed confirmation like uninstall's `UNINSTALL`, rather than a checkbox.

---

## Cross-cutting notes

1. **Two of the four items are mostly un-burying, not building.** `restriction-status` v1 is a page
   over a computation that already exists and is already correct; `dryrun-preview` v1 is a rollback
   around a pure function that already exists. Neither needs a new Plex capability. That is where the
   value is concentrated.
2. **Three of the four need `pnpm gen:api`** and a regenerated `web/openapi.snapshot.json`
   (`tests/unit/test_openapi_snapshot.py` enforces it). Only `posters` is close to schema-free, and
   even it adds `rating_key` to two pick payloads.
3. **Only one migration across all four: none.** `posters` avoids one by keying on `rating_key`;
   `restriction-status` reads fields already stored; the other two are API-shape work.
4. **Architecture Review**: `restriction-status` reads share filters and mints no tokens in v1 — it is
   read-only, but it _reports on privacy_, and a wrong report here is the failure mode the whole item
   exists to prevent. Per `.claude/CLAUDE.md`'s risk list, include it in the wave's single review.
   `bulk3state`'s `manage_sharing` field writes share filters, so if it lands in v1 it is definitely in
   scope. `posters` and `dryrun-preview` are read-only and UI-shaped.
5. **Verify on the live server from a NON-OWNER account** (the programme's definition of done, item 4).
   For `restriction-status` this is not optional: the owner sees every row by design, so an owner-side
   check of a privacy screen proves nothing at all.

## Files read for this design (read-only, no edits)

`shortlist/engine/privacy.py` (353-420, 590-610, 860-973) · `shortlist/engine/pipeline.py` (430-680,
738-895) · `shortlist/engine/models.py` (160-260, 800-870, 1340-1400) · `shortlist/engine/picker.py`
(55-95) · `shortlist/engine/rows.py` (2008-2035, 2878-2900, 3025-3050) ·
`shortlist/engine/clients/plextv.py` (30-200, 280-370) · `shortlist/engine/clients/plex_pms.py`
(440-500, 1570-1600) · `shortlist/engine/clients/tmdb.py` (180-215) ·
`shortlist/engine/candidates.py` (585-600) · `shortlist/server/api/support.py` (138-165, 366-402,
2047-2233) · `shortlist/server/api/users.py` (45-115, 240-480) · `shortlist/server/api/user_rows.py`
(1-172) · `shortlist/server/api/collections.py` (58, 942-1110, 1188, 1248, 1338-1380, 1420-1510) ·
`shortlist/server/api/row_changes.py` (1-175) · `shortlist/server/api/settings.py` (1-60, 456-520) ·
`shortlist/server/api/events.py` (1-86) · `shortlist/server/api/serializers.py` (1-110) ·
`shortlist/server/api/schemas_runs.py` (1-90) · `shortlist/server/notifications.py` (625-700) ·
`shortlist/server/services/run_persistence.py` (1425-1480, 1690-1740) ·
`shortlist/server/services/jobs.py` (1440-1545) · `shortlist/server/services/run_service.py` (77-90) ·
`shortlist/server/safe_mode.py` · `shortlist/server/db/models.py` (300-340, 489-534, 557-570,
786-830) · `web/src/components/pick-list.tsx` · `web/src/components/restricted-note.tsx` ·
`web/src/components/segmented.tsx` · `web/src/components/rows/row-destructive-actions.tsx` ·
`web/src/pages/requests.tsx` (70-110, 1120-1700) · `web/src/pages/user-detail.tsx` (1-90) ·
`web/src/pages/jobs.tsx` (335-400) · `web/src/pages/setup/step-users.tsx` (30-120) ·
`web/src/lib/types.ts` (220-240, 980-996) · `web/package.json` · `pyproject.toml` ·
`tests/unit/test_openapi_snapshot.py` · `.claude/docs/shortlist-design.md` (100-135, 180-250,
295-340) · `.claude/docs/audit-2026-09-programme.md` · `.claude/docs/orphan-guard-design.md` ·
`.claude/rules/{frontend,plex-safety,testing,docs}.md`
