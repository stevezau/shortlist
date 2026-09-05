# Orphan-guard fix design (Wave 2 — highest stakes)

From the September 2026 audit — see `.claude/docs/audit-2026-09-programme.md`. This is the one
irreversible write in the codebase: deleting a Plex collection. The audit flagged
`delivery.py:1367`'s `trust_labels` as weaker than `plex-safety.md` rule 4 claims.

## 1. Current behaviour — the full path, in order

`sweep_broken_rows` (`shortlist/engine/delivery.py:1318-1447`), called from `_sweep_phase`
(`pipeline.py:315-344`), the very first write-capable phase of every run (`pipeline.py:117-119`).
`_sweep_phase` wraps the call in `try/except` and **aborts the entire run** (no further writes) if
anything raises — a raised exception is fail-closed at the top level. The interesting failure mode is
a read that *succeeds* but comes back empty, not one that raises.

Guards, in the order they run:

1. **The walk** (`delivery.py:1377-1381`) — for every section, every collection, read
   `collection.labels`. On a real PMS, `<Label>` children are never served in the collection
   *listing*; they arrive only from a per-collection detail read plexapi triggers silently the first
   time `.labels` is accessed (`plex_pms.py:648-661`, pinned by
   `tests/fixtures/pms_collections_listing.json`). So this line is already one extra HTTP round-trip
   per collection, and `labelled_seen` increments once per collection whose label came back non-empty
   **across the entire server** — every `shortlist_*` row included, not just deletion candidates.

2. **Aggregate gate** (`delivery.py:1382-1383`):
   ```python
   orphan_candidates = sum(1 for _s, c, label in walked if label is None and has_marker(c.title))
   trust_labels = labelled_seen > 0 or orphan_candidates <= 1
   ```
   If `not trust_labels`, the whole batch is refused with one log line and **no per-collection work
   happens at all** — `confirm_unlabelled` is never even called (pinned by
   `test_no_labels_at_all_on_a_server_full_of_our_rows_is_a_read_failure`, which asserts
   `confirm_unlabelled.assert_not_called()`).

3. **Per-collection filter** (`delivery.py:1400`) —
   `if not has_marker(collection.title) or not trust_labels: continue`.

