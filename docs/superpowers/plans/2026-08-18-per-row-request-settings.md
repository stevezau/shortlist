# Per-Row Request Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each per-person row carry its own Sonarr/Radarr target and its own request floors, and stop a
multi-row run from letting one row consume the whole run's requests.

**Architecture:** Each row gates its own missing titles under its own floors (its own demand map, its own
share of the rating-lookup budget). One allocation step then divides the run's `max_per_run` slots evenly
across rows, redistributing any share a row cannot fill. A title wanted by several rows is claimed by the
first row, in row order, whose gate it passed — one title, one slot. Then a single send.

**Tech Stack:** Python 3.12, SQLAlchemy 2 + Alembic, FastAPI, pytest; React 19 + Vite + TS for the editor.

**Spec:** Design agreed in conversation 2026-08-18; the Architecture and Global Constraints sections
below are that design, restated in full. No separate spec file.

## Global Constraints

- **Ceilings stay global; taste goes per-row.** Never overridable per row: `enabled`, `rating_source`,
  `mdblist_api_key`, `max_per_run`, the rating-lookup budget, `tag`.
- **Per-row overridable** (NULL/empty → inherit global): radarr/sonarr **quality profile + root folder**
  (URL and API key stay global — one instance), `min_rating`, `min_votes`, `min_demand`, `min_year`,
  `max_year`, `auto_send`, `auto_min_demand`, `auto_min_rating`, plus a new `max_per_row`.
- **`max_per_row` may only restrict.** The global `max_per_run` still binds the run total.
- **Per-person rows only.** Shared rows and cold-start users build from titles already on the server, so
  they surface nothing missing and get no request settings. Verified 2026-08-18 three ways: `_shared_row`
  and `_cold_start` are passed no `demand`; a shared row is built from watch history (already in the
  library); and across 34 live candidates `why` never names the shared row.
- **Upgrade is a no-op.** Every new column is nullable with no backfill; an existing install behaves
  exactly as today until an override is set.
- **Engine purity.** `shortlist/engine/` must not import from `shortlist/server/`.
- Style: ruff, 120 cols, type hints, Google docstrings, `from loguru import logger`.

---

### Task 1: Resolve a row's effective RequestConfig

**Files:**

- Modify: `shortlist/engine/models.py` (add `RequestOverrides`; add `RowSpec.request_overrides`)
- Create: `shortlist/engine/request_config.py`
- Test: `tests/unit/test_request_config.py`

**Interfaces:**

- Produces: `RequestOverrides` (frozen dataclass, every field optional/None);
  `resolve_request_config(base: RequestConfig, overrides: RequestOverrides | None) -> RequestConfig`;
  `RowSpec.request_overrides: RequestOverrides | None = None`.

A separate dataclass rather than ten more flat `RowSpec` fields: `RowSpec` is built positionally in
several call sites, so a run of new fields in the middle silently shifts arguments (the hazard its own
`fallback_name` comment records). One optional field appended at the end is safe.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_overrides_returns_the_global_config_unchanged():
    base = _cfg(min_rating=7.0, max_per_run=10)
    assert resolve_request_config(base, None) == base


def test_an_override_replaces_only_that_field():
    base = _cfg(min_rating=7.0, min_year=2000)
    out = resolve_request_config(base, RequestOverrides(min_rating=8.5))
    assert (out.min_rating, out.min_year) == (8.5, 2000)


def test_a_row_cannot_widen_the_run_ceiling():
    """max_per_run is the library's protection; a row may never raise it."""
    base = _cfg(max_per_run=10)
    out = resolve_request_config(base, RequestOverrides(max_per_row=999))
    assert out.max_per_run == 10


def test_a_row_target_overrides_profile_and_folder_but_keeps_url_and_key():
    base = _cfg(radarr=ArrTarget(url="http://r", api_key="k", quality_profile_id=1, root_folder="/m"))
    out = resolve_request_config(base, RequestOverrides(radarr_quality_profile_id=9, radarr_root_folder="/kids"))
    assert (out.radarr.url, out.radarr.api_key) == ("http://r", "k")
    assert (out.radarr.quality_profile_id, out.radarr.root_folder) == (9, "/kids")


