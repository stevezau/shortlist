# "Your requests" Row Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Every subagent dispatched for this plan runs with `model: "fable"`** (owner instruction 2026-09-28, mandatory until the 2026-10-02 reset).

**Goal:** A private per-person row of the titles each person requested in Overseerr (or that carry
their tag in Radarr/Sonarr) that are now on Plex and they haven't watched, newest arrival first
(issue #127).

**Architecture:** One engine read per run (`requests_row.collect_requests`) turns Overseerr's request
list, Overseerr's user list and the Radarr/Sonarr tag lists into a `RequestLedger` keyed by Plex
account ID. A new row kind (`RowSpec.requests_row`) short-circuits `_run_user` BEFORE candidate
gathering: it filters the ledger by library, watch state, visibility and age, then hands ordinary
`Pick`s to the existing delivery. It is the first row kind that REMOVES its collection when a person
has nothing to show, and that removal is gated on a complete source read. Privacy is untouched: the
row carries the usual `shortlist_<slug>` label and goes through the same hide-then-promote order.

**Tech Stack:** Python 3.12, httpx + respx, SQLAlchemy 2 + Alembic, FastAPI/Pydantic v2, pytest;
React 19 + Vite + TS + vitest for the editor.

**Spec:** Design mockup https://claude.ai/artifact/Xt8Kf536mjnGRUxpukaV6W (v4, 2026-09-28) and the
memory note `issue-127-your-requests-row`. Decisions taken by recommendation (owner said "build it"):
remove the row when empty; default window 90 days ("Show titles that landed in the last N days",
0 = keep until watched); Plex Watchlist auto-requests count as the person's requests.

## Global Constraints

- **Match people by Plex account ID only.** Overseerr `requestedBy.plexId` == `users.plex_account_id`.
  Never by display name. An Overseerr requester tag (`<seerrUserId>-<username>`) resolves through
  Overseerr's user list to a plexId; a tag that resolves to nobody, or a pattern tag that fits two
  people, is IGNORED and reported, never guessed.
- **Removal only on a complete read.** `RequestLedger.complete` is False if ANY configured source
  raised or returned a partial page; then no requests row is removed anywhere this run. A history
  read failure already stops `_run_user` before any row (rows.py `except RuntimeError` → status error).
- **No recommendations in this row.** No candidate pool, no curator, no cold-start padding, no
  `_pad_picks`. The row may be short or absent.
- **Leave out Shortlist's own requests.** A Seerr request filed by the "Request as" account
  (`requests.overseerr.request_as_user_id`, only when `requests.enabled` and target is overseerr) is
  skipped; a Radarr/Sonarr item carrying Shortlist's global tag (`requests.tag`, default `shortlist`)
  never matches an OWN-PATTERN tag (Overseerr-format tags are never written by Shortlist, so they
  still count).
- **Sources are independent of the request feature.** The ledger reads Overseerr/Radarr/Sonarr
  whenever their URL + key are set, regardless of `requests.enabled` or `requests.target`.
- **Plex-safety rules apply**: every delete goes through `remove_row` (which already uses
  `delete_owned_collection` + the ledger), under `ctx.write_lock`, honouring `dry_run`.
- Engine purity: `shortlist/engine/` imports nothing from `shortlist/server/`.
- Style: ruff, 120 cols, type hints, Google docstrings, `from loguru import logger`; frontend rules in
  `.claude/rules/frontend.md` (no `any`, generated API types, four states per data view).
- Copy: "Overseerr" in UI text (the app supports Jellyseerr through the same client; the connections
  card already says "Overseerr/Jellyseerr").
- Tests: one scoped `pytest tests/unit/test_x.py` per task, never the whole suite until Task 12.

## Review Focus

1. **Two of the person's rows title-collide in one library.** A requests row named
   `{library_name} you asked for` and another row rendering the same title share one collection
   (rows.py:3392 warning). Pinned in Task 7: the template's default name is unique among shipped
   templates (`test_the_requests_template_name_is_unique_among_templates`).
2. **Seerr returns 200 with an empty `results` and a `pageInfo.results` of 0 during a rebuild.**
   Must read as complete-and-empty, not as a failure — but a row removal on that basis would be
   wrong if last night had rows. Pinned in Task 5: `complete` is False when Seerr reports zero
   requests AND zero users (`test_zero_users_and_zero_requests_reads_as_a_failed_read`).
3. **A person's `plex_account_id` appears as `plexId` on TWO Overseerr accounts** (a local account
   later linked to Plex). Pinned in Task 5: both Seerr ids map to the person; requests from either
   count (`test_two_seerr_accounts_with_one_plex_id_both_belong_to_that_person`).
4. **Sonarr series with no `tmdbId`** (older Sonarr, or an unmatched series). Pinned in Task 5: it
   is skipped with a problem line, never crashes (`test_a_series_without_tmdb_id_is_skipped_and_reported`).
5. **A 4K-only request whose ordinary copy is already on Plex.** Pinned in Task 5: an `is4k` request
   is landed only when `media.status4k` is AVAILABLE (`test_a_4k_request_lands_on_status4k_not_status`).

---

### Task 1: Fixtures (recorded 2026-09-28, already on disk) — commit them

**Files:**
- Already created: `tests/fixtures/overseerr_requests_page.json`, `tests/fixtures/overseerr_arr_settings.json`,
  `tests/fixtures/radarr_request_tags.json`, `tests/fixtures/sonarr_request_tags.json`
- Modified: `tests/fixtures/README.md` (four table rows)

- [ ] **Step 1: Verify the fixtures are sanitised** (no emails outside `example.test`, no IPs, no real hosts)

Run: `grep -l -E "@(?!example\.test)" tests/fixtures/*request* ; grep -c "host.example.test" tests/fixtures/overseerr_requests_page.json`
Expected: first grep prints nothing; second prints a number ≥ 1.

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/overseerr_requests_page.json tests/fixtures/overseerr_arr_settings.json tests/fixtures/radarr_request_tags.json tests/fixtures/sonarr_request_tags.json tests/fixtures/README.md
git commit -m "test(fixtures): record Seerr request list, arr settings and Radarr/Sonarr requester tags (#127)"
```

---

### Task 2: Seerr client — read requests, users' Plex ids, arr settings, media dates

**Files:**
- Modify: `shortlist/engine/clients/seerr.py` (after `users()` at :237)
- Test: `tests/unit/test_seerr.py` (uses `respx`; `_client()` helper at :60, `BASE` constant)

**Interfaces:**
- Produces on `SeerrClient`:
  - `requests(self) -> list[dict]` — every request (`/request?filter=all`), raw dicts.
  - `user_plex_ids(self) -> dict[int, int | None]` — Seerr user id → `plexId` (None when unlinked).
  - `arr_settings(self) -> dict[str, list[dict]]` — `{"radarr": [...], "sonarr": [...]}` raw server dicts (each carries `name`, `is4k`, `tagRequests`, `isDefault`).
  - `media_dates(self) -> dict[tuple[str, int], dict]` — `(mediaType, tmdbId)` → `{"mediaAddedAt", "lastSeasonChange", "tvdbId", "status", "status4k"}`.
- `_paged` gains `**params` so a filter can travel with the walk.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_seerr.py — append
import json
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class TestRequestReads:
    def test_requests_walks_the_list_with_filter_all(self):
        page = _fixture("overseerr_requests_page.json")
        with respx.mock:
            route = respx.get(f"{BASE}/request").mock(return_value=httpx.Response(200, json=page))
            got = _client().requests()
        assert len(got) == page["pageInfo"]["results"] == 7
        assert route.calls[0].request.url.params["filter"] == "all"
        assert got[0]["requestedBy"]["plexId"] == page["results"][0]["requestedBy"]["plexId"]

    def test_user_plex_ids_maps_seerr_id_to_plex_id_and_none_when_unlinked(self):
        users = _fixture("overseerr_users_page.json")
        with respx.mock:
            respx.get(f"{BASE}/user").mock(return_value=httpx.Response(200, json=users))
            got = _client().user_plex_ids()
        linked = {u["id"]: u["plexId"] for u in users["results"]}
        assert got == linked
        assert None in got.values()  # the fixture's local (non-Plex) account

    def test_arr_settings_returns_both_lists_with_tag_requests(self):
        st = _fixture("overseerr_arr_settings.json")
        with respx.mock:
            respx.get(f"{BASE}/settings/radarr").mock(return_value=httpx.Response(200, json=st["radarr"]))
            respx.get(f"{BASE}/settings/sonarr").mock(return_value=httpx.Response(200, json=st["sonarr"]))
            got = _client().arr_settings()
        assert [s["tagRequests"] for s in got["radarr"]] == [True]
        assert [s["tagRequests"] for s in got["sonarr"]] == [True]

    def test_media_dates_keys_by_media_type_and_tmdb_id(self):
        page = _fixture("overseerr_media_page.json")
        with respx.mock:
            respx.get(f"{BASE}/media").mock(return_value=httpx.Response(200, json=page))
            got = _client().media_dates()
        row = page["results"][0]
        assert got[(row["mediaType"], row["tmdbId"])]["mediaAddedAt"] == row.get("mediaAddedAt")
        assert set(got[(row["mediaType"], row["tmdbId"])]) == {
            "mediaAddedAt",
            "lastSeasonChange",
            "tvdbId",
            "status",
            "status4k",
        }

    def test_a_request_read_failure_raises_seerr_error(self):
        with respx.mock:
            respx.get(f"{BASE}/request").mock(return_value=httpx.Response(500, text="boom"))
            with pytest.raises(SeerrError):
                _client().requests()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_seerr.py -k TestRequestReads -q`
Expected: 5 failures, `AttributeError: 'SeerrClient' object has no attribute 'requests'` (and friends).

- [ ] **Step 3: Implement**

In `_paged`, add `**params: object` to the signature and pass them through:
`payload = self._get(path, permission=permission, take=self._PAGE_SIZE, skip=len(out), **params)`.

Add after `users()`:

```python
def requests(self) -> list[dict]:
    """Every request on the instance, newest first, as Seerr serialises them.

    ``filter=all`` is explicit: the endpoint's default filter also says "all", but this read
    exists to see DELETED-media and COMPLETED requests alike, so the intent is written down.
    """
    rows = self._paged("/request", filter="all", sort="added")
    return [r for r in rows if isinstance(r, dict)]


def user_plex_ids(self) -> dict[int, int | None]:
    """Seerr user id -> ``plexId`` (None for a local account never linked to Plex).

    The only identity Shortlist trusts: ``plexId`` is the same number as ``users.plex_account_id``
    (fixture ``overseerr_requests_page.json``), so a request maps to a person with no name match.
    """
    out: dict[int, int | None] = {}
    for row in self._paged("/user"):
        if not isinstance(row, dict) or _int_or_none(row.get("id")) is None:
            continue
        out[int(row["id"])] = _int_or_none(row.get("plexId"))
    return out


def arr_settings(self) -> dict[str, list[dict]]:
    """The Radarr and Sonarr servers Seerr sends to, with each one's ``tagRequests`` switch.

    ``tagRequests`` is absent from the published schema but real (fixture
    ``overseerr_arr_settings.json``); it is what stamps ``<userId>-<name>`` on each item sent.
    """
    out: dict[str, list[dict]] = {}
    for kind in ("radarr", "sonarr"):
        payload = self._get(f"/settings/{kind}")
        out[kind] = [s for s in payload if isinstance(s, dict)] if isinstance(payload, list) else []
    return out


def media_dates(self) -> dict[tuple[str, int], dict]:
    """``(mediaType, tmdbId)`` -> the dates a requests row orders by, for every media row Seerr holds.

    Read when a tagged title's request is gone: Seerr keeps the media row (and ``mediaAddedAt``)
    after a request is deleted, which is what lets an owner who tidies their queue still get
    arrival order.
    """
    out: dict[tuple[str, int], dict] = {}
    for row in self._paged("/media"):
        if not isinstance(row, dict):
            continue
        kind, tmdb_id = _media_type_of(row), _int_or_none(row.get("tmdbId"))
        if kind is None or tmdb_id is None:
            continue
        out[(kind, tmdb_id)] = {
            "mediaAddedAt": row.get("mediaAddedAt"),
            "lastSeasonChange": row.get("lastSeasonChange"),
            "tvdbId": _int_or_none(row.get("tvdbId")),
            "status": _int_or_none(row.get("status")),
            "status4k": _int_or_none(row.get("status4k")),
        }
    return out
```

