---
paths:
  - "tests/**/*.py"
---

# Testing Conventions

## Structure

- Files: `test_{module}.py`; a module too big for one file splits into `test_{module}_{topic}.py`
  (`test_delivery_rows.py`, `test_pipeline_privacy_order.py`, `test_clients_plextv.py`…), with shared
  helpers in a `*_support.py` beside them
- Classes: `Test{ClassName}` or `Test{FunctionGroup}`
- Methods: a plain sentence naming the behaviour, e.g.
  `test_a_managed_user_absent_from_the_roster_is_read_via_a_switched_token`
- Pattern: Arrange / Act / Assert

## Fixtures (from tests/conftest.py)

- `mock_plex` — Plex client mock (library sections, collections, accounts)
- `mock_plextv` — plex.tv client mock (pins, users, share filters)
- `mock_tmdb` / `mock_curator` — remaining boundary mocks
- `engine_config` — pre-built engine config dataclass with sensible defaults
- `tmp_path` — pytest built-in
- Recorded real API responses live in `tests/fixtures/` (PMS XML, plex.tv XML, TMDB JSON) — prefer
  replaying a recorded fixture over hand-built mock return values.
- Full-stack tests use `tests/fakes/fake_plex.py` (stub PMS + plex.tv server), not mocks.
- **The fake must be no easier than the real server.** It served `<Label>` children inline in the
  collections listing, where a real PMS serves none — so every test proved label identity against a
  shape Plex does not produce, and the production path (plexapi silently re-reading each collection)
  was exercised by nothing. When a fixture records a real response shape, the fake matches it.

## Markers

```python
@pytest.mark.integration  # Crosses module boundaries
@pytest.mark.plex         # Requires a real Plex server (skipped in CI)
@pytest.mark.slow         # Long-running
@pytest.mark.real_migrations  # Needs an empty config dir, not the pre-seeded schema template
@pytest.mark.e2e          # Playwright vs an in-process app (uvicorn + built SPA) + fake_plex
```

## Rules

- External dependencies (Plex, plex.tv, Tautulli, TMDB, LLM providers, filesystem) must always be
  mocked/faked — no test may touch the network
- New functionality requires corresponding tests; privacy/merge code requires property tests
  (hypothesis) — filter parse→merge→serialize must round-trip
- Use `monkeypatch` for env vars, `MagicMock` for objects

## Asserting boundary calls

When a test mocks a downstream call, **assert the kwargs the SUT controls — not just that the call
happened**. (Inherited from a real MPG production bug that hid for months: the test asserted
"called once" but not _with which arguments_ — call count was right, arguments were wrong.)

```python
# BAD — bug-blind: passes regardless of which filters were sent
mock_put.assert_called_once()

# GOOD — asserts the contract the SUT is responsible for
call = mock_put.call_args
assert call.kwargs["params"]["filterMovies"] == "label!=shortlist_sarah,shortlist_mike"
```

Rule of thumb: if removing a parameter from the SUT wouldn't break the test, the test isn't
covering that parameter.

## Cover the matrix, not one cell

When a function branches on the type/state of an input, write tests for **every cell** that
produces different downstream behavior — not just the happy path. Shortlist's recurring branch
variables, each of which needs its full matrix:

- `user_type`: shared / managed / owner
- watch-history token acquisition: owner (admin token) / shared (own roster token) / managed (roster
  miss, switched to a switch-exchanged token) — the one `HistorySource` implementation
  (`ShareTokenWatchSource` in `history.py`) branches on this, not on which history backend is in play; see
  `TestShareTokenWatchSource` in `test_history.py`
- curator provider: anthropic / openai / google / ollama / null
- filter state: empty / shortlist-only / pre-existing-foreign-filters / mixed

If a row would just duplicate another, add a one-liner explaining _why_ they collapse — otherwise
write it.