def test_a_target_override_on_an_unconfigured_arr_stays_none():
    """No global Radarr means no Radarr — a row override must not conjure one without a URL/key."""
    out = resolve_request_config(_cfg(radarr=None), RequestOverrides(radarr_root_folder="/kids"))
    assert out.radarr is None
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/unit/test_request_config.py -q` → ImportError.
- [ ] **Step 3: Implement `RequestOverrides` + `resolve_request_config`.**
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** — `feat(requests): resolve a row's effective request config`

---

### Task 2: Demand keyed by row

**Files:**

- Modify: `shortlist/engine/requests.py` (`RowDemand` type alias)
- Modify: `shortlist/engine/rows.py:1711` (`_record_demand`)
- Modify: `shortlist/engine/pipeline.py:107,116,170`
- Test: `tests/unit/test_requests.py`, `tests/unit/test_rows_demand.py`

**Interfaces:**

- Consumes: nothing from Task 1.
- Produces: `RowDemand = dict[str, DemandMap]` (row slug → that row's demand map);
  `_record_demand(policy, demand: RowDemand)` writes under each spec's slug.

`min_demand` becomes a per-row floor, so demand must be counted per row or the number silently changes
meaning — a row set to "3 people" would otherwise count wanters from rows it has nothing to do with.

- [ ] **Step 1: Write the failing test**

```python
def test_demand_is_counted_per_row_not_across_rows():
    """One person wanting a title in two rows is demand 1 in each — never 2."""
    demand: RowDemand = {}
    _record_demand(_policy(user="sarah", specs=[_spec("picked"), _spec("because")]), demand)
    assert set(demand) == {"picked", "because"}
    assert demand["picked"][(550, MediaType.MOVIE)].demand == 1
    assert demand["because"][(550, MediaType.MOVIE)].demand == 1


def test_two_people_in_the_same_row_accumulate():
    demand: RowDemand = {}
    _record_demand(_policy(user="sarah", specs=[_spec("picked")]), demand)
    _record_demand(_policy(user="mike", specs=[_spec("picked")]), demand)
    assert demand["picked"][(550, MediaType.MOVIE)].demand == 2
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — key `demand` by `spec.slug`; move the per-user dedup inside the spec loop.
- [ ] **Step 4: Run to verify they pass; run `tests/unit/test_pipeline.py` for regressions.**
- [ ] **Step 5: Commit** — `refactor(requests): count demand per row`

---

### Task 3: Allocation — even split, surplus redistribution, one slot per title

**Files:**

- Create: `shortlist/engine/request_alloc.py`
- Test: `tests/unit/test_request_alloc.py`

**Interfaces:**

- Produces: `allocate(per_row: list[tuple[str, list[MissingTitle]]], *, cap: int, row_caps: dict[str, int])
-> list[tuple[str, MissingTitle]]` — rows in run order; returns (row_slug, title) claims, at most `cap`.

- [ ] **Step 1: Write the failing tests** (the full matrix — every cell changes behaviour)

```python
def test_a_single_row_gets_its_own_max_not_the_global():
    """Steve's case: global 10, one row capped at 3 -> 3."""
    out = allocate([("picked", _titles(20))], cap=10, row_caps={"picked": 3})
    assert len(out) == 3


def test_two_rows_split_the_global_cap_evenly():
    out = allocate([("a", _titles(20)), ("b", _titles(20))], cap=10, row_caps={})
    assert Counter(slug for slug, _ in out) == {"a": 5, "b": 5}


def test_a_row_that_cannot_fill_its_share_hands_the_surplus_back():
    """global 10, A capped at 3, B uncapped -> 3 + 7, not 3 + 5 with two slots idle."""
    out = allocate([("a", _titles(20)), ("b", _titles(20))], cap=10, row_caps={"a": 3})
    assert Counter(slug for slug, _ in out) == {"a": 3, "b": 7}


def test_a_row_short_of_titles_also_hands_its_surplus_back():
    out = allocate([("a", _titles(2)), ("b", _titles(20))], cap=10, row_caps={})
    assert Counter(slug for slug, _ in out) == {"a": 2, "b": 8}


def test_a_title_in_two_rows_is_claimed_once_by_the_earlier_row():
    shared = _title(550)
    out = allocate([("a", [shared]), ("b", [shared] + _titles(5))], cap=10, row_caps={})
    assert [(s, t.tmdb_id) for s, t in out].count(("a", 550)) == 1
    assert all(not (s == "b" and t.tmdb_id == 550) for s, t in out)


def test_a_claimed_title_frees_the_other_rows_slot_for_its_next_pick():
    """One title consumes ONE slot in total, so 10 slots still yield 10 titles."""
    shared = _title(550)
    out = allocate([("a", [shared]), ("b", [shared] + _titles(20))], cap=10, row_caps={})
    assert len(out) == 10
    assert len({t.tmdb_id for _, t in out}) == 10


def test_everything_is_short_so_the_cap_is_not_reached():
    out = allocate([("a", _titles(1)), ("b", _titles(1))], cap=10, row_caps={})
    assert len(out) == 2


def test_no_rows_yields_nothing():
    assert allocate([], cap=10, row_caps={}) == []


def test_a_zero_cap_sends_nothing():
    assert allocate([("a", _titles(5))], cap=0, row_caps={}) == []


def test_rounding_favours_the_earlier_row():
    """10 slots across 3 rows: 4/3/3, deterministic by run order."""
    out = allocate([("a", _titles(9)), ("b", _titles(9)), ("c", _titles(9))], cap=10, row_caps={})
    assert [Counter(s for s, _ in out)[k] for k in ("a", "b", "c")] == [4, 3, 3]
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement water-filling** — even shares, claim titles in row order skipping already-claimed
      ids, return unusable share to the pool, repeat until cap met or no row can take more.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** — `feat(requests): allocate run slots across rows`

---

### Task 4: Split the lookup budget across rows

**Files:**

- Modify: `shortlist/engine/requests.py` (`request_missing` → per-row orchestration)
- Test: `tests/unit/test_requests.py`

**Interfaces:**

- Produces: `request_missing(base_cfg, tmdb, per_row_demand: RowDemand, per_row_cfg: dict[str, RequestConfig],
*, row_order: list[str], dry_run, min_write_interval=1.0, already_handled=None, mdblist=None) -> RequestReport`.

Without this, one row's titles consume the whole rating budget and another row reaches allocation with
nothing rated to put in its slots — the starvation bug of 2026-08-18, one level up.

- [ ] **Step 1: Write the failing tests**

```python
def test_each_row_gets_a_share_of_the_lookup_budget():
    """Row A's 500 candidates must not consume every lookup and leave B unrated."""
    report = request_missing(
        base,
        FakeTmdb(),
        {"a": _demand(500), "b": _demand(500)},
        {"a": base, "b": base},
        row_order=["a", "b"],
        dry_run=True,
        mdblist=mdb,
    )
    assert report.examined_by_row["b"] > 0


def test_an_unused_lookup_share_is_returned_to_the_other_rows():
    report = request_missing(
        base,
        FakeTmdb(),
        {"a": _demand(2), "b": _demand(500)},
        {"a": base, "b": base},
        row_order=["a", "b"],
        dry_run=True,
        mdblist=mdb,
    )
    assert mdb.live_lookups == requests_mod._lookup_budget(base.max_per_run)


def test_a_title_in_two_rows_costs_one_live_lookup():
    """The second row's read is a cache hit — free — so overlap never doubles quota spend."""
    same = _demand_with(550)
    request_missing(
        base,
        FakeTmdb(),
        {"a": same, "b": same},
        {"a": base, "b": base},
        row_order=["a", "b"],
        dry_run=True,
        mdblist=mdb,
    )
    assert mdb.live_lookups == 1


def test_each_row_gates_on_its_own_floors():
    strict = replace(base, min_rating=9.0)
    report = request_missing(
        base,
        FakeTmdb(),
        {"a": _demand_rated(7.5), "b": _demand_rated(7.5)},
        {"a": strict, "b": base},
        row_order=["a", "b"],
        dry_run=True,
        mdblist=mdb,
    )
    assert report.considered_by_row == {"a": 0, "b": 1}
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** — per-row gate using each row's config and lookup share (water-filled the same
      way), then `allocate`, then one `_send`. Add `considered_by_row` / `examined_by_row` to `RequestReport`.
- [ ] **Step 4: Run the whole of `tests/unit/test_requests.py`** — all 74 existing tests must still pass.
- [ ] **Step 5: Commit** — `feat(requests): gate and budget per row`

---

### Task 5: Send under the claiming row's target

**Files:**

- Modify: `shortlist/engine/requests.py` (`_send`, `_request_one`)
- Test: `tests/unit/test_requests.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_each_title_is_sent_to_its_claiming_rows_target():
    kids = replace(base, radarr=replace(RADARR, root_folder="/kids", quality_profile_id=9))
    ...
    assert fake_by_folder["/kids"] == ["Luca"]
    assert fake_by_folder["/movies"] == ["Dune"]


def test_a_row_with_auto_send_off_queues_while_another_row_sends():
    """auto_send is per row: one row queueing must not stop another sending."""
    ...
    assert [m.title for m in report.sent] == ["Dune"]
    assert "auto-send is off" in report.queued[0].detail
```

- [ ] **Step 2–4: fail, implement, pass.**
- [ ] **Step 5: Commit** — `feat(requests): send each title under its row's target`

---

### Task 6: Migrations + persistence

**Files:**

- Modify: `shortlist/server/db/models.py` (`Collection` request columns; `RequestCandidate.row_slug`)
- Create: `alembic/versions/XXXX_per_row_request_settings.py`
- Create: `alembic/versions/XXXX_request_candidate_row_slug.py`
- Modify: `shortlist/engine/models.py` (`RequestWhy.row_slug`)
- Test: `tests/unit/test_migrations.py`, `tests/unit/test_request_queue.py`

`RequestWhy.row` is a _rendered display name_ ("✨ TV Shows Picked for You"), so a candidate approved
months later cannot resolve its row. Persist the slug.

- [ ] **Step 1: Write the failing tests** — upgrade/downgrade round-trip; existing rows read NULL and
      therefore inherit; a candidate with no `row_slug` falls back to the global config.
- [ ] **Step 2–4: fail, implement, pass.**
- [ ] **Step 5: Commit** — `feat(db): per-row request settings and candidate row slug`

---

### Task 7: Server wiring

**Files:**

- Modify: `shortlist/server/services/context_builder.py:1033` (build `RequestOverrides` per row)
- Modify: `shortlist/server/services/run_persistence.py` (per-row stats; `row_slug` on persisted candidates)
- Modify: `shortlist/server/api/requests.py:407` (approve resolves the row's target)
- Test: `tests/unit/test_run_service.py`, `tests/integration/test_requests_api.py`

- [ ] **Step 1: Write the failing tests** — including: approving a queued title sends it to _its row's_
      root folder, and a pre-migration candidate (NULL slug) still sends under the global config.
- [ ] **Step 2–4: fail, implement, pass.**
- [ ] **Step 5: Commit** — `feat(requests): wire per-row settings through the server`

---

### Task 8: Row editor UI

**Files:**

- Modify: `web/src/components/rows/row-editor.tsx`
- Modify: `web/src/lib/api-schema.d.ts` (regenerate: `pnpm -C web gen:api`)
- Test: `web/src/test/row-editor-requests.test.tsx`

- [ ] **Step 1: Write the failing tests** — the Requests section renders inherited globals as placeholders;
      setting an override marks the field overridden; **the section is absent for a shared row**.
- [ ] **Step 2–4: fail, implement, pass.** Run `tsc -b` and `pnpm -C web test`.
- [ ] **Step 5: Commit** — `feat(web): per-row request settings in the row editor`

---

### Task 9: Docs, full suite, live dry-run proof

**Files:**

- Modify: `docs/reference.md`, `docs/guides/requests.md`, `CHANGELOG.md` (Unreleased)

- [ ] **Step 1: Update docs** — the global/per-row split table, `max_per_row`, the allocation rule, the
      collision rule, and that shared rows have no request settings and why.
- [ ] **Step 2: Full suite** — `pytest`, `pnpm -C web test`, `tsc -b`, `pytest -m e2e` (the row editor changed).
- [ ] **Step 3: Live dry-run proof** — sync to the host build, run against the real server with
      `dry_run=True`, and show the per-row allocation in the run stats. **No real sends without asking first.**
- [ ] **Step 4: Commit** — `docs(requests): per-row request settings`