Check `_media_type_of` (seerr.py:491) returns `"movie"`/`"tv"` strings; if it returns `MediaType`, key on `row.get("mediaType")` instead so the key matches `request["type"]`/`media["mediaType"]` literally.

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/test_seerr.py -q`
Expected: all pass (existing tests included — `_paged`'s new kwargs must not change existing calls).

- [ ] **Step 5: Commit**

```bash
git add shortlist/engine/clients/seerr.py tests/unit/test_seerr.py
git commit -m "feat(seerr): read requests, users' Plex ids, arr settings and media dates (#127)"
```

---

### Task 3: Arr clients — read tags and the tagged library

**Files:**
- Modify: `shortlist/engine/clients/arr.py` (`_ArrClient` :60, `RadarrClient` :251, `SonarrClient` :316)
- Test: `tests/unit/test_arr.py` (look at its top for the existing respx client helper and base URL; reuse them)

**Interfaces:**
- Produces on `_ArrClient`: `tags(self) -> dict[int, str]` — tag id → label, read-only (never creates).
- Produces on `RadarrClient`: `movies(self) -> list[dict]` — raw `/api/v3/movie`.
- Produces on `SonarrClient`: `series(self) -> list[dict]` — raw `/api/v3/series`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_arr.py — append (adapt `_radarr()`/`_sonarr()`/BASE names to the file's helpers)
import json
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures"


class TestTagReads:
    def test_tags_is_a_read_only_id_to_label_map(self):
        fx = json.loads((FIX / "radarr_request_tags.json").read_text())
        with respx.mock:
            get = respx.get(f"{RADARR}/api/v3/tag").mock(return_value=httpx.Response(200, json=fx["tags"]))
            post = respx.post(f"{RADARR}/api/v3/tag")
            got = _radarr().tags()
        assert got == {t["id"]: t["label"] for t in fx["tags"]}
        assert get.called and not post.called

    def test_movies_returns_the_raw_items_with_tags_and_file_state(self):
        fx = json.loads((FIX / "radarr_request_tags.json").read_text())
        with respx.mock:
            respx.get(f"{RADARR}/api/v3/movie").mock(return_value=httpx.Response(200, json=fx["tagged_items"]))
            got = _radarr().movies()
        assert [m["tmdbId"] for m in got] == [m["tmdbId"] for m in fx["tagged_items"]]
        assert "hasFile" in got[0] and "tags" in got[0]

    def test_series_returns_the_raw_items(self):
        fx = json.loads((FIX / "sonarr_request_tags.json").read_text())
        with respx.mock:
            respx.get(f"{SONARR}/api/v3/series").mock(return_value=httpx.Response(200, json=fx["tagged_items"]))
            got = _sonarr().series()
        assert [s["tvdbId"] for s in got] == [s["tvdbId"] for s in fx["tagged_items"]]
        assert "statistics" in got[0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/test_arr.py -k TestTagReads -q`
Expected: 3 failures, `AttributeError`.

- [ ] **Step 3: Implement**

On `_ArrClient`:

```python
    def tags(self) -> dict[int, str]:
        """Every tag the app has, id -> label. A READ: `_resolve_tag` is the one that may create."""
        payload = self._get("/api/v3/tag")
        return {
            int(t["id"]): str(t["label"])
            for t in (payload if isinstance(payload, list) else [])
            if isinstance(t, dict) and t.get("id") is not None and t.get("label")
        }
```

On `RadarrClient`: `def movies(self) -> list[dict]: payload = self._get("/api/v3/movie"); return [m for m in payload if isinstance(m, dict)] if isinstance(payload, list) else []` (with a one-line docstring: "Every movie Radarr tracks, raw — `tags`, `hasFile`, `movieFile.dateAdded` are what a requests row reads."). Same on `SonarrClient` as `series()` for `/api/v3/series` ("`tags`, `tmdbId`, `added`, `statistics.episodeFileCount`").

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/test_arr.py -q` — Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add shortlist/engine/clients/arr.py tests/unit/test_arr.py
git commit -m "feat(arr): read-only tag and library listings for the requests row (#127)"
```

---

### Task 4: Engine models — the new fields

**Files:**
- Modify: `shortlist/engine/models.py` (`UserProfile` :316, `RowSpec` :397 — append fields at the END, `RequestConfig` region ~:737-770 for the new dataclass, `EngineConfig` ~:1261)
- Modify: `shortlist/engine/context.py` (`EngineContext` :33 — add one optional field)
- Test: `tests/unit/test_models.py` (create if absent)

**Interfaces:**
- `RowSpec.requests_row: bool = False`, `RowSpec.requests_window_days: int = 90`, `RowSpec.requests_tag_pattern: str = ""` — appended after `season` (RowSpec is built positionally in places; append only).
- `UserProfile.requested_by_tag: str = ""` — appended last.
- New frozen dataclass `RequestSources(overseerr: SeerrTarget | None = None, radarr: ArrTarget | None = None, sonarr: ArrTarget | None = None, exclude_seerr_user_id: int = 0, shortlist_tag: str = "")` with `def any(self) -> bool`.
- `EngineConfig.request_sources: RequestSources | None = None` (append last).
- `EngineContext.request_ledger: "RequestLedger | None" = None` (string annotation; `RequestLedger` is defined in Task 5's module — import under `TYPE_CHECKING` to avoid a cycle).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_models.py
from shortlist.engine.models import EngineConfig, RequestSources, RowSpec, SeerrTarget, UserProfile, UserType


def test_a_row_is_not_a_requests_row_by_default():
    spec = RowSpec(slug="r", name_template="n", size=5)
    assert (spec.requests_row, spec.requests_window_days, spec.requests_tag_pattern) == (False, 90, "")


def test_request_sources_any_is_true_only_with_a_target():
    assert RequestSources().any() is False
    assert RequestSources(overseerr=SeerrTarget(url="http://s", api_key="k")).any() is True


def test_config_and_profile_carry_the_new_fields_with_safe_defaults():
    assert EngineConfig().request_sources is None
    assert UserProfile(username="u", plex_account_id=1, user_type=UserType.SHARED).requested_by_tag == ""
```

- [ ] **Step 2: Run to verify it fails** — `pytest tests/unit/test_models.py -q` → ImportError / TypeError.

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class RequestSources:
    """Where a "Your requests" row reads who asked for what. Independent of the request FEATURE:
    the owner may send nothing through Shortlist and still want this row, so this is built whenever
    a URL + key exist, whatever `requests.enabled` / `requests.target` say."""

    overseerr: SeerrTarget | None = None
    radarr: ArrTarget | None = None
    sonarr: ArrTarget | None = None
    # Requests Shortlist itself files via Overseerr land on this account; they are recommendations,
    # not something the person asked for, so that account never gets a row from them.
    exclude_seerr_user_id: int = 0
    # Shortlist's own Radarr/Sonarr tag: an item carrying it never matches an own-pattern tag.
    shortlist_tag: str = ""

    def any(self) -> bool:
        return bool(self.overseerr or self.radarr or self.sonarr)
```

RowSpec (append after `season`):
```python
    # A "Your requests" row (issue #127): built from the person's Overseerr requests and Radarr/Sonarr
    # requester tags, never from the candidate pool. `requests_window_days` 0 = keep until watched.
    requests_row: bool = False
    requests_window_days: int = 90
    # Owner's own tag format for hand-managed Radarr/Sonarr setups, e.g. "req-{username}". "" = off.
    requests_tag_pattern: str = ""
```
UserProfile (append): `requested_by_tag: str = ""` with comment "A Radarr/Sonarr tag that marks THIS person's requests when it fits no pattern (set on their Users page)."
EngineConfig (append): `request_sources: RequestSources | None = None`.
EngineContext: under `if TYPE_CHECKING: from shortlist.engine.requests_row import RequestLedger`, add field `request_ledger: RequestLedger | None = None` with comment "Built once per run by the pipeline when any row is a requests row; None otherwise."

- [ ] **Step 4: Run** — `pytest tests/unit/test_models.py -q` → pass.
- [ ] **Step 5: Commit** — `git add shortlist/engine/models.py shortlist/engine/context.py tests/unit/test_models.py && git commit -m "feat(engine): model fields for the requests row (#127)"`

---

### Task 5: `requests_row.collect_requests` — who asked for what

**Files:**
- Create: `shortlist/engine/requests_row.py`
- Test: `tests/unit/test_requests_row.py`

**Interfaces:**
- `REQUESTER_TAG: re.Pattern` — `^(\d+)\s?-\s?\S`.
- `parse_requester_tag(label: str) -> int | None` — the Seerr user id in an Overseerr-format tag.
- `pattern_matches(label: str, pattern: str, people: list[UserProfile]) -> list[UserProfile]` — every person whose `{username}`/`{name}` rendering of `pattern` equals the label (case-insensitive; spaces↔dashes, the Arr charset).
- `@dataclass(frozen=True) RequestedTitle(tmdb_id: int, media_type: MediaType, plex_account_id: int, requested_at: datetime | None, landed_at: datetime | None, on_disk: bool, seasons_landed: bool, found_in: tuple[str, ...], title: str = "")`.
- `@dataclass(frozen=True) TagMatch(label: str, source: str, plex_account_id: int | None, titles: int, ambiguous: bool)` (`source` is "overseerr" or "pattern").
- `@dataclass RequestLedger(titles: list[RequestedTitle], complete: bool, problems: list[str], tag_matches: list[TagMatch], seerr_requests: int = 0, seerr_requesters: int = 0, seerr_linked: int = 0, seerr_servers: list[dict] = ...)` with `for_person(self, plex_account_id: int) -> list[RequestedTitle]`.
- `collect_requests(sources: RequestSources, people: list[UserProfile], *, seerr: SeerrClient | None = None, radarr: RadarrClient | None = None, sonarr: SonarrClient | None = None) -> RequestLedger` — clients are injectable for tests; built from `sources` when None.
- Seerr enums as module constants: `_REQ_APPROVED = 2`, `_REQ_COMPLETED = 5`, `_MEDIA_AVAILABLE = 5`, `_MEDIA_PARTIAL = 4`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_requests_row.py
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.seerr import SeerrError
from shortlist.engine.models import ArrTarget, MediaType, RequestSources, SeerrTarget, UserProfile, UserType
from shortlist.engine.requests_row import (
    collect_requests,
    parse_requester_tag,
    pattern_matches,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures"
REQS = json.loads((FIX / "overseerr_requests_page.json").read_text())
RADARR = json.loads((FIX / "radarr_request_tags.json").read_text())
SONARR = json.loads((FIX / "sonarr_request_tags.json").read_text())
SEERR = SeerrTarget(url="http://seerr", api_key="k")
ARR = ArrTarget(url="http://arr", api_key="k", quality_profile_id=0, root_folder="", tag="shortlist")


def _person(n: int, **kw) -> UserProfile:
    return UserProfile(
        username=f"person{n}", plex_account_id=100000 + n, user_type=UserType.SHARED, slug=f"person{n}", **kw
    )


def _seerr(requests=None, users=None, media=None) -> MagicMock:
    c = MagicMock()
    c.requests.return_value = REQS["results"] if requests is None else requests
    c.user_plex_ids.return_value = (
        users if users is not None else {r["requestedBy"]["id"]: r["requestedBy"]["plexId"] for r in REQS["results"]}
    )
    c.arr_settings.return_value = {"radarr": [{"name": "r", "is4k": False, "tagRequests": True}], "sonarr": []}
    c.media_dates.return_value = media or {}
    return c


def _radarr(items=None, tags=None) -> MagicMock:
    c = MagicMock()
    c.tags.return_value = {t["id"]: t["label"] for t in (tags or RADARR["tags"])}
    c.movies.return_value = RADARR["tagged_items"] if items is None else items
    return c


def _sonarr(items=None) -> MagicMock:
    c = MagicMock()
    c.tags.return_value = {t["id"]: t["label"] for t in SONARR["tags"]}
    c.series.return_value = SONARR["tagged_items"] if items is None else items
    return c


class TestTagParsing:
    @pytest.mark.parametrize(
        "label,expected", [("12-sarah", 12), ("12 - sarah", 12), ("12-", None), ("sarah", None), ("requested", None)]
    )
    def test_parse_requester_tag(self, label, expected):
        assert parse_requester_tag(label) == expected

    def test_pattern_matches_username_and_name_case_and_dash_insensitively(self):
        sarah = _person(1, nickname="Sarah Jones")
        assert pattern_matches("req-person1", "req-{username}", [sarah]) == [sarah]
        assert pattern_matches("REQ-PERSON1", "req-{username}", [sarah]) == [sarah]
        assert pattern_matches("sarah-jones", "{name}", [sarah]) == [sarah]
        assert pattern_matches("req-nobody", "req-{username}", [sarah]) == []

    def test_a_pattern_without_a_placeholder_matches_nobody(self):
        assert pattern_matches("req-person1", "req-person1", [_person(1)]) == []


class TestCollectFromSeerr:
    def test_requests_map_to_people_by_plex_id_only(self):
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(RequestSources(overseerr=SEERR), people, seerr=_seerr())
        assert ledger.complete and ledger.problems == []
        by_person = {p.plex_account_id: len(ledger.for_person(p.plex_account_id)) for p in people}
        assert sum(by_person.values()) == 7
        assert ledger.seerr_requests == 7 and ledger.seerr_linked == 6

    def test_a_requester_with_no_plex_id_gets_no_titles(self):
        req = dict(REQS["results"][0])
        req["requestedBy"] = {**req["requestedBy"], "plexId": None}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR),
            [_person(10)],
            seerr=_seerr(requests=[req], users={req["requestedBy"]["id"]: None}),
        )
        assert ledger.titles == [] and ledger.complete

    def test_pending_and_declined_requests_are_ignored(self):
        rows = [dict(REQS["results"][0], status=1), dict(REQS["results"][1], status=3)]
        ledger = collect_requests(
            RequestSources(overseerr=SEERR), [_person(n) for n in range(10, 18)], seerr=_seerr(requests=rows)
        )
        assert ledger.titles == []

    def test_a_tv_request_is_landed_only_when_the_request_is_completed(self):
        tv = next(r for r in REQS["results"] if r["type"] == "tv")
        waiting = dict(tv, status=2, seasons=[dict(tv["seasons"][0], status=2)])
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(RequestSources(overseerr=SEERR), people, seerr=_seerr(requests=[tv, waiting]))
        landed = [t.seasons_landed for t in ledger.titles]
        assert landed == [True, False]

    def test_the_request_as_account_is_left_out(self):
        req = REQS["results"][0]
        src = RequestSources(overseerr=SEERR, exclude_seerr_user_id=req["requestedBy"]["id"])
        ledger = collect_requests(src, [_person(n) for n in range(10, 18)], seerr=_seerr(requests=[req]))
        assert ledger.titles == []

    def test_a_4k_request_lands_on_status4k_not_status(self):
        req = dict(REQS["results"][0], is4k=True, status=2)
        req["media"] = {**req["media"], "status": 5, "status4k": 3}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR), [_person(n) for n in range(10, 18)], seerr=_seerr(requests=[req])
        )
        assert [t.on_disk for t in ledger.titles] == [False]

    def test_two_seerr_accounts_with_one_plex_id_both_belong_to_that_person(self):
        a, b = REQS["results"][0], dict(REQS["results"][1])
        b["requestedBy"] = {**b["requestedBy"], "id": 999, "plexId": a["requestedBy"]["plexId"]}
        users = {a["requestedBy"]["id"]: a["requestedBy"]["plexId"], 999: a["requestedBy"]["plexId"]}
        person = UserProfile(username="x", plex_account_id=a["requestedBy"]["plexId"], user_type=UserType.SHARED)
        ledger = collect_requests(RequestSources(overseerr=SEERR), [person], seerr=_seerr(requests=[a, b], users=users))
        assert len(ledger.for_person(person.plex_account_id)) == 2

    def test_zero_users_and_zero_requests_reads_as_a_failed_read(self):
        ledger = collect_requests(RequestSources(overseerr=SEERR), [_person(10)], seerr=_seerr(requests=[], users={}))
        assert ledger.complete is False and any("Overseerr" in p for p in ledger.problems)

    def test_a_seerr_error_marks_the_ledger_incomplete_and_keeps_going(self):
        c = _seerr()
        c.requests.side_effect = SeerrError("down")
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), [_person(n) for n in range(10, 18)], seerr=c, radarr=_radarr()
        )
        assert ledger.complete is False
        assert any(t.found_in == ("tag",) for t in ledger.titles)  # Radarr still read