4. **Second independent read** (`plex_pms.py:648-677`, `confirm_unlabelled`) — does
   `collection.reload()` explicitly. A raise here is caught and returns `False` (fail-closed: "I don't
   know" never authorises a delete). A clean read finding no `shortlist_*` label returns `True`.

5. **Delete** (`delivery.py:1421-1424` → `plex_pms.py:1483-1496`, `delete_owned_collection`) —
   re-checks `collection.labels` (already refreshed by step 4's `reload()`) and
   `has_shortlist_marker(title)` as ownership proof, strips visibility, then `collection.delete()`.

**Raise vs empty, concretely:** a raise in step 1 aborts the *entire nightly run* before any write
(expensive, safe). A raise in step 4 is caught locally and means "leave it" for that one collection
(cheap, safe). The dangerous case is a read that **succeeds and returns nothing** — the only shape
these guards exist to catch, and exactly where the gap lives.

## 2. Is the audit's gap real, as stated?

**Gap #1 (per-candidate vs global corroboration) is real but overstated in isolation.** Rule 4's prose
— *"if rows of ours exist and NOT ONE reads as labelled, that is a failed read"* — is itself a global
heuristic, and `labelled_seen > 0` faithfully implements it. In the case that matters most (a
multi-row server hit by a genuinely systemic glitch — PMS mid-reindex, a version that stops serving
labels), every real row fails the same way, so `orphan_candidates` grows past 1 alongside
`labelled_seen` staying 0, and the aggregate refusal fires correctly. Gap #1 alone only bites when a
coincidental double-failure hits the *exact same object* twice while unrelated rows read fine — real,
but it needs two independent round-trips to the same collection to both go wrong.

**Gap #2 is the real one, and it is not narrow.** Algebraically, `orphan_candidates <= 1 AND
labelled_seen == 0` can only be true when there is **at most one Shortlist-owned collection anywhere
on the server this pass** — any second marked-but-unlabelled row pushes `orphan_candidates` past 1,
and any labelled row pushes `labelled_seen` past 0. So the bypass fires exactly when there is nothing
on the server to corroborate the read against — the case with the *least* evidence, not the safest to
trust blindly. That is backwards from every other guard here (fail-closed on ambiguity, fail-closed
on a raise).

## 3. The exact failure scenario

A deployment with **exactly one active row**. The documented "Rollout 5 → 15 → 40 users" bootstrap
guarantees this state exists for real, for days, on every install — it is not contrived. That user's
row has been running fine for weeks, genuinely labelled `shortlist_<slug>`, genuinely marked.

Tonight the PMS has one bad moment during the sweep — the cause the code's own comment already names
as real and previously observed (*a PMS mid library-index rebuild*, verified against 1.43.3.10861) —
and answers "no `<Label>`" to both the walk read and the `confirm_unlabelled` re-read a moment later,
for this one collection, even though the label is genuinely still in the PMS's database.

- `labelled_seen = 0` (the only collection, and its read failed)
- `orphan_candidates = 1` (marked, unlabelled)
- `trust_labels = (0 > 0) or (1 <= 1) = True` — the bypass fires with zero corroborating evidence
- `confirm_unlabelled` re-reads → still no label (same glitch, still live) → `True`
- The user's genuine, months-old row is permanently deleted. **The run reports success.** It is
  silently rebuilt tomorrow with different picks — invisible in the UI as anything but "the row
  changed".

This needs no coincidence beyond "one PMS hiccup on a small or early deployment".

## 4. The fix

`config: EngineConfig` is already a required parameter of `sweep_broken_rows` and is **currently
unused in the body** — a clean place for a tunable without touching the signature.

Add to `EngineConfig` (`shortlist/engine/models.py`), following the existing "safe dataclass default,
opinionated `settings_store` product default" pattern used for `refresh_days`/`recency`:

```python
# Delay, in seconds, between the two independent label re-reads sweep_broken_rows demands before
# deleting an unlabelled-but-marked "orphan" — the one irreversible write plex-safety rule 4
# governs. Dataclass default 0 (immediate, existing behaviour) so tests stay fast; settings_store
# defaults the PRODUCT to a real delay. A transient PMS hiccup (mid library-index rebuild) tends to
# clear within seconds; a genuine orphan's label never arrives no matter how long you wait — so
# waiting is real discriminating power a same-instant re-read does not have.
orphan_confirm_delay_s: float = 0.0
```

Replace `delivery.py:1382-1425`:

```python
orphan_candidates = sum(1 for _s, c, label in walked if label is None and has_marker(c.title))
# A read failure this systemic — several rows that are ours by title and NOT ONE reads as labelled —
# is refused outright, with no per-collection work at all (rule 4). A SINGLE candidate with zero
# corroboration used to be waved through here on the theory that one orphan isn't a mass-deletion
# signature — true, but that candidate is numerically IDENTICAL to "the server's only row just had a
# bad read": with nothing else on the server to compare against, a lone properly-labelled row on a
# small deployment reads exactly like a lone fresh orphan. Nothing here can tell them apart from a
# single read any more, so nothing here trusts a single read (see `_confirm_orphan_twice`).
systemic_failure = labelled_seen == 0 and orphan_candidates > 1
if systemic_failure:
    logger.error(
        "the PMS returned NO labels for any of {} collection(s) that are ours by title — treating "
        "that as a failed read, NOT as {} orphans. Nothing will be deleted this pass.",
        orphan_candidates,
        orphan_candidates,
    )

for section, collection, label in walked:
    if label is None:
        if not has_marker(collection.title) or systemic_failure:
            continue
        if not _confirm_orphan_twice(plex, collection, LABEL_PREFIX, config.orphan_confirm_delay_s):
            logger.warning(
                "{}: looked unlabelled but did not survive a second, delayed re-read — NOT deleting it",
                log_title(collection.title),
            )
            continue
        orphan_slug = slug_by_marker.get(collection.title[-64:]) or f"orphan:{marker_account(collection.title)}"
        logger.warning(
            "{}{}: removing an UNLABELLED orphan row in '{}' — no label, so no share filter can "
            "hide it (visible to everyone)",
            "[dry-run] " if dry_run else "",
            orphan_slug,
            section.title,
        )
        title = collection.title
        if not dry_run:
            plex.delete_owned_collection(collection, LABEL_PREFIX)
        deleted.setdefault(orphan_slug, []).append(title)
        continue
    ...  # labelled branch unchanged
```

New helper beside `confirm_unlabelled`'s call sites:

```python
def _confirm_orphan_twice(plex: PlexClient, collection: Collection, label_prefix: str, delay_s: float) -> bool:
    """Two independent 'still no label' answers, `delay_s` apart, before deleting.

    A single `confirm_unlabelled` defeats a one-off flaky read but not a hiccup still live a moment
    later — and having SOME other row on the server read as labelled proves the mechanism works in
    general, not that THIS collection's own two reads are trustworthy. Waiting between them is real
    discriminating power a same-instant re-read does not have.
    """
    if not plex.confirm_unlabelled(collection, label_prefix):
        return False
    if delay_s:
        time.sleep(delay_s)
    return plex.confirm_unlabelled(collection, label_prefix)
```

### On the `<= 1` bypass specifically

`git log -S"orphan_candidates"` shows it was introduced in one commit (`69d4478`, "fix(privacy): a
share filter Plex Web wrote is merged, not corrupted") alongside `confirm_unlabelled` itself, with no
separate rationale in the message — but the test added in the same era,
`test_a_lone_orphan_on_an_otherwise_empty_server_is_still_deleted`
(`tests/unit/test_delivery.py:1123-1136`), states the real reason: *"A fresh install whose first run
died between creating a collection and labelling it: no labelled rows exist to prove the read works,
but ONE orphan is not a mass-deletion signature."*

That case is **preserved** above — it just needs two confirms `delay_s` apart instead of instant,
uncorroborated trust. This is deliberately not a removal: it is a strictly stronger check, so the
legitimate case still works (a genuine orphan's label never appears however long you wait) while the
dangerous case now needs a glitch to survive a real wall-clock gap.

### Recommended addition (not required for the core fix)

Call `plex.demote_all(collection, reason="looks unlabelled — confirming")` immediately after the
*first* `confirm_unlabelled` succeeds, before the delay. `demote_all` (`plex_pms.py:906-926`) is
documented as "monotonically private… needs no privacy gate" and is exactly the existing "unsure →
hide, don't destroy" pattern `_converge_phase` already uses
(`test_an_incomplete_picture_demotes_instead_of_deleting`). This fixes the actual urgency this code
path exists for — an unlabelled row leaks to *everyone* — within the same pass, without needing the
irreversible delete to happen before it is corroborated.

### Honesty check

This sharply reduces but does not eliminate the risk. If a PMS-side fault genuinely persists across
the whole delay (a multi-minute reindex), the row is still deleted, because `sweep_broken_rows` never
persists state across nights. A fully bulletproof version would defer the delete to a second,
separate run — but that needs a persisted "first seen unconfirmed on night X" ledger, which the pure
engine has no DB access to write. Not recommended here: an unlabelled row is an active leak *right
now*, so resolving it within the same run rather than deferring a full day is the better trade-off,
and the in-run delay converts a sub-second race into one that must survive tens of seconds — a large,
cheap risk reduction for a one-line config knob.

## 5. The missing test

### Layer 1 — unit, `tests/unit/test_delivery.py::TestSweepBrokenRows`

The regression test that actually pins the fix (**fails against today's code** —
`confirm_unlabelled.call_count == 2` is false today, per
`test_an_orphan_is_confirmed_against_the_server_before_it_is_deleted`'s existing
`assert_called_once_with`):

```python
def test_confirm_unlabelled_is_required_twice_with_a_real_gap_before_deleting(
    self, engine_config: EngineConfig, movies, shows, monkeypatch
):
    orphan = self._collection(movies, title="✨ Movies Picked for You" + row_marker(202))
    plex = self._plex(movies, shows, orphan)
    plex.matches_section.return_value = True
    plex.confirm_unlabelled.return_value = True
    engine_config.orphan_confirm_delay_s = 30.0
    slept = []
    monkeypatch.setattr("shortlist.engine.delivery.time.sleep", slept.append)

    deleted = sweep_broken_rows(plex, engine_config, markers={"mike": row_marker(202)})

    assert deleted == {"mike": [orphan.title]}
    assert plex.confirm_unlabelled.call_count == 2, "a single confirm must never authorise a delete"
    assert slept == [30.0], "the two confirms must be separated by a real gap, not back-to-back"
```

**`test_an_orphan_is_confirmed_against_the_server_before_it_is_deleted` needs updating** to
`call_count == 2` / `assert_has_calls` once the fix lands — flagging now so it is not mistaken for a
new failure.

Also add, next to `test_a_lone_orphan_on_an_otherwise_empty_server_is_still_deleted` (the case it is
paired against), a test documenting the residual limit honestly — numerically identical inputs
(`labelled_seen=0`, `orphan_candidates=1`) but an established, genuinely labelled row hit by a
systemic glitch on both reads. Against today's code and against the fix it still deletes if the glitch
survives the full delay; the test's value is stating in the codebase that nothing in a single pass can
tell the two apart, rather than asserting a guarantee a delay-only fix cannot provide.

### Layer 2 — the actually-missing end-to-end test, `tests/integration/test_engine_vs_fake.py`

That file proves `confirm_unlabelled`/`owned_row_surfaces` against the real fake HTTP server
(`test_confirm_unlabelled_says_yes_for_a_genuinely_unlabelled_one`, line 2410) and calls `engine_run`
dozens of times for privacy/promotion scenarios — but **never once plants an orphan and runs
`engine_run` over it**.

```python
def test_engine_run_deletes_a_genuine_unlabelled_orphan_end_to_end(fakes, tmp_path):
    """Plants a real interrupted-run orphan directly on the fake PMS — marked title, no label,
    exactly what a crash between create() and the label write leaves behind — and proves the REAL
    PlexClient, talking to a server that serves no <Label> in the listing (the real-PMS shape
    `pms_collections_listing.json` records), correctly identifies and removes it through the actual
    pipeline entrypoint. Nothing today runs sweep_broken_rows against anything but a MagicMock; this
    is the only place plexapi's lazy per-collection reload, `confirm_unlabelled`'s fresh re-read and
    the aggregate guard all run together against the shape Plex actually serves.
    """
    state, pms_url, _tmdb = fakes
    orphan_key = 9200
    state.collections[orphan_key] = FakeCollection(
        rating_key=orphan_key,
        title="✨ Movies Picked for You" + row_marker(202),  # mike's marker, no label
        section_id=state.section_id,
        labels=[],
    )
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=5, min_history=5, candidates_pre_rank=10, max_seeds=5),
        plex=plex, plextv=plextv, tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(), snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={202: "mike"},
    )
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    report = engine_run(ctx, [mike])

    assert report.swept_rows.get("mike") == ["✨ Movies Picked for You" + row_marker(202)]
    assert orphan_key not in state.collections
