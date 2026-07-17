---
globs: "tests/**/*.py"
---

# Testing Conventions

## Structure

- Files: `test_{module}.py`
- Classes: `Test{ClassName}` or `Test{FunctionGroup}`
- Methods: `test_{behavior}_when_{condition}`
- Pattern: Arrange / Act / Assert

## Fixtures (from tests/conftest.py)

- `mock_plex` — Plex client mock (library sections, collections, accounts)
- `mock_plextv` — plex.tv client mock (pins, users, share filters)
- `mock_tautulli` / `mock_tmdb` / `mock_curator` — remaining boundary mocks
- `engine_config` — pre-built engine config dataclass with sensible defaults
- `tmp_path` — pytest built-in
- Recorded real API responses live in `tests/fixtures/` (PMS XML, plex.tv XML, TMDB JSON) — prefer
  replaying a recorded fixture over hand-built mock return values.
- Full-stack tests use `tests/fakes/fake_plex.py` (stub PMS + plex.tv server), not mocks.

## Markers

```python
@pytest.mark.integration  # Crosses module boundaries
@pytest.mark.plex         # Requires a real Plex server (skipped in CI)
@pytest.mark.slow         # Long-running
@pytest.mark.e2e          # Playwright vs built image + fake_plex
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
- history source: tautulli / plex
- curator provider: anthropic / openai / google / ollama / null
- filter state: empty / shortlist-only / pre-existing-foreign-filters / mixed

If a row would just duplicate another, add a one-liner explaining _why_ they collapse — otherwise
write it.