class TestCollectFromTags:
    def test_overseerr_tags_resolve_through_seerr_users(self):
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), people, seerr=_seerr(requests=[]), radarr=_radarr()
        )
        tagged = [t for t in ledger.titles if "tag" in t.found_in]
        assert len(tagged) == len(RADARR["tagged_items"])
        assert all(t.plex_account_id in {p.plex_account_id for p in people} for t in tagged)

    def test_overseerr_tags_without_overseerr_connected_are_reported_not_guessed(self):
        ledger = collect_requests(RequestSources(radarr=ARR), [_person(10)], radarr=_radarr())
        assert ledger.titles == []
        assert any("Overseerr" in p and "tag" in p.lower() for p in ledger.problems)
        assert ledger.complete  # a read that WORKED, with nothing we may use

    def test_radarr_on_disk_follows_has_file_and_lands_on_the_file_date(self):
        item = dict(RADARR["tagged_items"][0], hasFile=True, movieFile={"dateAdded": "2026-09-16T03:00:00Z"})
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), people, seerr=_seerr(requests=[]), radarr=_radarr(items=[item])
        )
        (t,) = ledger.titles
        assert t.on_disk and t.landed_at == datetime(2026, 9, 16, 3, tzinfo=UTC)

    def test_sonarr_on_disk_needs_an_episode_file(self):
        item = dict(SONARR["tagged_items"][0])
        item["statistics"] = {**item["statistics"], "episodeFileCount": 0}
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, sonarr=ARR), people, seerr=_seerr(requests=[]), sonarr=_sonarr(items=[item])
        )
        assert [t.on_disk for t in ledger.titles] == [False]

    def test_a_series_without_tmdb_id_is_skipped_and_reported(self):
        item = dict(SONARR["tagged_items"][0])
        item.pop("tmdbId", None)
        people = [_person(n) for n in range(10, 18)]
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, sonarr=ARR), people, seerr=_seerr(requests=[]), sonarr=_sonarr(items=[item])
        )
        assert ledger.titles == [] and any("TMDB" in p for p in ledger.problems)

    def test_own_pattern_tags_match_people_and_skip_shortlists_own_items(self):
        sarah = _person(1)
        tags = [{"id": 1, "label": "req-person1"}, {"id": 2, "label": "shortlist"}]
        asked = {
            "tmdbId": 501,
            "tags": [1],
            "hasFile": True,
            "movieFile": {"dateAdded": "2026-09-01T00:00:00Z"},
            "title": "A",
        }
        ours = {
            "tmdbId": 502,
            "tags": [1, 2],
            "hasFile": True,
            "movieFile": {"dateAdded": "2026-09-01T00:00:00Z"},
            "title": "B",
        }
        src = RequestSources(radarr=ARR, shortlist_tag="shortlist")
        ledger = collect_requests(src, [sarah], radarr=_radarr(items=[asked, ours], tags=tags))
        assert [t.tmdb_id for t in ledger.for_person(sarah.plex_account_id)] == [501]

    def test_an_ambiguous_pattern_tag_is_ignored_and_listed(self):
        a = _person(1, nickname="Sam")
        b = _person(2, nickname="Sam")
        tags = [{"id": 1, "label": "sam"}]
        item = {"tmdbId": 501, "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(RequestSources(radarr=ARR), [a, b], radarr=_radarr(items=[item], tags=tags))
        assert ledger.titles == []
        assert [(m.label, m.ambiguous) for m in ledger.tag_matches] == [("sam", True)]

    def test_a_per_person_override_tag_wins(self):
        kids = _person(3, requested_by_tag="children")
        tags = [{"id": 7, "label": "children"}]
        item = {"tmdbId": 501, "tags": [7], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(RequestSources(radarr=ARR), [kids], radarr=_radarr(items=[item], tags=tags))
        assert [t.plex_account_id for t in ledger.titles] == [kids.plex_account_id]

    def test_a_title_in_seerr_and_tagged_counts_once_and_keeps_seerr_dates(self):
        req = REQS["results"][0]
        people = [_person(n) for n in range(10, 18)]
        tag_label = next(t["label"] for t in RADARR["tags"] if t["label"].startswith(f"{req['requestedBy']['id']}-"))
        tag_id = next(t["id"] for t in RADARR["tags"] if t["label"] == tag_label)
        item = {
            "tmdbId": req["media"]["tmdbId"],
            "tags": [tag_id],
            "hasFile": True,
            "movieFile": {"dateAdded": "2020-01-01T00:00:00Z"},
            "title": "A",
        }
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            people,
            seerr=_seerr(requests=[req]),
            radarr=_radarr(items=[item]),
        )
        (t,) = [t for t in ledger.titles if t.tmdb_id == req["media"]["tmdbId"]]
        assert t.found_in == ("overseerr", "tag")
        assert t.requested_at == datetime.fromisoformat(req["createdAt"].replace("Z", "+00:00"))

    def test_a_tagged_title_whose_request_is_gone_dates_from_seerr_media(self):
        people = [_person(n) for n in range(10, 18)]
        item = dict(RADARR["tagged_items"][0], hasFile=True, movieFile={"dateAdded": "2026-09-16T03:00:00Z"})
        media = {
            ("movie", item["tmdbId"]): {
                "mediaAddedAt": "2026-09-15T00:00:00.000Z",
                "lastSeasonChange": None,
                "tvdbId": None,
                "status": 5,
                "status4k": 1,
            }
        }
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            people,
            seerr=_seerr(requests=[], media=media),
            radarr=_radarr(items=[item]),
        )
        (t,) = ledger.titles
        assert t.landed_at == datetime(2026, 9, 15, tzinfo=UTC)