```

This **passes against today's code** — there is no bug in the *pure genuine-orphan* case; that is
precisely what the audit means by "decision logic is proven against mocks, but nothing plants a
genuine orphan and runs the pipeline end to end". Its value is as a regression harness: once
`_confirm_orphan_twice` lands, it still passes unchanged with `orphan_confirm_delay_s` at the
dataclass default of 0, proving the fix does not break real end-to-end orphan cleanup through the
PMS-shape-accurate path — which no mock-only test can promise.

## 6. Risk and rollback

- **Blast radius is small**: one new dataclass field (default `0.0`, so existing tests and runtime
  behaviour are unchanged until a non-zero delay is configured), one small helper, and a handful of
  `test_delivery.py` assertions moving from `assert_called_once_with` to `call_count == 2`. No
  migration, no schema change, no API surface change unless `orphan_confirm_delay_s` is also wired
  into `settings_store`/`/api/settings` (recommended for parity with `plextv.throttle_s`, but not
  required to land the safety fix — a `settings_store` product default of 30–60s is enough).
- **Mandatory Architecture Review before commit** per `.claude/CLAUDE.md`'s risk list: this touches
  the Plex write path and the orphan-deletion path specifically.
- **Never verify against SFLIX or any real server without explicit consent.** Verification with zero
  real-server risk: `pytest tests/unit/test_delivery.py tests/integration/test_engine_vs_fake.py -q`
  — everything here is fake/mock-backed. If real confirmation is wanted later it goes through
  `shortlist run --user <slug> --dry-run` and, since this path is about *deletion*, should be
  rehearsed against a disposable canary collection, never a real user's row, and only with an
  explicit go-ahead.
- **Rollback**: with `orphan_confirm_delay_s` at its dataclass default, the fix reproduces today's
  single-confirm timing exactly (minus the removed `<= 1` bypass). If the delayed-confirm logic itself
  needs reverting, it is one self-contained function (`_confirm_orphan_twice`) plus the one block
  replacing lines 1382-1425 — revertible as a unit without touching anything else in the file.

## Files read for this design (read-only, no edits)

`shortlist/engine/delivery.py` (1318-1447), `shortlist/engine/clients/plex_pms.py` (483-757,
890-928, 1483-1496), `shortlist/engine/pipeline.py` (74-200, 315-344, 1196-1335),
`shortlist/engine/models.py` (1027-1120), `tests/unit/test_delivery.py` (938-1170),
`tests/unit/test_pipeline.py` (3834-3966 — confirmed it tests a *different* orphan concept,
`_converge_phase`), `tests/integration/test_engine_vs_fake.py` (130-200, 1380-1460, 2380-2470),
`tests/fakes/fake_plex.py`, and `git log -S"orphan_candidates" -- shortlist/engine/delivery.py`
(commit `69d4478`).