```

(Whether the fixture's first Radarr requester tag belongs to a person in `range(10, 18)` depends on the recorded remap; if a test's expected person is not in that range, widen the range — the fixture's tag numbers are 10-17.)

- [ ] **Step 2: Run to verify they fail** — `pytest tests/unit/test_requests_row.py -q` → ImportError.

- [ ] **Step 3: Implement `shortlist/engine/requests_row.py`**

```python
"""Who asked for what: the read behind a "Your requests" row (issue #127).

Two sources, merged so a title counts once per person. Overseerr's request list covers requests that
are still there; Radarr/Sonarr requester tags (``<seerrUserId>-<username>``, written by Seerr's
"Tag Requests" and never removed when the request is deleted — fixture ``radarr_request_tags.json``)
cover the ones an owner tidied away. A person is only ever identified by Plex account ID: an
Overseerr tag resolves through Overseerr's user list, an own-pattern tag through the roster, and a
tag that fits nobody or two people is reported, never guessed — a wrong guess puts one person's
requests in another person's private row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from loguru import logger

from shortlist.engine.clients.arr import RadarrClient, SonarrClient
from shortlist.engine.clients.seerr import SeerrClient
from shortlist.engine.models import MediaType, RequestSources, UserProfile

REQUESTER_TAG = re.compile(r"^(\d+)\s?-\s?\S")
_REQ_APPROVED, _REQ_COMPLETED = 2, 5
_MEDIA_AVAILABLE = 5
_TAG_CHARSET = re.compile(r"[^a-z0-9-]")


@dataclass(frozen=True)
class RequestedTitle:
    tmdb_id: int
    media_type: MediaType
    plex_account_id: int
    requested_at: datetime | None
    landed_at: datetime | None
    on_disk: bool
    seasons_landed: bool
    found_in: tuple[str, ...]
    title: str = ""


@dataclass(frozen=True)
class TagMatch:
    label: str
    source: str  # "overseerr" | "pattern" | "override"
    plex_account_id: int | None
    titles: int
    ambiguous: bool


@dataclass
class RequestLedger:
    titles: list[RequestedTitle]
    complete: bool
    problems: list[str] = field(default_factory=list)
    tag_matches: list[TagMatch] = field(default_factory=list)
    seerr_requests: int = 0
    seerr_requesters: int = 0
    seerr_linked: int = 0
    seerr_servers: list[dict] = field(default_factory=list)

    def for_person(self, plex_account_id: int) -> list[RequestedTitle]:
        return [t for t in self.titles if t.plex_account_id == plex_account_id]


def parse_requester_tag(label: str) -> int | None:
    m = REQUESTER_TAG.match(label.strip())
    return int(m.group(1)) if m else None


def _norm(label: str) -> str:
    return _TAG_CHARSET.sub("", label.strip().lower().replace(" ", "-").replace("_", "-"))


def pattern_matches(label: str, pattern: str, people: list[UserProfile]) -> list[UserProfile]:
    if "{username}" not in pattern and "{name}" not in pattern:
        return []
    want = _norm(label)
    out = []
    for p in people:
        rendered = pattern.replace("{username}", p.username).replace("{name}", p.nickname or p.username)
        if _norm(rendered) == want:
            out.append(p)
    return out


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
```

Then `collect_requests`:

```python
def collect_requests(
    sources: RequestSources,
    people: list[UserProfile],
    *,
    seerr: SeerrClient | None = None,
    radarr: RadarrClient | None = None,
    sonarr: SonarrClient | None = None,
    patterns: frozenset[str] = frozenset(),
) -> RequestLedger:
    """Read every configured source once and return the ledger. Never raises: a source that fails
    is recorded in ``problems`` and flips ``complete`` to False, which is what stops any row being
    REMOVED on the strength of a read that did not happen."""
    ledger = RequestLedger(titles=[], complete=True)
    by_plex = {p.plex_account_id: p for p in people}
    seerr_plex: dict[int, int | None] = {}
    media_dates: dict[tuple[str, int], dict] = {}
    seerr_client = seerr or (SeerrClient(sources.overseerr) if sources.overseerr else None)
    merged: dict[tuple[MediaType, int, int], RequestedTitle] = {}

    def add(t: RequestedTitle) -> None:
        key = (t.media_type, t.tmdb_id, t.plex_account_id)
        prev = merged.get(key)
        if prev is None:
            merged[key] = t
        elif "overseerr" in prev.found_in:
            merged[key] = RequestedTitle(
                **{
                    **prev.__dict__,
                    "found_in": tuple(dict.fromkeys(prev.found_in + t.found_in)),
                    "title": prev.title or t.title,
                }
            )
        else:
            merged[key] = RequestedTitle(
                **{
                    **t.__dict__,
                    "found_in": tuple(dict.fromkeys(t.found_in + prev.found_in)),
                    "title": t.title or prev.title,
                }
            )

    if seerr_client is not None:
        try:
            seerr_plex = seerr_client.user_plex_ids()
            rows = seerr_client.requests()
            ledger.seerr_servers = [
                {
                    "kind": kind,
                    "name": s.get("name", ""),
                    "is4k": bool(s.get("is4k")),
                    "tag_requests": bool(s.get("tagRequests")),
                }
                for kind, servers in seerr_client.arr_settings().items()
                for s in servers
            ]
            if not rows and not seerr_plex:
                ledger.complete = False
                ledger.problems.append("Overseerr answered with no users and no requests — treated as a failed read")
            ledger.seerr_requests = len(rows)
            requesters = {r.get("requestedBy", {}).get("id") for r in rows if isinstance(r.get("requestedBy"), dict)}
            ledger.seerr_requesters = len(requesters)
            ledger.seerr_linked = len({u for u in requesters if seerr_plex.get(u) in by_plex})
            for r in rows:
                t = _from_seerr_request(r, seerr_plex, by_plex, sources.exclude_seerr_user_id)
                if t is not None:
                    add(t)
        except Exception as e:  # noqa: BLE001 — any source failure must not stop the run, only removals
            ledger.complete = False
            ledger.problems.append(f"Overseerr could not be read: {e}")
            logger.warning("requests row: Overseerr read failed ({})", e)

    for kind, client, media in (
        (MediaType.MOVIE, radarr or (RadarrClient(sources.radarr) if sources.radarr else None), "movie"),
        (MediaType.SHOW, sonarr or (SonarrClient(sources.sonarr) if sources.sonarr else None), "tv"),
    ):
        if client is None:
            continue
        try:
            tags = client.tags()
            items = client.movies() if kind is MediaType.MOVIE else client.series()
        except Exception as e:  # noqa: BLE001
            ledger.complete = False
            ledger.problems.append(f"{client.app_name} could not be read: {e}")
            continue
        _add_tagged(
            ledger,
            kind,
            media,
            items,
            tags,
            people,
            by_plex,
            seerr_plex,
            seerr_client is not None,
            sources,
            patterns,
            add,
        )

    # Tagged titles whose request is gone take their arrival date from Seerr's media table.
    undated = [k for k, t in merged.items() if t.landed_at is None and "overseerr" not in t.found_in]
    if undated and seerr_client is not None:
        try:
            media_dates = seerr_client.media_dates()
        except Exception as e:  # noqa: BLE001
            ledger.problems.append(f"Overseerr media dates could not be read: {e}")
        for key in undated:
            t = merged[key]
            rec = media_dates.get(("movie" if t.media_type is MediaType.MOVIE else "tv", t.tmdb_id))
            landed = _iso(rec.get("lastSeasonChange") if t.media_type is MediaType.SHOW else None) if rec else None
            landed = landed or (_iso(rec.get("mediaAddedAt")) if rec else None)
            if landed:
                merged[key] = RequestedTitle(**{**t.__dict__, "landed_at": landed})

    ledger.titles = sorted(
        merged.values(),
        key=lambda t: t.landed_at or datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo),
        reverse=True,
    )
    return ledger
```

Helpers `_from_seerr_request` and `_add_tagged`:

```python
def _from_seerr_request(
    r: dict, seerr_plex: dict[int, int | None], by_plex: dict, exclude_uid: int
) -> RequestedTitle | None:
    who = r.get("requestedBy") if isinstance(r.get("requestedBy"), dict) else {}
    uid = who.get("id")
    if uid is None or (exclude_uid and uid == exclude_uid):
        return None
    if r.get("status") not in (_REQ_APPROVED, _REQ_COMPLETED):
        return None
    plex_id = who.get("plexId") or seerr_plex.get(uid)
    if plex_id not in by_plex:
        return None
    media = r.get("media") if isinstance(r.get("media"), dict) else {}
    tmdb_id = media.get("tmdbId")
    kind = MediaType.MOVIE if r.get("type") == "movie" else MediaType.SHOW if r.get("type") == "tv" else None
    if not isinstance(tmdb_id, int) or kind is None:
        return None
    status = media.get("status4k" if r.get("is4k") else "status")
    on_disk = status == _MEDIA_AVAILABLE or (kind is MediaType.SHOW and r.get("status") == _REQ_COMPLETED)
    seasons = r.get("seasons") if isinstance(r.get("seasons"), list) else []
    seasons_landed = (
        kind is MediaType.MOVIE
        or r.get("status") == _REQ_COMPLETED
        or bool(seasons)
        and all(s.get("status") == _REQ_COMPLETED for s in seasons)
    )
    landed = None
    if kind is MediaType.SHOW:
        done = [_iso(s.get("updatedAt")) for s in seasons if s.get("status") == _REQ_COMPLETED]
        landed = max((d for d in done if d), default=None) or _iso(media.get("lastSeasonChange"))
    landed = landed or _iso(media.get("mediaAddedAt"))
    return RequestedTitle(
        tmdb_id=tmdb_id,
        media_type=kind,
        plex_account_id=int(plex_id),
        requested_at=_iso(r.get("createdAt")),
        landed_at=landed,
        on_disk=bool(on_disk),
        seasons_landed=bool(seasons_landed),
        found_in=("overseerr",),
    )
```

**Design note for the pattern:** the pattern is a per-row setting (`RowSpec.requests_tag_pattern`), but the ledger is per run. Resolve it like this: `collect_requests` takes `patterns: frozenset[str]` (every distinct non-empty pattern across requests rows, passed by the pipeline in Task 7) and records, per title, which pattern matched it; `RequestedTitle` gains `pattern: str = ""` (empty for Overseerr/override matches). `build_requests_picks` (Task 6) then keeps a title only if `t.pattern in ("", spec.requests_tag_pattern)`. Add `patterns: frozenset[str] = frozenset()` as a keyword argument of `collect_requests`, and in the tests above pass `patterns=frozenset({"req-{username}"})` / `frozenset({"{name}"})` for the pattern cases (the override and Overseerr cases need none).

`_add_tagged`, in full:

```python
def _add_tagged(
    ledger, kind, media, items, tags, people, by_plex, seerr_plex, seerr_connected, sources, patterns, add
) -> None:
    shortlist_tag_ids = {
        i for i, label in tags.items() if sources.shortlist_tag and _norm(label) == _norm(sources.shortlist_tag)
    }
    override = {_norm(p.requested_by_tag): p for p in people if p.requested_by_tag}
    counts: dict[tuple[str, str], int] = {}
    owner_of: dict[
        int, tuple[UserProfile | None, str, str, bool]
    ] = {}  # tag id -> (person, source, pattern, ambiguous)
    warned_seerr = False
    for tag_id, label in tags.items():
        uid = parse_requester_tag(label)
        if uid is not None:
            if not seerr_connected:
                if not warned_seerr:
                    ledger.problems.append(
                        "Overseerr requester tags were found in Radarr/Sonarr, but Overseerr isn't connected, so they can't be traced to a person"
                    )
                    warned_seerr = True
                continue
            plex_id = seerr_plex.get(uid)
            owner_of[tag_id] = (by_plex.get(plex_id), "overseerr", "", False)
            continue
        if _norm(label) in override:
            owner_of[tag_id] = (override[_norm(label)], "override", "", False)
            continue
        for pattern in sorted(patterns):
            hits = pattern_matches(label, pattern, people)
            if hits:
                owner_of[tag_id] = (hits[0] if len(hits) == 1 else None, "pattern", pattern, len(hits) > 1)
                break
    for item in items:
        tmdb_id = item.get("tmdbId")
        if not isinstance(tmdb_id, int):
            if any(t in owner_of for t in item.get("tags", [])):
                ledger.problems.append(
                    f"{item.get('title', '?')} carries a requester tag but has no TMDB id, so it can't be matched to Plex"
                )
            continue
        item_tags = [t for t in item.get("tags", []) if t in owner_of]
        ours = bool(shortlist_tag_ids & set(item.get("tags", [])))
        for tag_id in item_tags:
            person, source, pattern, ambiguous = owner_of[tag_id]
            counts[(tags[tag_id], source)] = counts.get((tags[tag_id], source), 0) + 1
            if person is None or (ours and source != "overseerr"):
                continue
            if kind is MediaType.MOVIE:
                on_disk = bool(item.get("hasFile"))
                landed = _iso((item.get("movieFile") or {}).get("dateAdded"))
            else:
                on_disk = int((item.get("statistics") or {}).get("episodeFileCount") or 0) > 0
                landed = _iso(item.get("added"))
            add(
                RequestedTitle(
                    tmdb_id=tmdb_id,
                    media_type=kind,
                    plex_account_id=person.plex_account_id,
                    requested_at=_iso(item.get("added")),
                    landed_at=landed if on_disk else None,
                    on_disk=on_disk,
                    seasons_landed=True,
                    found_in=("tag",),
                    title=str(item.get("title") or ""),
                    pattern=pattern,
                )
            )
    for tag_id, (person, source, _pattern, ambiguous) in owner_of.items():
        ledger.tag_matches.append(
            TagMatch(
                label=tags[tag_id],
                source=source,
                plex_account_id=person.plex_account_id if person else None,
                titles=counts.get((tags[tag_id], source), 0),
                ambiguous=ambiguous,
            )
        )
```

(Replace the sort key's awkward `datetime.min` expression with `datetime(1, 1, 1, tzinfo=UTC)` — import `UTC` from datetime.) Update the `add` merge to carry `pattern` too.

- [ ] **Step 4: Run** — `pytest tests/unit/test_requests_row.py -q` → all pass. Then `ruff check shortlist/engine/requests_row.py --fix && ruff format shortlist/engine/requests_row.py`.

- [ ] **Step 5: Commit** — `git add shortlist/engine/requests_row.py tests/unit/test_requests_row.py && git commit -m "feat(engine): collect who asked for what from Overseerr and arr tags (#127)"`

---

### Task 6: `build_requests_picks` — from ledger to picks

**Files:**
- Modify: `shortlist/engine/requests_row.py`
- Test: `tests/unit/test_requests_row.py`

**Interfaces:**
- `build_requests_picks(policy: RowPolicy, spec: RowSpec, targets: list, k: int, ledger: RequestLedger, *, now: datetime) -> dict[str, list[Pick]]` — `{section.key: picks}`; also appends one `selection` trace entry per section to `policy.report.trace` with `"decision": "requests"` and a `"requests": [...]` list of `{tmdb_id, media_type, title, asked_at, landed_at, found_in, result}` where `result` ∈ `in_row | not_on_plex | season_not_landed | watched | too_old | hidden | over_size`.
- Titles/years come from `policy.ctx.plex.fetch_items(keys)` (one call per section); a key Plex no longer holds is `not_on_plex`.
- Uses `policy.watched_movies: set[int]`, `policy.watched_shows: dict[int, tuple[int, int | None]]`, `policy.visible(keys)`, `policy.ctx.section_index`.
- Import `RowPolicy` under `TYPE_CHECKING` only (rows.py imports this module; avoid the cycle).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_requests_row.py — append
from datetime import timedelta
from shortlist.engine.requests_row import RequestLedger, RequestedTitle, build_requests_picks
from shortlist.engine.models import RowSpec


def _title(tmdb_id, kind=MediaType.MOVIE, *, days_ago=1, person=100001, **kw):
    landed = datetime(2026, 9, 28, tzinfo=UTC) - timedelta(days=days_ago)
    base = dict(
        tmdb_id=tmdb_id,
        media_type=kind,
        plex_account_id=person,
        requested_at=landed - timedelta(days=2),
        landed_at=landed,
        on_disk=True,
        seasons_landed=True,
        found_in=("overseerr",),
    )
    return RequestedTitle(**{**base, **kw})


def _policy(section_index, *, watched_movies=(), watched_shows=None, visible=None):
    policy = MagicMock()
    policy.user = _person(1)
    policy.ctx.section_index = section_index
    policy.ctx.plex.fetch_items.side_effect = lambda keys: (
        [MagicMock(ratingKey=k, title=f"t{k}", year=2020) for k in keys],
        [],
    )
    policy.watched_movies = set(watched_movies)
    policy.watched_shows = watched_shows or {}
    policy.visible.side_effect = lambda keys: visible if visible is not None else None
    policy.report.trace = {}
    return policy


def _section(key="1", kind="movie"):
    s = MagicMock()
    s.key = key
    s.type = kind
    s.title = "Movies" if kind == "movie" else "TV Shows"
    return s


NOW = datetime(2026, 9, 28, tzinfo=UTC)
SPEC = RowSpec(slug="asked", name_template="📬 {library_name} you asked for", size=20, requests_row=True)


class TestBuildRequestsPicks:
    def test_newest_landed_first_and_only_titles_on_plex(self):
        ledger = RequestLedger(
            titles=[_title(1, days_ago=5), _title(2, days_ago=1), _title(3, days_ago=2)], complete=True
        )
        policy = _policy({"1": {1: 11, 2: 22}})
        picks = build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2, 1]
        assert [p.rank for p in picks["1"]] == [1, 2]
        results = {r["tmdb_id"]: r["result"] for r in policy.report.trace["selection"][0]["requests"]}
        assert results == {2: "in_row", 1: "in_row", 3: "not_on_plex"}

    def test_a_watched_movie_and_a_finished_show_drop_but_an_unfinished_show_stays(self):
        ledger = RequestLedger(titles=[_title(1), _title(2, MediaType.SHOW), _title(3, MediaType.SHOW)], complete=True)
        policy = _policy(
            {"1": {1: 11}, "2": {2: 22, 3: 33}}, watched_movies={1}, watched_shows={2: (10, 10), 3: (4, 10)}
        )
        picks = build_requests_picks(policy, SPEC, [_section(), _section("2", "show")], 20, ledger, now=NOW)
        assert picks["1"] == [] and [p.tmdb_id for p in picks["2"]] == [3]

    def test_a_season_that_has_not_landed_waits(self):
        ledger = RequestLedger(titles=[_title(2, MediaType.SHOW, seasons_landed=False)], complete=True)
        policy = _policy({"2": {2: 22}})
        picks = build_requests_picks(policy, SPEC, [_section("2", "show")], 20, ledger, now=NOW)
        assert picks["2"] == []
        assert policy.report.trace["selection"][0]["requests"][0]["result"] == "season_not_landed"

    def test_the_window_drops_old_arrivals_and_zero_keeps_them(self):
        ledger = RequestLedger(titles=[_title(1, days_ago=91), _title(2, days_ago=89)], complete=True)
        policy = _policy({"1": {1: 11, 2: 22}})
        assert [p.tmdb_id for p in build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)["1"]] == [2]
        forever = RowSpec(slug="asked", name_template="n", size=20, requests_row=True, requests_window_days=0)
        assert [
            p.tmdb_id
            for p in build_requests_picks(_policy({"1": {1: 11, 2: 22}}), forever, [_section()], 20, ledger, now=NOW)[
                "1"
            ]
        ] == [2, 1]

    def test_an_undated_title_is_kept_and_sorted_last(self):
        ledger = RequestLedger(titles=[_title(1, landed_at=None), _title(2, days_ago=1)], complete=True)
        picks = build_requests_picks(_policy({"1": {1: 11, 2: 22}}), SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2, 1]

    def test_hidden_titles_are_dropped_when_visibility_is_known(self):
        ledger = RequestLedger(titles=[_title(1), _title(2)], complete=True)
        picks = build_requests_picks(
            _policy({"1": {1: 11, 2: 22}}, visible={22}), SPEC, [_section()], 20, ledger, now=NOW
        )
        assert [p.tmdb_id for p in picks["1"]] == [2]

    def test_the_row_size_caps_and_marks_the_rest(self):
        ledger = RequestLedger(titles=[_title(i, days_ago=i) for i in range(1, 5)], complete=True)
        policy = _policy({"1": {i: i * 11 for i in range(1, 5)}})
        picks = build_requests_picks(policy, SPEC, [_section()], 2, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [1, 2]
        assert [r["result"] for r in policy.report.trace["selection"][0]["requests"]][2:] == ["over_size", "over_size"]

    def test_only_this_persons_titles_and_this_rows_pattern(self):
        ledger = RequestLedger(
            titles=[
                _title(1, person=999),
                _title(2, found_in=("tag",), pattern="other-{username}"),
                _title(3, found_in=("tag",), pattern="req-{username}"),
            ],
            complete=True,
        )
        spec = RowSpec(
            slug="asked", name_template="n", size=20, requests_row=True, requests_tag_pattern="req-{username}"
        )
        picks = build_requests_picks(_policy({"1": {1: 11, 2: 22, 3: 33}}), spec, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [3]

    def test_a_pick_says_when_they_asked(self):
        ledger = RequestLedger(titles=[_title(1)], complete=True)
        (pick,) = build_requests_picks(_policy({"1": {1: 11}}), SPEC, [_section()], 20, ledger, now=NOW)["1"]
        assert pick.reason == "You asked for this on 25 Sep" and pick.sources == ["requests"] and pick.title == "t11"
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/unit/test_requests_row.py -k TestBuildRequestsPicks -q` → ImportError on `build_requests_picks`.

- [ ] **Step 3: Implement**

```python
def build_requests_picks(
    policy: RowPolicy, spec: RowSpec, targets: list, k: int, ledger: RequestLedger, *, now: datetime
) -> dict[str, list[Pick]]:
    """This person's requested titles that are on Plex, unwatched, visible to them and recent — newest first.

    No pool, no curator, no padding: the row is exactly what they asked for, or nothing. Every title
    the ledger holds for them is written to the trace with the reason it is or isn't in the row.
    """
    ctx, user = policy.ctx, policy.user
    mine = [t for t in ledger.for_person(user.plex_account_id) if t.pattern in ("", spec.requests_tag_pattern)]
    cutoff = now - timedelta(days=spec.requests_window_days) if spec.requests_window_days > 0 else None
    out: dict[str, list[Pick]] = {}
    for section in targets:
        kind = section_kind(section)
        sec_idx = ctx.section_index.get(section.key, {})
        rows: list[dict] = []
        keep: list[tuple[RequestedTitle, int]] = []
        for t in (x for x in mine if x.media_type is kind):
            key = sec_idx.get(t.tmdb_id)
            result = "in_row"
            if key is None or not t.on_disk:
                result = "not_on_plex"
            elif not t.seasons_landed:
                result = "season_not_landed"
            elif _watched(policy, t):
                result = "watched"
            elif cutoff and t.landed_at and t.landed_at < cutoff:
                result = "too_old"
            rows.append(
                {
                    "tmdb_id": t.tmdb_id,
                    "media_type": t.media_type.value,
                    "title": t.title,
                    "asked_at": _stamp(t.requested_at),
                    "landed_at": _stamp(t.landed_at),
                    "found_in": list(t.found_in),
                    "result": result,
                }
            )
            if result == "in_row":
                keep.append((t, key))
        seen = policy.visible([key for _, key in keep])
        if seen is not None:
            for (t, key), row in zip(keep, [r for r in rows if r["result"] == "in_row"], strict=True):
                if key not in seen:
                    row["result"] = "hidden"
            keep = [(t, key) for t, key in keep if key in seen]
        keep.sort(key=lambda tk: tk[0].landed_at or datetime(1, 1, 1, tzinfo=UTC), reverse=True)
        for t, _key in keep[k:]:
            next(r for r in rows if r["tmdb_id"] == t.tmdb_id)["result"] = "over_size"
        keep = keep[:k]
        items, missing = ctx.plex.fetch_items([key for _, key in keep]) if keep else ([], [])
        by_key = {int(getattr(i, "ratingKey", 0)): i for i in items}
        picks: list[Pick] = []
        for t, key in keep:
            item = by_key.get(key)
            if item is None:
                next(r for r in rows if r["tmdb_id"] == t.tmdb_id)["result"] = "not_on_plex"
                continue
            picks.append(
                Pick(
                    tmdb_id=t.tmdb_id,
                    rating_key=key,
                    title=str(getattr(item, "title", "") or t.title),
                    rank=len(picks) + 1,
                    reason=_reason(t),
                    media_type=kind,
                    sources=["requests"],
                    year=getattr(item, "year", None),
                )
            )
        out[section.key] = picks
        policy.report.trace.setdefault("selection", []).append(
            {
                "row": spec.slug,
                "library": getattr(section, "title", str(section.key)),
                "decision": "requests",
                "size": k,
                "delivered": len(picks),
                "candidates": len(rows),
                "pick_order": "newest",
                "requests": rows,
            }
        )
    return out


def _watched(policy: RowPolicy, t: RequestedTitle) -> bool:
    if t.media_type is MediaType.MOVIE:
        return t.tmdb_id in policy.watched_movies
    viewed, total = policy.watched_shows.get(t.tmdb_id, (0, None))
    return total is not None and total > 0 and viewed >= total


def _reason(t: RequestedTitle) -> str:
    return f"You asked for this on {t.requested_at:%-d %b}" if t.requested_at else "You asked for this"


def _stamp(d: datetime | None) -> str | None:
    return d.isoformat() if d else None
```

Import `section_kind` from `shortlist.engine.delivery` (check for an import cycle: delivery must not import requests_row; it doesn't). `RequestedTitle` gains `pattern: str = ""` (from Task 5's note).

- [ ] **Step 4: Run** — `pytest tests/unit/test_requests_row.py -q` → all pass; ruff.
- [ ] **Step 5: Commit** — `git commit -am "feat(engine): build a person's requests row from the ledger (#127)"` (stage the two files explicitly).

---

### Task 7: Wire it into the run — short-circuit, remove-on-empty, ledger once per run

**Files:**
- Modify: `shortlist/engine/rows.py` (`_run_user` :3308-3340; `_drop_cold_skipped_rows` :1752)
- Modify: `shortlist/engine/pipeline.py` (after `ctx.section_index = section_index` :391, before the per-user loop that calls `rows._run_user` :538)
- Modify: `web/src/lib/row-templates.ts` is NOT touched here; the uniqueness test below reads the Python-side default only.
- Test: `tests/unit/test_pipeline.py` (fixture `ctx` :66, helpers `_run_two_row_user` :115), `tests/unit/test_requests_row.py`

**Interfaces:**
- Consumes: `build_requests_picks`, `collect_requests`, `RequestLedger`, `remove_row` (delivery.py:1012), `_ledger_keys` (rows.py:1739), `_forget` (rows.py:1801).
- Produces: `pipeline.engine_run` sets `ctx.request_ledger` when `any(s.requests_row for s in cfg.rows)` and `cfg.request_sources` is set; `rows._run_user` builds requests rows from it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_pipeline.py — append
from shortlist.engine.requests_row import RequestLedger, RequestedTitle


def _requests_spec(**kw) -> RowSpec:
    return RowSpec(slug="asked", name_template="📬 {library_name} you asked for", size=5, requests_row=True, **kw)


def _ledger(*tmdb_ids: int, complete: bool = True, person: int = 100) -> RequestLedger:
    at = datetime(2026, 9, 27, tzinfo=UTC)
    return RequestLedger(
        titles=[
            RequestedTitle(
                tmdb_id=t,
                media_type=MediaType.MOVIE,
                plex_account_id=person,
                requested_at=at,
                landed_at=at,
                on_disk=True,
                seasons_landed=True,
                found_in=("overseerr",),
            )
            for t in tmdb_ids
        ],
        complete=complete,
    )


class TestRequestsRow:
    def test_a_requests_row_delivers_the_ledger_and_never_gathers(self, ctx: EngineContext, mock_plextv, mock_tmdb):
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(10, 20)
        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))
        assert sorted(p.tmdb_id for p in report.picks) == [10, 20]
        assert all(p.sources == ["requests"] for p in report.picks)
        mock_tmdb.suggestions.assert_not_called()
        ctx.curator.curate.assert_not_called() if hasattr(ctx.curator, "curate") else None

    def test_an_empty_requests_row_is_removed_when_the_read_was_complete(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        removed = []
        monkeypatch.setattr(rows_mod, "remove_row", lambda *a, **kw: removed.append(kw.get("sections")) or ["1"])
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=True)
        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))
        assert removed and report.removed_deliveries == [{"row_slug": "asked", "library_key": "1"}]

    def test_an_empty_requests_row_is_left_alone_when_the_read_was_incomplete(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        removed = []
        monkeypatch.setattr(rows_mod, "remove_row", lambda *a, **kw: removed.append(1) or [])
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True)
        ctx.request_ledger = _ledger(complete=False)
        _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))
        assert removed == []

    def test_a_cold_person_still_gets_their_requests_row(self, ctx: EngineContext, mock_plextv):
        ctx.history_source.fetch.return_value = []  # below min_history
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, cold_start="skip")
        ctx.request_ledger = _ledger(10)
        report = _run_one(ctx, mock_plextv, make_profile("sarah", account_id=100))
        assert [p.tmdb_id for p in report.picks] == [10]

    def test_the_pipeline_builds_the_ledger_once_when_a_requests_row_exists(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(
            pipeline_mod,
            "collect_requests",
            lambda sources, people, **kw: calls.append(kw.get("patterns")) or _ledger(10),
        )
        ctx.config = replace(
            ctx.config,
            rows=[_requests_spec(requests_tag_pattern="req-{username}"), _requests_spec()],
            rows_defined=True,
            request_sources=RequestSources(overseerr=SeerrTarget(url="http://s", api_key="k")),
        )
        pipeline_mod.engine_run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])
        assert calls == [frozenset({"req-{username}"})]

    def test_no_sources_means_no_ledger_and_no_row(self, ctx: EngineContext, mock_plextv):
        ctx.config = replace(ctx.config, rows=[_requests_spec()], rows_defined=True, request_sources=None)
        reports = pipeline_mod.engine_run(ctx, [make_profile("sarah", account_id=100)])
        assert ctx.request_ledger is None
```

Write `_run_one(ctx, mock_plextv, profile)` next to `_run_two_row_user` (:115) by copying its body but with `ctx.config.rows` left as set by the test and one profile; read `_run_two_row_user` first — it shows how `_run_user` is called and which report it returns. Also import `RequestSources`, `SeerrTarget` from models. If `engine_run` needs a different call shape than `engine_run(ctx, users)`, follow `TestRun.test_happy_path_delivers_syncs_then_promotes` (:151).

- [ ] **Step 2: Run to verify they fail** — `pytest tests/unit/test_pipeline.py -k TestRequestsRow -q`.

- [ ] **Step 3: Implement**

`pipeline.py`, right after `ctx.section_index = section_index` (:391):
```python
# One read of who-asked-for-what per run, shared by every person's requests row. Built only when
# such a row exists, so a server without one pays nothing; and never fatal — an unreadable source
# leaves `complete=False`, which stops removals but not the rest of the night.
request_rows = [s for s in cfg.rows if s.requests_row]
if request_rows and cfg.request_sources is not None and cfg.request_sources.any():
    ctx.request_ledger = collect_requests(
        cfg.request_sources,
        users,
        patterns=frozenset(s.requests_tag_pattern for s in request_rows if s.requests_tag_pattern),
    )
    for problem in ctx.request_ledger.problems:
        logger.warning("requests row: {}", problem)
```
(`users` is the roster list `engine_run` receives; use its actual name. Import `from shortlist.engine.requests_row import collect_requests` at module top.)

`rows.py` `_run_user`, inside `for spec in specs:` after `targets = target_sections(...)` (:3316) and BEFORE `pool_for_row: list[Candidate] = []`:
```python
            if spec.requests_row:
                ledger = ctx.request_ledger
                if ledger is None:
                    continue  # no source configured — the Rows page says so; nothing to build or remove
                section_picks = build_requests_picks(policy, spec, targets, k, ledger, now=datetime.now(UTC))
                if ledger.complete:
                    # Nothing ready in a library = the row goes, or watched titles would sit in it.
                    # Only on a COMPLETE read: an outage must never empty everyone's row.
                    diff = user_report.diff if user_report.diff is not None else CollectionDiff()
                    user_report.diff = diff
                    for section in targets:
                        if section_picks.get(section.key):
                            continue
                        with ctx.write_lock:
                            removed_in = remove_row(ctx.plex, user, cfg, spec, dry_run=cfg.dry_run, diff=diff, sections=[section], delivered_keys=_ledger_keys(ctx, user, spec), other_rows=cfg.per_person_rows())
                        _forget(user_report, spec, removed_in)
                if not any(section_picks.values()):
                    continue
            else:
                <existing block from `pool_for_row: list[Candidate] = []` through the `_build_section_picks(...)` call, indented one level>
```
Then the existing stamping code continues unchanged with `section_picks`. Import `build_requests_picks` from `shortlist.engine.requests_row` and `UTC` from datetime (check what rows.py already imports).

`_drop_cold_skipped_rows`: first line of the loop body: `if spec.requests_row: keep.append(spec); continue` with the comment "A requests row needs no history depth — it is built from what they asked for, not what they watched."

Also in `_run_user`, the cold early-return: check the block at ~:3226-3245 — if `cold` and every spec was dropped it returns; with the requests row kept that path is not taken. Verify `base_cold` is only computed `if cold` and unused by the requests branch.

- [ ] **Step 4: Run** — `pytest tests/unit/test_pipeline.py -k TestRequestsRow -q`, then `pytest tests/unit/test_pipeline.py -q` (the whole file, ~seconds) → all pass.

- [ ] **Step 5: Prove the removal gate has teeth** — temporarily change `if ledger.complete:` to `if True:`; run `-k test_an_empty_requests_row_is_left_alone_when_the_read_was_incomplete` → must FAIL; restore by hand (never `git checkout` the file).

- [ ] **Step 6: Commit** — `git add shortlist/engine/rows.py shortlist/engine/pipeline.py tests/unit/test_pipeline.py && git commit -m "feat(engine): deliver the requests row and remove it when nothing is ready (#127)"`

---

### Task 8: Server — columns, migration, API, context builder

**Files:**
- Modify: `shortlist/server/db/models.py` (`Collection` after `sort_title_prefix` ~:318; `User` after `request_tag` :142)
- Create: `shortlist/server/db/alembic/versions/0094_requests_row.py` (`revision="0094"`, `down_revision="0093"`)
- Modify: `shortlist/server/api/collections.py` (`CollectionIn` :149, `CollectionOut` :396, `_validate` :572, `_serialize` :811, `create_collection` :1127-1155, `_PATCHABLE_COLUMNS` :1206)
- Modify: `shortlist/server/api/serializers.py` (`UserOut` :48, `user_dict` :160), `shortlist/server/api/users.py` (`UserPatch` :83, `patch_user` :461)
- Modify: `shortlist/server/services/context_builder.py` (`_build_rows` :1124-1170, `enabled_profiles` :963-974, `_engine_config` :1080, new `_build_request_sources`)
- Modify: `web/openapi.snapshot.json` (regenerate), `web/src/lib/api-schema.d.ts` (regenerate)
- Test: `tests/integration/test_api_collections.py`, `tests/integration/test_api_users.py` (or the users API test file that exists), `tests/unit/test_context_builder.py` (or wherever `_build_requests` is tested — `grep -rn "_build_requests" tests/`)

**Interfaces:**
- Columns: `collections.requests_row` Boolean NOT NULL server_default "0"; `collections.requests_window_days` Integer NOT NULL server_default "90"; `collections.requests_tag_pattern` String(128) NOT NULL server_default ""; `users.requested_by_tag` String(64) NOT NULL server_default "".
- `CollectionIn`: `requests_row: bool = False`, `requests_window_days: int = Field(default=90, ge=0, le=3650)`, `requests_tag_pattern: str = Field(default="", max_length=128)`. `CollectionOut` + `_serialize` echo them.
- `_validate`: `requests_row` requires `build == "per_person"`, `rewatch is False`, `seasons == []` (422 messages below); a non-empty pattern must contain `{username}` or `{name}` (422 `Tag pattern needs {username} or {name} in it`).
- `UserPatch.requested_by_tag: str | None = Field(default=None, max_length=64)`; `UserOut.requested_by_tag: str`.
- `ContextBuilder._build_request_sources(store) -> RequestSources | None` (staticmethod).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_api_collections.py — append inside the existing class or a new one
class TestRequestsRowFields:
    def test_requests_row_fields_round_trip_and_reach_the_spec(self, client):
        body = {
            "name": "📬 {library_name} you asked for",
            "build": "per_person",
            "requests_row": True,
            "requests_window_days": 30,
            "requests_tag_pattern": "req-{username}",
            "size": 20,
        }
        r = client.post("/api/collections", json=body)
        assert r.status_code == 201, r.text
        out = r.json()
        assert (out["requests_row"], out["requests_window_days"], out["requests_tag_pattern"]) == (
            True,
            30,
            "req-{username}",
        )
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            spec = next(
                s
                for s in ContextBuilder._build_rows(client.app.state.run_service._ctx, session, store)
                if s.slug == out["slug"]
            )
        assert (spec.requests_row, spec.requests_window_days, spec.requests_tag_pattern) == (True, 30, "req-{username}")

    @pytest.mark.parametrize(
        "bad,msg",
        [
            ({"build": "shared"}, "one row per person"),
            ({"rewatch": True}, "rewatch"),
            ({"seasons": ["halloween"]}, "seasonal"),
            ({"requests_tag_pattern": "req-sarah"}, "{username}"),
            ({"requests_window_days": 4000}, "less than or equal to 3650"),
        ],
    )
    def test_a_requests_row_rejects_shapes_it_cannot_be(self, client, bad, msg):
        body = {"name": "n", "build": "per_person", "requests_row": True, **bad}
        r = client.post("/api/collections", json=body)
        assert r.status_code == 422 and msg in r.text

    def test_patch_can_turn_a_row_into_a_requests_row(self, client):
        created = client.post("/api/collections", json={"name": "n", "build": "per_person"}).json()
        r = client.patch(f"/api/collections/{created['id']}", json={"requests_row": True})
        assert r.status_code == 200 and r.json()["requests_row"] is True
```

```python
# users API test file — append
def test_requested_by_tag_round_trips(client):
    users = client.get("/api/users").json()
    uid = users[0]["id"]
    r = client.patch(f"/api/users/{uid}", json={"requested_by_tag": " children "})
    assert r.status_code == 200 and r.json()["requested_by_tag"] == "children"
```

```python
# context builder test file — append (find how a SettingsStore is built there)
def test_request_sources_are_built_whenever_a_url_and_key_exist(store_factory):
    store = store_factory(
        {
            "requests.enabled": False,
            "requests.target": "arr",
            "requests.overseerr.url": "http://s",
            "requests.overseerr.apikey": "k",
            "requests.radarr.url": "http://r",
            "requests.radarr.apikey": "k",
            "requests.tag": "shortlist",
        }
    )
    src = ContextBuilder._build_request_sources(store)
    assert src.overseerr.url == "http://s" and src.radarr.url == "http://r" and src.sonarr is None
    assert src.exclude_seerr_user_id == 0  # requests are off, so no account is Shortlist's
    assert src.shortlist_tag == "shortlist"


def test_the_request_as_account_is_excluded_only_when_shortlist_sends_via_overseerr(store_factory):
    store = store_factory(
        {
            "requests.enabled": True,
            "requests.target": "overseerr",
            "requests.overseerr.url": "http://s",
            "requests.overseerr.apikey": "k",
            "requests.overseerr.request_as_user_id": 7,
        }
    )
    assert ContextBuilder._build_request_sources(store).exclude_seerr_user_id == 7


def test_no_sources_is_none(store_factory):
    assert ContextBuilder._build_request_sources(store_factory({})) is None
```
(Replace `store_factory` with the fixture that file already uses; if none, build `SettingsStore` over an in-memory session the way `tests/unit/test_settings_store.py` does.)

- [ ] **Step 2: Run to verify they fail** — the three files, scoped.

- [ ] **Step 3: Implement**

Migration `0094_requests_row.py` (copy 0091's guarded pattern):
```python
"""Requests row (issue #127): three per-row settings and one per-person tag."""

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("collections", sa.Column("requests_row", sa.Boolean(), nullable=False, server_default="0")),
    ("collections", sa.Column("requests_window_days", sa.Integer(), nullable=False, server_default="90")),
    ("collections", sa.Column("requests_tag_pattern", sa.String(128), nullable=False, server_default="")),
    ("users", sa.Column("requested_by_tag", sa.String(64), nullable=False, server_default="")),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in _COLUMNS:
        if column.name not in _columns(bind, table):
            op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in reversed(_COLUMNS):
        if column.name in _columns(bind, table):
            op.drop_column(table, column.name)
```
Models: matching `mapped_column(..., default=..., server_default=...)` lines in the style of `rewatch` (:191) and `request_tag` (:142). `tests/unit/test_migrations.py::test_a_migrated_database_has_no_drift_from_the_models` enforces the match — run it.

`_validate` additions (messages exactly): `"A requests row is always one row per person"`, `"A requests row can't also be a rewatch row"`, `"A requests row can't be seasonal — it shows what they asked for whenever it lands"`, `"Tag pattern needs {username} or {name} in it"`.

`_build_rows`: `requests_row=bool(collection.requests_row), requests_window_days=int(collection.requests_window_days if collection.requests_window_days is not None else 90), requests_tag_pattern=(collection.requests_tag_pattern or "").strip()`. `enabled_profiles`: `requested_by_tag=(user.requested_by_tag or "").strip()`.

`_build_request_sources`:
```python
@staticmethod
def _build_request_sources(store: SettingsStore) -> RequestSources | None:
    """Where a requests row reads from — every app with a URL and key, whatever `requests.*` says."""

    def seerr() -> SeerrTarget | None:
        url, key = (store.get("requests.overseerr.url") or "").strip(), store.get("requests.overseerr.apikey") or ""
        return (
            SeerrTarget(
                url=url, api_key=key, request_as_user_id=int(store.get("requests.overseerr.request_as_user_id") or 0)
            )
            if url and key
            else None
        )

    def arr(prefix: str) -> ArrTarget | None:
        url, key = (store.get(f"{prefix}.url") or "").strip(), store.get(f"{prefix}.apikey") or ""
        return ArrTarget(url=url, api_key=key, quality_profile_id=0, root_folder="", tag="") if url and key else None

    overseerr, radarr, sonarr = seerr(), arr("requests.radarr"), arr("requests.sonarr")
    if not (overseerr or radarr or sonarr):
        return None
    sends_via_seerr = bool(store.get("requests.enabled")) and store.get("requests.target") == "overseerr"
    return RequestSources(
        overseerr=overseerr,
        radarr=radarr,
        sonarr=sonarr,
        exclude_seerr_user_id=overseerr.request_as_user_id if overseerr and sends_via_seerr else 0,
        shortlist_tag=(store.get("requests.tag") or "").strip(),
    )
```
and in `_engine_config`: `request_sources=self._build_request_sources(store)`.

Regenerate the snapshot + web types with the command in `tests/unit/test_openapi_snapshot.py:10-15`.

- [ ] **Step 4: Run** — the three scoped files + `pytest tests/unit/test_migrations.py tests/unit/test_openapi_snapshot.py -q` → pass. `pnpm -C web exec tsc -b --force` must still pass (generated types only added fields).

- [ ] **Step 5: Commit** — stage the listed files + `web/openapi.snapshot.json` + `web/src/lib/api-schema.d.ts`; `git commit -m "feat(server): requests-row settings, per-person request tag, request sources (#127)"`

---

### Task 9: Server — the setup-check endpoint

**Files:**
- Modify: `shortlist/server/api/requests.py` (add route; look at `_fetch_statuses` :310 for how `svc.build_requests_context()` and `SeerrClient` are used)
- Modify: `shortlist/server/services/context_builder.py` (`build_request_sources_only(self) -> tuple[RequestSources | None, list[UserProfile]]` next to `build_requests_only` :588), `shortlist/server/services/run_service.py` (delegate)
- Test: `tests/integration/test_api_requests.py` (or create)

**Interfaces:**
- `GET /api/requests/row-sources?pattern=<str>` → `RowSourcesOut`:
  ```python
  class RowSourceServerOut(BaseModel):
      kind: str
      name: str
      is4k: bool
      tag_requests: bool


  class TagMatchOut(BaseModel):
      label: str
      source: str
      user_id: int | None
      display_name: str
      titles: int
      ambiguous: bool


  class RowSourcesOut(BaseModel):
      overseerr: str  # "connected" | "unreachable" | "off"
      radarr: str
      sonarr: str
      complete: bool
      problems: list[str]
      seerr_requests: int
      seerr_requesters: int
      seerr_linked: int
      servers: list[RowSourceServerOut]
      tagged_movies: int
      tagged_shows: int
      people: list[PersonReadyOut]  # user_id, display_name, linked: bool, ready: int (titles on disk, any library)
      tags: list[TagMatchOut]
  ```
- Implementation calls `collect_requests(sources, profiles, patterns=frozenset({pattern} if pattern else ()))`; `overseerr`/`radarr`/`sonarr` state = "off" when the target is None, "unreachable" when a problem line names it, else "connected". `people` covers every enabled profile; `ready` = `len([t for t in ledger.for_person(pid) if t.on_disk])`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_api_requests.py — append
import respx, httpx, json
from pathlib import Path

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def test_row_sources_reports_each_source_and_who_is_linked(client):
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("requests.overseerr.url", "http://seerr")
        store.set("requests.overseerr.apikey", "k")
        session.commit()
    reqs = json.loads((FIX / "overseerr_requests_page.json").read_text())
    users = {
        "pageInfo": {"results": 1, "pages": 1},
        "results": [{"id": reqs["results"][0]["requestedBy"]["id"], "plexId": 100, "displayName": "Sarah"}],
    }
    with respx.mock:
        respx.get("http://seerr/api/v1/request").mock(return_value=httpx.Response(200, json=reqs))
        respx.get("http://seerr/api/v1/user").mock(return_value=httpx.Response(200, json=users))
        respx.get("http://seerr/api/v1/settings/radarr").mock(
            return_value=httpx.Response(200, json=[{"name": "r", "is4k": False, "tagRequests": True}])
        )
        respx.get("http://seerr/api/v1/settings/sonarr").mock(return_value=httpx.Response(200, json=[]))
        respx.get("http://seerr/api/v1/media").mock(
            return_value=httpx.Response(200, json={"pageInfo": {"results": 0}, "results": []})
        )
        r = client.get("/api/requests/row-sources")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["overseerr"] == "connected" and out["radarr"] == "off"
    assert out["servers"] == [{"kind": "radarr", "name": "r", "is4k": False, "tag_requests": True}]
    sarah = next(p for p in out["people"] if p["display_name"].lower().startswith("sarah"))
    assert sarah["linked"] is True


def test_row_sources_says_off_when_nothing_is_configured(client):
    out = client.get("/api/requests/row-sources").json()
    assert (out["overseerr"], out["radarr"], out["sonarr"], out["complete"]) == ("off", "off", "off", True)
```
(The `client` fixture seeds users sarah (plex_account_id 100) and mike — confirm in `tests/integration/conftest.py:32`.)

- [ ] **Step 2: Run to verify it fails.** — `pytest tests/integration/test_api_requests.py -k row_sources -q`

- [ ] **Step 3: Implement** the route (owner-gated like its neighbours in `requests.py`), the pydantic models, and `build_request_sources_only` (a session, `SettingsStore`, `self._build_request_sources(store)`, `self.enabled_profiles(session)`). Map `problems` → state: a problem containing "Overseerr" → `overseerr="unreachable"`, "Radarr" → radarr, "Sonarr" → sonarr. Regenerate the OpenAPI snapshot + web types again.

- [ ] **Step 4: Run** — scoped file + `pytest tests/unit/test_openapi_snapshot.py -q`.
- [ ] **Step 5: Commit** — `git commit -m "feat(api): requests-row setup check (#127)"`.

---

### Task 10: Web — the row kind, template, editor block, gallery tile

**Files:**
- Modify: `web/src/lib/row-kind-meta.ts` (`RowKind` :10, `ROW_KINDS` :28, `ROW_FILLS` :31, `KIND_META` :33, `FILL_META` :56)
- Modify: `web/src/lib/row-kinds.ts` (`fillOf` :94, `KIND_FIELDS` :151, `fillPatch` :227, `ROW_SETTING_KEYS` :290, `SETTING_LABELS` :328, `FIELD_SETTING` :376, `FILL_SETTINGS` :466, `FILL_TEMPLATE_ID` :682)
- Modify: `web/src/lib/row-templates.ts` (new entry after "seen-it-already")
- Modify: `web/src/components/rows/row-kind-settings.tsx` (`FillBlock` :192; new `YourRequestsBlock`)
- Modify: `web/src/components/rows/row-template-gallery.tsx` (disabled tile)
- Modify: `web/src/lib/queries.ts` (`useRequestRowSources`), `web/src/lib/api.ts` (`getRequestRowSources`), `web/src/lib/types.ts` (`RowSources = Schemas["RowSourcesOut"]`)
- Test: `web/src/test/row-kinds.test.ts`, `web/src/test/row-editor-kinds.test.tsx`, new `web/src/test/row-requests-block.test.tsx`

**Interfaces:**
- `RowKind` gains `"requests"`; `ROW_KINDS = ["picked", "byw", "again", "requests", "seasonal", "popular"]`; `ROW_FILLS` gains `"requests"`; seasonal's fill picker excludes it (`SEASONAL_FILLS = ROW_FILLS.filter((f) => f !== "requests")`, used wherever the seasonal "How it's filled" picker lists fills).
- `KIND_META.requests = { title: "Your requests", description: "What they asked for in Overseerr that's now on Plex, newest first. Never recommendations." }`.
- `fillOf`: `if (input.requests_row) return "requests";` first (before `build === "shared"`; a requests row is per person by validation).
- `fillPatch` case `"requests"`: `{ build: "per_person", rewatch: false, requests_row: true, unstarted_only: false, seed_window: 1 }`; every other case adds `requests_row: false`.
- New setting keys `requests_window_days`, `requests_tag_pattern`, `requests_sources`; `FILL_SETTINGS.requests = ["requests_window_days", "requests_tag_pattern", "requests_sources"]`; `FIELD_SETTING`: `requests_row: "kind"` (or whatever key `rewatch` maps to), `requests_window_days`, `requests_tag_pattern` → themselves.
- Template `your-requests`: emoji 📬, title "Your requests", blurb "What they asked for in Overseerr, once it's on Plex. Each title leaves once they've watched it.", highlights `["Only what they asked for", "Newest first", "Overseerr or Radarr/Sonarr tags"]`, values `{ name: "📬 {library_name} you asked for", build: "per_person", requests_row: true, requests_window_days: 90, size: 20 }`.
- `YourRequestsBlock`: `KindBlock title="Which requests show up"`: number input (`id="row-requests-window"`, label "Show titles that landed in the last", suffix "days", helper "Older arrivals drop off, so a request they've lost interest in doesn't sit there for good. 0 keeps every title until they've watched it."); sources panel (from `useRequestRowSources(input.requests_tag_pattern)`, 300ms debounce on the pattern) with one row each for Overseerr / Radarr / Sonarr / People and a Badge per state; `<details>` "Use my own tags" with the pattern `Input` (`id="row-requests-pattern"`, helper as in the mockup) and a preview table of `tags`; callout "A person with nothing ready gets no row. It appears on the first run after something they asked for lands, and goes again once they've watched everything in it."
- Gallery: when `useRequestRowSources("")` says every source is "off", the `your-requests` tile renders disabled with the text "Needs a way to know who asked for what: an Overseerr or Jellyseerr connection, or Radarr/Sonarr with request tags." and a link to `/settings#connections` (check the settings route/anchor the app uses).

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/test/row-kinds.test.ts — update the order pin and add:
it("a requests row is its own fill and never shared", () => {
  const input = { ...blankInput(), requests_row: true };
  expect(rowKindOf(input, ctx).fill).toBe("requests");
  const patched = applyRowKind(blankInput(), { kind: "requests", fill: "requests" }, ctx);
  expect(patched.requests_row).toBe(true);
  expect(patched.build).toBe("per_person");
  const back = applyRowKind(patched, { kind: "picked", fill: "picked" }, ctx);
  expect(back.requests_row).toBe(false);
});

it("only the requests settings show for a requests row", () => {
  const shown = visibleSettings({ ...blankInput(), requests_row: true }, ctx);
  expect(shown.has("requests_window_days")).toBe(true);
  expect(shown.has("candidate_sources")).toBe(false);
  expect(shown.has("cold_start")).toBe(false);
});
```
```tsx
// web/src/test/row-requests-block.test.tsx
it("shows each source's state and the window field", async () => {
  server.use(http.get("/api/requests/row-sources", () => HttpResponse.json({ overseerr: "connected", radarr: "off", sonarr: "off", complete: true, problems: [], seerr_requests: 7, seerr_requesters: 6, seerr_linked: 6, servers: [], tagged_movies: 0, tagged_shows: 0, people: [], tags: [] })));
  renderBlock({ ...blankInput(), requests_row: true, requests_window_days: 90 });
  expect(screen.getByLabelText("Show titles that landed in the last")).toHaveValue(90);
  expect(await screen.findByText("Connected")).toBeInTheDocument();
  expect(screen.getByText(/7 requests/)).toBeInTheDocument();
});
```
(Use the MSW/`server.use` pattern `row-request-settings.test.tsx` uses — read it first; if that file mocks `api.*` with `vi.mock` instead, follow that.)

- [ ] **Step 2: Run to verify they fail** — `pnpm -C web test -- row-kinds row-requests-block`.

- [ ] **Step 3: Implement** all the interface items above. Keep `visibleSettings` hiding `size` for the default row as today. Copy strings verbatim from the Interfaces block.

- [ ] **Step 4: Run** — the two test files, then `pnpm -C web exec tsc -b --force` and `pnpm -C web exec eslint src`.

- [ ] **Step 5: Commit** — `git commit -m "feat(web): the Your requests row kind, template and editor block (#127)"`.

---

### Task 11: Web — Users column, per-person tag, trace step

**Files:**
- Modify: `web/src/pages/users.tsx` (header :365-414, cells ~:438)
- Create: `web/src/components/user-detail/user-requested-by-tag.tsx` (copy `user-request-tag.tsx`, field `requested_by_tag`, label "Their request tag in Radarr/Sonarr", helper "If their requests carry a tag that doesn't fit the row's pattern, put it here, e.g. children."); mount it in `user-detail.tsx` next to :116
- Modify: `web/src/pages/run-user-trace.tsx` (`LibraryFlow` defs :645-741), `web/src/lib/trace.ts` (add `requestResultLabel(result: string): string`)
- Test: `web/src/test/trace.test.ts` (or the existing trace test file), a testing-library test for the users column

**Interfaces:**
- Users page: new `hidden lg:table-cell` column "Requests"; per row a Badge: `Linked` (success) with sub "N ready" / `No account` (secondary, "Hasn't signed in to Overseerr") / `Can't use Overseerr` (secondary, for `user_type === "managed"`) / `Tag: <requested_by_tag>` (outline) when set. Data from `useRequestRowSources("")` joined on `people[].user_id`; while loading, a skeleton in the cell; on error, "—" with a tooltip "Couldn't read Overseerr".
- `requestResultLabel`: `in_row → "In the row"`, `not_on_plex → "Not on Plex yet"`, `season_not_landed → "That season hasn't landed"`, `watched → "Already watched"`, `too_old → "Landed more than N days ago"` (N from the selection entry's spec via the row's `requests_window_days` if present in the payload, else "Landed too long ago"), `hidden → "Hidden by their Plex restrictions"`, `over_size → "Past the row size"`.
- Trace step: when a `selection` entry for this library has `decision === "requests"`, `LibraryFlow` renders `{ id: `${lib.key}-requests`, title: "What they asked for", count: entry.delivered, subtitle: `${entry.candidates} requests looked at`, body: <table> Title / Asked for / Landed / Found in / Result }` and skips the watched/seeds/search/shortlist steps for that library.

- [ ] **Step 1: Write the failing tests** — `requestResultLabel` table test; a users-page test that mocks `/api/users` and `/api/requests/row-sources` and expects "Linked" next to the linked user and "Can't use Overseerr" next to a managed one.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the two files, `tsc -b --force`, eslint.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): Users column, per-person request tag and the requests trace step (#127)"`.

---

### Task 12: Docs, full verification, Architecture Review, live proof

**Files:**
- Modify: `docs/guides/requests.md` (new section "A row of what they asked for"), `docs/reference.md` (Collection fields `requests_row`, `requests_window_days`, `requests_tag_pattern`; User `requested_by_tag`; `GET /api/requests/row-sources`), `README.md` (one feature bullet), `docs/llms-full.txt` (regenerate: `python scripts/build_llms_full.py`)
- Modify: `.claude/docs/jobs-and-runs-design.md` §12 — add the requests row's removal to the mutation audit (what changes on Plex, and the `ledger.complete` gate)
- Modify: `.claude/rules/plex-safety.md` rule 1 — one sentence: a requests row is REMOVED when empty, only on a complete source read.

- [ ] **Step 1: Write the docs.** The guide section says, in this order: what the row shows; the two sources and that deleting a request in Overseerr is fine because the tag stays; "Tag Requests" must be on in Overseerr → Settings → Services → each server, and only requests made after that are covered; own tags via the pattern and the per-person tag; that a person with nothing ready has no row; the window setting.
- [ ] **Step 2: `python scripts/build_llms_full.py`** and `pytest tests/unit/test_llms_full.py -q`.
- [ ] **Step 3: Full pass, once:** `ruff check . && ruff format --check .`, `pytest` (respect the one-at-a-time hook), `pnpm -C web test`, `pnpm -C web exec tsc -b --force`, `pnpm -C web exec eslint src`, `pnpm -C web build`, then `pytest -m e2e` (the wizard and rows page changed). Dispatch to `verifier` with `model: "fable"`; fix and re-run only what failed.
- [ ] **Step 4: Architecture Review** (`model: "fable"`) on the whole branch diff vs `dev` — it adds a delete path, maps identity across systems, and adds a migration. Block on HIGH findings.
- [ ] **Step 5: Commit docs** — `git commit -m "docs: the Your requests row (#127)"`.
- [ ] **Step 6: Live proof (ask the owner first — it writes to Plex).** Fix the stale Overseerr address in Settings → Connections, add the template row, run it scoped to ONE linked person via the API with an owner cookie (memory `live-run-proof-recipe`), and confirm on Plex: the row exists only for that person, holds only their requested titles, and is absent from another account's Home. Then a second run with nothing ready for a test person must remove the row.
