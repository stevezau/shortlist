# AI instructions on any row (discussion #138, phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner tell AI web search what to look for: a server-wide default in Settings, and per row "Use the default / Add to the default / Write your own", with the prompt's mechanics locked.

**Architecture:** A new pure engine module (`engine/web_guidance.py`) resolves a row's instructions plus the server-wide text into a `Guidance` (replace / extra). The three web prompt templates in `curator/base.py` are split into head + guidance + tail so the built-in output stays byte-identical and owner text is spliced in after `.format()`. The row's `Guidance` rides `RowPolicy.pools_for` → `_candidate_pool` → `gather_candidates` → `web_recommendations` → the prompt builders, and splits `pool_key` and the row recipe only when it is not built-in. The server stores a row's instructions in the dead `Collection.prompt` JSON column (cleared to `{}` by migration 0036, so no migration) and the default in a new `llm_web.instructions` setting. The SPA adds one field to the row editor's What goes in section and one to Settings → Defaults → Title sources → Web search.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Pydantic v2, pytest; React 19 + TypeScript + Tailwind, vitest.

**Spec:** `.claude/docs/discussion-138-ai-custom-rows.md` (Part A, "Where it sits in the redesigned app", Decisions 1, 2, 8). Mockups: boards A1 and A2 of https://claude.ai/artifact/Y8CMBv8UQYbVTgFmE8cnwC.

## Global Constraints

- With no instructions anywhere, every prompt, pool key and row recipe is byte-identical to today (decision: new settings default to today's behaviour).
- Modes are exactly `"default"`, `"add"`, `"own"`. Instruction text is at most 2000 characters, row and server-wide.
- Owner text may use only `{count}` and `{year}`; every other brace is kept as typed and must never reach `str.format`.
- Locked (never owner-editable): today's year, "search before answering", exact title and release year, released-only, the reply format.
- No per-person instructions (decision 2). `CollectionUserOverride.prompt` stays dead.
- `shortlist/engine/` must not import from `shortlist/server/`.
- Code style: ruff (120 cols), type hints everywhere, Google docstrings on public APIs, `from loguru import logger`.
- One pytest at a time on this host. During a task run only that task's test file(s); the full suites run once, in Task 7.
- Do NOT commit. Stage nothing. The owner approves commits at the end (global CLAUDE.md: "Ask before committing to git").

## Review Focus

1. Owner text containing `{`, `}`, `{k}` or `{0}` must not raise and must reach the model exactly as typed (only `{count}` and `{year}` are filled). Test in Task 1.
2. Upgrade night: a row with stored `prompt == {}` and no server-wide text keeps its recipe and pool key, so nothing rebuilds. Test in Task 2.
3. Legacy or hand-edited stored values (`None`, `[]`, `{"tone": "x"}`, `{"mode": "own", "text": "   "}`) read as "no instructions" and never crash serialisation or a run. Tests in Tasks 1 and 3.
4. Exa with no AI provider: the instructions cannot apply (Exa's titles are used as found). The engine makes no AI call and the editor says so. Tests in Tasks 2 and 4.
5. Two rows identical except for instructions get separate pools (each pays its own AI call), and a row that does not use AI web search is untouched by any instructions. Test in Task 2.

---

## File structure

- Create `shortlist/engine/web_guidance.py`: `AiInstructions`, `Guidance`, `BUILTIN`, `resolve_guidance`, `render_owner_text`, `INSTRUCTION_MODES`, `MAX_INSTRUCTIONS_CHARS`.
- Modify `shortlist/engine/curator/base.py`: split the three templates; builders take `guidance`; add `web_system_prompt` and `builtin_guidance`.
- Modify `shortlist/engine/curator/anthropic.py`, `openai.py`, `google.py` (and the curator base class/protocol that declares `recommend_web`): `recommend_web(..., *, guidance=None)`.
- Modify `shortlist/engine/candidates.py`: `gather_candidates(..., web_guidance=None)`, `web_recommendations(..., guidance=None)`, `_web_via_search(..., guidance=None)`; public `LLM_WEB_K`.
- Modify `shortlist/engine/models.py`: `RowSpec.ai_instructions`, `EngineConfig.web_instructions`.
- Modify `shortlist/engine/rows.py`: `RowPolicy.effective_guidance`, `pool_key`, `row_recipe`, `pools_for` (pool label + pass-through), `_candidate_pool`.
- Modify `shortlist/server/settings_store.py`, `shortlist/server/api/settings.py`: the `llm_web.instructions` setting.
- Modify `shortlist/server/api/collections.py`: `ai_instructions` in/out/validate/create/patch/audit.
- Modify `shortlist/server/services/context_builder.py`: map to `RowSpec` / `EngineConfig`.
- Create `shortlist/server/api/ai.py`; modify `shortlist/server/main.py`: `POST /api/ai/web-prompt-preview`.
- Modify `shortlist/server/db/models.py`: the `Collection.prompt` comment.
- Web: `lib/api-schema.d.ts` (generated), `lib/api.ts`, `lib/collections.ts`, `lib/row-kinds.ts`, `components/rows/row-draft-diff.ts`, `components/rows/row-contents-fields.tsx`, create `components/rows/row-ai-instructions-field.tsx`, `components/settings/recommendations-section.tsx`, `lib/sources.ts`, `components/runs/run-stat-tiles.tsx`.
- Docs: `docs/guides/rows.md`, `docs/reference/settings.md`, `docs/reference/api.md`, `docs/llms-full.txt` (generated), `CHANGELOG.md`, `docs/feed.xml` (generated, if the changelog edit touches a feed entry), `PRODUCT.md`, `.claude/CLAUDE.md`.

---

### Task 1: Engine guidance model and byte-identical prompt split

**Files:**
- Create: `shortlist/engine/web_guidance.py`
- Modify: `shortlist/engine/curator/base.py:99-215`
- Test: `tests/unit/test_web_guidance.py` (new), `tests/unit/test_web_prompt.py` (extend)

**Interfaces:**
- Produces: `AiInstructions(mode: str = "default", text: str = "")` with `AiInstructions.from_stored(value: object) -> AiInstructions | None`; `Guidance(replace: str = "", extra: str = "")` with property `is_builtin: bool` and `fingerprint() -> str` ("" when built-in); `BUILTIN: Guidance`; `resolve_guidance(row: AiInstructions | None, server_text: str) -> Guidance`; `render_owner_text(text: str, *, k: int, year: int) -> str`; `INSTRUCTION_MODES = ("default", "add", "own")`; `MAX_INSTRUCTIONS_CHARS = 2000`.
- Produces in `curator/base.py`: `build_web_prompt(profile, seeds, k, *, year=None, guidance=None)`, `build_web_rag_prompt(profile, results, k, *, year=None, guidance=None)`, `build_web_pick_prompt(profile, candidates, k, *, year=None, guidance=None)`, `web_system_prompt(backend: str, *, k: int, year: int, guidance: Guidance | None) -> str` (backend `"native"` → native template, `"exa"` → pick template, `"searxng"` → RAG template, anything else → native), `builtin_guidance(backend: str, *, k: int, year: int) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_guidance.py`:

```python
"""Owner-written guidance for AI web search (discussion #138)."""

import pytest

from shortlist.engine.web_guidance import (
    BUILTIN,
    AiInstructions,
    Guidance,
    render_owner_text,
    resolve_guidance,
)


class TestFromStored:
    @pytest.mark.parametrize(
        "stored",
        [
            None,
            {},
            [],
            "own",
            {"tone": "warm"},
            {"mode": "own", "text": "   "},
            {"mode": "default", "text": "x"},
            {"mode": "bogus", "text": "x"},
            {"mode": "add", "text": 7},
        ],
    )
    def test_anything_unrecognised_reads_as_no_instructions(self, stored):
        assert AiInstructions.from_stored(stored) is None

    def test_add_and_own_round_trip_with_text_trimmed(self):
        assert AiInstructions.from_stored({"mode": "add", "text": "  Only French films. "}) == AiInstructions(
            "add", "Only French films."
        )
        assert AiInstructions.from_stored({"mode": "own", "text": "Any decade."}) == AiInstructions(
            "own", "Any decade."
        )


class TestResolveGuidance:
    def test_no_row_and_no_server_text_is_builtin(self):
        assert resolve_guidance(None, "") == BUILTIN
        assert resolve_guidance(None, "   ") == BUILTIN
        assert BUILTIN.is_builtin and BUILTIN.fingerprint() == ""

    def test_server_text_replaces_the_builtin_for_rows_without_their_own(self):
        assert resolve_guidance(None, " Favour classics. ") == Guidance(replace="Favour classics.")

    def test_add_keeps_the_server_text_and_adds_the_rows(self):
        assert resolve_guidance(AiInstructions("add", "No kids films."), "") == Guidance(extra="No kids films.")
        assert resolve_guidance(AiInstructions("add", "No kids films."), "Favour classics.") == Guidance(
            replace="Favour classics.", extra="No kids films."
        )

    def test_own_replaces_whatever_the_server_says(self):
        assert resolve_guidance(AiInstructions("own", "Any decade."), "Favour classics.") == Guidance(
            replace="Any decade."
        )

    def test_fingerprint_differs_by_content_and_is_short(self):
        a, b = Guidance(extra="x").fingerprint(), Guidance(replace="x").fingerprint()
        assert a and b and a != b and len(a) == 16


class TestRenderOwnerText:
    def test_only_count_and_year_are_filled(self):
        assert render_owner_text(" Give {count}, from {year}. ", k=40, year=2026) == "Give 40, from 2026."

    def test_every_other_brace_is_kept_as_typed(self):
        text = 'Use {k} and {0} and {{x}} and {"a": 1} and a lone { brace'
        assert render_owner_text(text, k=40, year=2026) == text
```

Extend `tests/unit/test_web_prompt.py` (append; keep every existing test). The three `EXPECTED_*` strings are the CURRENT templates formatted by hand, copied verbatim from `curator/base.py:99-164` before any edit (they pin byte-identity):

```python
from shortlist.engine.curator.base import (
    build_web_pick_prompt,
    build_web_rag_prompt,
    builtin_guidance,
    web_system_prompt,
)
from shortlist.engine.web_guidance import BUILTIN, Guidance

EXPECTED_NATIVE_2026_K40 = (
    "You are a film and TV recommender with live web search. Today is in 2026, which is LATER than "
    "your training cutoff — so your own knowledge of what is new is out of date. Search the web "
    "before answering rather than recommending from memory. Search for what 'what to watch next' "
    "articles, critics' best-of lists and review sites are recommending in 2026 and 2025. "
    "Based on what this person recently watched, give 40 titles they'd most likely want to watch "
    "next. Strongly prefer titles released in 2025 or 2026; include something older only "
    "when it is an unusually good match for their taste. Rules: (1) never recommend a title they "
    "already watched, or another season or sequel of one; (2) ALWAYS give the exact release year — "
    "it is used to look the title up, and a missing year means the recommendation is discarded; "
    "(3) use the exact title as released, not a description of it; (4) only titles ALREADY RELEASED "
    "and watchable now — never announced, upcoming or unaired ones; (5) name a series by its series "
    "title alone, never 'Season 2' or 'Part 3'. Prefer real, findable titles over "
    'obscure guesses. Respond with ONLY a JSON array of up to 40 objects, each {"title": str, '
    '"year": int, "media": "movie" or "show"}. No prose.'
)
EXPECTED_RAG_K40 = (
    "You are a film and TV recommender. Below are excerpts from recent web articles about what to "
    "watch. Based on what this person recently enjoyed, pick the 40 titles mentioned in these "
    "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles. "
    "Give the exact release year wherever the article states it — it is used to look the title up. "
    'Respond with ONLY a JSON array of up to 40 objects, each {"title": str, "year": int or null, '
    '"media": "movie" or "show"}. No prose.'
)
EXPECTED_PICK_K40 = (
    "You are a film and TV recommender. Below is a list of titles that recent web articles "
    "recommend as things to watch next. Based on what this person recently enjoyed, "
    "pick the 40 they'd most likely want to watch next. Choose only from the list — do not add "
    "titles of your own. Keep each title and year exactly as written; they are used to look the "
    'title up. Respond with ONLY a JSON array of up to 40 objects, each {"title": str, "year": '
    'int or null, "media": "movie" or "show"}. No prose.'
)


class TestBuiltinPromptsAreByteIdentical:
    """No instructions anywhere must send exactly what this source always sent (#138 global constraint)."""

    def test_native(self, profile):
        assert build_web_prompt(profile, [], 40, year=2026)[0] == EXPECTED_NATIVE_2026_K40
        assert build_web_prompt(profile, [], 40, year=2026, guidance=BUILTIN)[0] == EXPECTED_NATIVE_2026_K40

    def test_rag_and_pick(self, profile):
        assert build_web_rag_prompt(profile, [], 40)[0] == EXPECTED_RAG_K40
        assert build_web_pick_prompt(profile, [], 40)[0] == EXPECTED_PICK_K40

    def test_preview_matches_the_builders(self):
        assert web_system_prompt("native", k=40, year=2026, guidance=None) == EXPECTED_NATIVE_2026_K40
        assert web_system_prompt("searxng", k=40, year=2026, guidance=None) == EXPECTED_RAG_K40
        assert web_system_prompt("exa", k=40, year=2026, guidance=None) == EXPECTED_PICK_K40


class TestOwnerGuidance:
    def test_add_keeps_the_builtin_guidance_and_appends_the_rows_text(self):
        system = web_system_prompt("exa", k=40, year=2026, guidance=Guidance(extra="Nothing aimed at kids."))
        assert "Based on what this person recently enjoyed, pick the 40" in system
        assert "The server owner adds, for this row: Nothing aimed at kids. Choose only from the list" in system

    def test_replace_drops_the_builtin_guidance_but_keeps_every_mechanic(self):
        system = web_system_prompt("native", k=40, year=2026, guidance=Guidance(replace="Films from any decade."))
        assert "Strongly prefer titles released in" not in system
        assert "Films from any decade. Give up to 40 titles." in system
        for locked in (
            "Today is in 2026",
            "Search the web",
            "ALWAYS give the exact release year",
            "ALREADY RELEASED",
            "Respond with ONLY a JSON array of up to 40 objects",
        ):
            assert locked in system

    def test_replace_on_rag_and_pick_keeps_the_list_constraint(self):
        rag = web_system_prompt("searxng", k=40, year=2026, guidance=Guidance(replace="Any decade."))
        pick = web_system_prompt("exa", k=40, year=2026, guidance=Guidance(replace="Any decade."))
        assert "Pick up to 40 of the titles mentioned in these articles." in rag
        assert "Pick up to 40 of them." in pick and "Choose only from the list" in pick

    def test_braces_in_owner_text_never_reach_format(self, profile):
        system, _ = build_web_pick_prompt(profile, [], 40, guidance=Guidance(extra='Use {k} and {"a": 1}.'))
        assert 'Use {k} and {"a": 1}.' in system

    def test_builtin_guidance_is_the_sentence_an_owner_would_replace(self):
        assert builtin_guidance("native", k=40, year=2026).startswith(
            "Based on what this person recently watched, give 40 titles"
        )
        assert builtin_guidance("exa", k=40, year=2026) == (
            "Based on what this person recently enjoyed, pick the 40 they'd most likely want to watch next."
        )
```

If `test_web_prompt.py` has no `profile` fixture, add one at the top of the new block that builds the same minimal `UserProfile` the existing tests in that file use (copy their construction).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_web_guidance.py tests/unit/test_web_prompt.py -q -p no:randomly`
Expected: FAIL with `ModuleNotFoundError: No module named 'shortlist.engine.web_guidance'` / `ImportError` for `web_system_prompt`.

- [ ] **Step 3: Create `shortlist/engine/web_guidance.py`**

```python
"""Owner-written guidance for the AI web search source (discussion #138).

The built-in web prompts (`curator/base.py`) mix two kinds of text. MECHANICS make an answer usable: today's
year, "search before answering", the exact title and release year, released titles only, the reply format.
GUIDANCE says what to favour. An owner may replace the guidance server-wide, and a row may add to it or
replace it; the mechanics are never theirs to change, because each one is load-bearing (see the comments
above `_WEB_SYSTEM`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

INSTRUCTION_MODES = ("default", "add", "own")
MAX_INSTRUCTIONS_CHARS = 2000


@dataclass(frozen=True)
class AiInstructions:
    """One row's instructions, as the owner saved them.

    Attributes:
        mode: "default" (use the server's guidance), "add" (append to it) or "own" (replace it).
        text: The owner's words. Empty for "default".
    """

    mode: str = "default"
    text: str = ""

    @classmethod
    def from_stored(cls, value: object) -> AiInstructions | None:
        """Parse a row's stored JSON. Anything unrecognised reads as "no instructions".

        The column held curation settings until migration 0036 cleared it to ``{}``; a value this code does
        not recognise must never take a row, or a run, down.

        Args:
            value: The stored JSON value of the row's ``prompt`` column.

        Returns:
            The row's instructions, or None when it has none in effect.
        """
        if not isinstance(value, dict):
            return None
        mode, text = value.get("mode"), value.get("text")
        if mode not in ("add", "own") or not isinstance(text, str) or not text.strip():
            return None
        return cls(mode=mode, text=text.strip())


@dataclass(frozen=True)
class Guidance:
    """The guidance a prompt is built with.

    Attributes:
        replace: Text that replaces the built-in guidance. "" keeps it.
        extra: Text appended after the guidance. "" adds nothing.
    """

    replace: str = ""
    extra: str = ""

    @property
    def is_builtin(self) -> bool:
        """True when the prompt is exactly the one this source has always sent."""
        return not self.replace and not self.extra

    def fingerprint(self) -> str:
        """A short stable hash for pool keys and row recipes; "" when built-in, so nothing else changes."""
        if self.is_builtin:
            return ""
        return hashlib.blake2b(f"{self.replace}\x00{self.extra}".encode(), digest_size=8).hexdigest()


BUILTIN = Guidance()


def resolve_guidance(row: AiInstructions | None, server_text: str) -> Guidance:
    """Combine a row's instructions with the server-wide text.

    Args:
        row: The row's instructions, or None.
        server_text: The ``llm_web.instructions`` setting. "" means Shortlist's built-in guidance.

    Returns:
        The guidance for this row's prompts.
    """
    server = (server_text or "").strip()
    if row is None or row.mode not in ("add", "own") or not row.text.strip():
        return Guidance(replace=server)
    if row.mode == "own":
        return Guidance(replace=row.text.strip())
    return Guidance(replace=server, extra=row.text.strip())


def render_owner_text(text: str, *, k: int, year: int) -> str:
    """Fill ``{count}`` and ``{year}``. Every other brace is kept exactly as typed.

    Owner text is never passed through ``str.format``: a stray brace would raise, and ``{k}`` would be
    filled with a value the owner never meant.
    """
    return text.strip().replace("{count}", str(k)).replace("{year}", str(year))
```

- [ ] **Step 4: Split the templates in `shortlist/engine/curator/base.py`**

Replace the three template constants (keep the explanatory comments above `_WEB_SYSTEM` where they are) with parts whose concatenation is the old literal, and route the builders through one function. Exact strings:

```python
from typing import NamedTuple

from shortlist.engine.web_guidance import BUILTIN, Guidance, render_owner_text


class _WebPrompt(NamedTuple):
    """A web system prompt as mechanics around one guidance passage (#138).

    ``head + guide + tail`` is the built-in prompt, byte for byte. ``count`` restates how many titles to
    give when an owner's text replaces ``guide``, which is where the built-in states it.
    """

    head: str
    guide: str
    tail: str
    count: str


_WEB = _WebPrompt(
    head=(
        "You are a film and TV recommender with live web search. Today is in {year}, which is LATER than "
        "your training cutoff — so your own knowledge of what is new is out of date. Search the web "
        "before answering rather than recommending from memory. Search for what 'what to watch next' "
        "articles, critics' best-of lists and review sites are recommending in {year} and {last_year}. "
    ),
    guide=(
        "Based on what this person recently watched, give {k} titles they'd most likely want to watch "
        "next. Strongly prefer titles released in {last_year} or {year}; include something older only "
        "when it is an unusually good match for their taste. "
    ),
    tail=(
        "Rules: (1) never recommend a title they "
        "already watched, or another season or sequel of one; (2) ALWAYS give the exact release year — "
        "it is used to look the title up, and a missing year means the recommendation is discarded; "
        "(3) use the exact title as released, not a description of it; (4) only titles ALREADY RELEASED "
        "and watchable now — never announced, upcoming or unaired ones; (5) name a series by its series "
        "title alone, never 'Season 2' or 'Part 3'. Prefer real, findable titles over "
        'obscure guesses. Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, '
        '"year": int, "media": "movie" or "show"}}. No prose.'
    ),
    count="Give up to {k} titles. ",
)
_WEB_SYSTEM = _WEB.head + _WEB.guide + _WEB.tail

_WEB_RAG = _WebPrompt(
    head="You are a film and TV recommender. Below are excerpts from recent web articles about what to watch. ",
    guide=(
        "Based on what this person recently enjoyed, pick the {k} titles mentioned in these "
        "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles. "
    ),
    tail=(
        "Give the exact release year wherever the article states it — it is used to look the title up. "
        'Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, "year": int or null, '
        '"media": "movie" or "show"}}. No prose.'
    ),
    count="Pick up to {k} of the titles mentioned in these articles. ",
)
_WEB_RAG_SYSTEM = _WEB_RAG.head + _WEB_RAG.guide + _WEB_RAG.tail

_WEB_PICK = _WebPrompt(
    head=(
        "You are a film and TV recommender. Below is a list of titles that recent web articles "
        "recommend as things to watch next. "
    ),
    guide="Based on what this person recently enjoyed, pick the {k} they'd most likely want to watch next. ",
    tail=(
        "Choose only from the list — do not add "
        "titles of your own. Keep each title and year exactly as written; they are used to look the "
        'title up. Respond with ONLY a JSON array of up to {k} objects, each {{"title": str, "year": '
        'int or null, "media": "movie" or "show"}}. No prose.'
    ),
    count="Pick up to {k} of them. ",
)
_WEB_PICK_SYSTEM = _WEB_PICK.head + _WEB_PICK.guide + _WEB_PICK.tail

_OWNER_ADDS = "The server owner adds, for this row: "
_PROMPT_FOR_BACKEND = {"native": _WEB, "exa": _WEB_PICK, "searxng": _WEB_RAG}


def _web_system(prompt: _WebPrompt, *, k: int, year: int, guidance: Guidance | None) -> str:
    """One web system prompt. Built-in guidance gives exactly the text this source has always sent.

    Owner text is spliced in AFTER ``str.format`` runs on Shortlist's own parts, so its braces are inert.
    """
    g = guidance or BUILTIN
    fmt = {"k": k, "year": year, "last_year": year - 1}
    if g.replace:
        middle = render_owner_text(g.replace, k=k, year=year) + " " + prompt.count.format(**fmt)
    else:
        middle = prompt.guide.format(**fmt)
    if g.extra:
        middle += _OWNER_ADDS + render_owner_text(g.extra, k=k, year=year) + " "
    return prompt.head.format(**fmt) + middle + prompt.tail.format(**fmt)


def web_system_prompt(backend: str, *, k: int, year: int, guidance: Guidance | None) -> str:
    """The system prompt AI web search sends for ``backend`` ("native", "exa", "searxng").

    Exa is shown with its pick prompt, the one used whenever Exa extracts titles. An unknown backend is
    treated as native, as ``web_recommendations`` does.
    """
    return _web_system(_PROMPT_FOR_BACKEND.get(backend, _WEB), k=k, year=year, guidance=guidance)


def builtin_guidance(backend: str, *, k: int, year: int) -> str:
    """The built-in guidance passage an owner's text would replace, for showing in Settings."""
    return _PROMPT_FOR_BACKEND.get(backend, _WEB).guide.format(k=k, year=year, last_year=year - 1).strip()
```

Then change the three builders (keep their docstrings; add the new args to them):

- `build_web_prompt(profile, seeds, k, *, year=None, guidance=None)`: replace `system = _WEB_SYSTEM.format(k=k, year=now, last_year=now - 1)` with `system = _web_system(_WEB, k=k, year=now, guidance=guidance)`.
- `build_web_rag_prompt(profile, results, k, *, year=None, guidance=None)`: `now = year if year is not None else datetime.now(UTC).year`; `system = _web_system(_WEB_RAG, k=k, year=now, guidance=guidance)`.
- `build_web_pick_prompt(profile, candidates, k, *, year=None, guidance=None)`: same, with `_WEB_PICK`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/unit/test_web_guidance.py tests/unit/test_web_prompt.py tests/unit/test_trace_capture.py -q -p no:randomly`
Expected: PASS (every existing test in `test_web_prompt.py` and `test_trace_capture.py` included).

- [ ] **Step 6: Lint**

Run: `ruff check shortlist/engine/web_guidance.py shortlist/engine/curator/base.py tests/unit/test_web_guidance.py tests/unit/test_web_prompt.py && ruff format --check shortlist/engine/web_guidance.py shortlist/engine/curator/base.py`
Expected: no findings.

---

### Task 2: Carry a row's guidance through the engine

**Files:**
- Modify: `shortlist/engine/models.py` (`RowSpec` ~445, `EngineConfig` ~1290)
- Modify: `shortlist/engine/rows.py` (`RowPolicy.pool_key` 2325-2370, `pools_for` 2374-2440, `_candidate_pool` 1266-1330, `row_recipe` 957-1026, add `RowPolicy.effective_guidance`)
- Modify: `shortlist/engine/candidates.py` (`gather_candidates` 811, `web_recommendations` 160, `_web_via_search` 257, the call at 1017, and rename-or-alias `_LLM_WEB_K` → public `LLM_WEB_K`)
- Modify: the curator class that declares `recommend_web` (in `shortlist/engine/curator/base.py` or `__init__.py`), `anthropic.py:72`, `openai.py:125`, `google.py:124`
- Test: `tests/unit/test_candidates.py`, `tests/unit/test_pipeline.py`, new `tests/unit/test_row_guidance.py`

**Interfaces:**
- Consumes: Task 1's `AiInstructions`, `Guidance`, `BUILTIN`, `resolve_guidance`, builders' `guidance=` kwarg.
- Produces: `RowSpec.ai_instructions: AiInstructions | None = None`; `EngineConfig.web_instructions: str = ""`; `RowPolicy.effective_guidance(spec: RowSpec) -> Guidance` (returns `BUILTIN` when "llm_web" is not in `effective_sources(spec)`); `gather_candidates(..., web_guidance: Guidance | None = None)`; `web_recommendations(..., guidance: Guidance | None = None)`; `Curator.recommend_web(profile, seeds, k, *, guidance: Guidance | None = None)`; `candidates.LLM_WEB_K: int`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_row_guidance.py`. Build `RowPolicy`/`ctx` the way `tests/unit/test_seasonal_rows.py::TestWhatDecidesARebuild` (line ~651) does; copy its helpers into this file rather than importing them. The tests to write:

```python
"""A row's AI instructions decide its pool and recipe only when it uses AI web search (#138)."""

from shortlist.engine.models import RowSpec
from shortlist.engine.rows import row_recipe
from shortlist.engine.web_guidance import AiInstructions

# `make_policy(cfg_overrides=..., sources=...)` is the helper copied from test_seasonal_rows.py, returning a
# RowPolicy for one person whose effective sources are `sources`.


class TestNothingChangesWithoutInstructions:
    def test_a_row_with_no_instructions_keeps_its_recipe_and_pool_key_byte_for_byte(self):
        policy = make_policy(sources=["tmdb_similar", "llm_web"])
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        explicit_default = RowSpec(slug="plain", name_template="Plain", size=5, ai_instructions=None)
        assert row_recipe(policy, plain) == row_recipe(policy, explicit_default)
        assert "guide=" not in row_recipe(policy, plain)
        assert policy.pool_key(plain) == policy.pool_key(explicit_default)

    def test_instructions_on_a_row_without_ai_web_search_change_nothing(self):
        policy = make_policy(sources=["tmdb_similar"])
        plain = RowSpec(slug="r", name_template="R", size=5)
        told = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("own", "Any decade."))
        assert row_recipe(policy, told) == row_recipe(policy, plain)
        assert policy.pool_key(told) == policy.pool_key(plain)


class TestInstructionsSplitTheirRow:
    def test_two_rows_that_differ_only_in_instructions_get_separate_pools(self):
        policy = make_policy(sources=["tmdb_similar", "llm_web"])
        a = RowSpec(slug="a", name_template="A", size=5)
        b = RowSpec(slug="b", name_template="B", size=5, ai_instructions=AiInstructions("add", "No kids films."))
        assert policy.pool_key(a) != policy.pool_key(b)

    def test_changing_a_rows_instructions_changes_its_recipe(self):
        policy = make_policy(sources=["llm_web"])
        one = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("add", "One."))
        two = RowSpec(slug="r", name_template="R", size=5, ai_instructions=AiInstructions("add", "Two."))
        assert row_recipe(policy, one) != row_recipe(policy, two)
        assert row_recipe(policy, one).endswith("guide=" + policy.effective_guidance(one).fingerprint())

    def test_server_wide_text_changes_every_ai_web_search_rows_recipe(self):
        plain = RowSpec(slug="r", name_template="R", size=5)
        assert row_recipe(
            make_policy(sources=["llm_web"], cfg_overrides={"web_instructions": "Favour classics."}), plain
        ) != (row_recipe(make_policy(sources=["llm_web"]), plain))
```

In `tests/unit/test_candidates.py`:
- Give every fake that defines `recommend_web(self, profile, seeds, k)` (lines ~145, 246, 279, 349, 1052, 1110, 1403, 1681) the signature `recommend_web(self, profile, seeds, k, *, guidance=None)` (or `*a, **kw` where it already takes `*a`), and do the same for `tests/unit/test_pipeline.py:2825`.
- Make `_NonNativeCurator` (~324) also record `self.last_system = system` in `complete`, and `_NativeCurator` (~340) record `self.last_guidance = guidance`.
- Add to `TestLlmWebBackends` (~358):

```python
def test_a_rows_guidance_reaches_the_exa_pick_prompt(self):
    curator = _NonNativeCurator(reply='[{"title": "Dune", "year": 2021, "media": "movie"}]')
    gather_candidates(
        _tmdb_with_dune(),  # use the fixture/helper these tests already use for an Exa-extracting gather
        seeds=[_seed()],
        sources=["llm_web"],
        curator=curator,
        profile=_profile(),
        search=_FakeExtractingSearch(titles=[("Dune", 2021, "movie")]),
        web_search_mode="exa",
        web_guidance=Guidance(extra="Nothing aimed at kids."),
    )
    assert "The server owner adds, for this row: Nothing aimed at kids." in curator.last_system


def test_native_search_is_handed_the_rows_guidance(self):
    curator = _NativeCurator(...)  # as the existing native tests build it
    g = Guidance(replace="Any decade.")
    gather_candidates(..., sources=["llm_web"], curator=curator, web_search_mode="native", web_guidance=g)
    assert curator.last_guidance == g
```

Adapt the helper names to the ones the surrounding tests already use (`_FakeExtractingSearch` at ~308; look at `TestStructuredExtractionPath` ~1474 for a complete Exa gather to copy).
- Add to `TestWebSearchWithoutAnLlm` (~547): with the null curator and Exa, a non-builtin `web_guidance` still returns Exa's own titles and the curator's `complete` is never called (assert the fake's call count is 0).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_row_guidance.py tests/unit/test_candidates.py -q -p no:randomly`
Expected: FAIL: `RowSpec.__init__() got an unexpected keyword argument 'ai_instructions'`, `gather_candidates() got an unexpected keyword argument 'web_guidance'`.

- [ ] **Step 3: Implement**

`engine/models.py`, on `RowSpec` beside the other optional row fields:

```python
    # Owner-written instructions for AI web search on this row (#138); None = use the server's.
    ai_instructions: AiInstructions | None = None
```

and on `EngineConfig` beside `web_search_provider`:

```python
    # Server-wide AI web search instructions (#138); "" = Shortlist's built-in guidance.
    web_instructions: str = ""
```

(import `AiInstructions` from `shortlist.engine.web_guidance`).

`engine/rows.py`, `RowPolicy`:

```python
    def effective_guidance(self, spec: RowSpec) -> Guidance:
        """This row's AI web search guidance; built-in when the row does not use AI web search (#138)."""
        if "llm_web" not in self.effective_sources(spec):
            return BUILTIN
        return resolve_guidance(spec.ai_instructions, self.cfg.web_instructions)
```

`pool_key`: append one element to the returned tuple, after the season slug:

```python
# Rows with different AI instructions must not share an AI web search (#138); "" otherwise,
# which every row without instructions shares, so no pool splits on the night this ships.
(self.effective_guidance(spec).fingerprint(),)
```

`pools_for`: pass `guidance=self.effective_guidance(spec)` to `_candidate_pool`, and after the seed-count label part:

```python
            if not self.effective_guidance(spec).is_builtin:
                pool_label += " · own AI instructions"
```

`_candidate_pool`: new keyword `guidance: Guidance | None = None`, passed as `web_guidance=guidance` to `gather_candidates`.

`row_recipe`: one more conditional part after the season part:

```python
# AI web search rows with instructions only, so no other row's recipe changes (#138). Editing
# the instructions, or the server-wide text a row inherits, rebuilds that row.
(
    *(
        (f"guide={policy.effective_guidance(spec).fingerprint()}",)
        if not policy.effective_guidance(spec).is_builtin
        else ()
    ),
)
```

`engine/candidates.py`: add `LLM_WEB_K = _LLM_WEB_K` (public alias for the preview endpoint); `gather_candidates(..., web_guidance: Guidance | None = None)` passes `guidance=web_guidance` to `web_recommendations`; `web_recommendations(..., guidance: Guidance | None = None)` passes it to `_web_via_search(..., guidance=guidance)` and to `curator.recommend_web(profile, seeds, k, guidance=guidance)`; `_web_via_search` passes `guidance=guidance` to `build_web_pick_prompt` / `build_web_rag_prompt` (lines ~364-367). Do not touch `build_web_query_for_title`: Exa's per-title queries stay shared and cached across people.

Curators: the base declaration and `anthropic.py`, `openai.py`, `google.py` `recommend_web(self, profile, seeds, k, *, guidance: Guidance | None = None)`, each passing `guidance=guidance` to `build_web_prompt`. `null.py`: accept and ignore the kwarg if it defines `recommend_web`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_row_guidance.py tests/unit/test_candidates.py tests/unit/test_seasonal_rows.py -q -p no:randomly`
Then: `pytest tests/unit/test_pipeline.py -q -p no:randomly -k "pool or recipe or rebuild or settings_change"`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `ruff check shortlist/engine tests/unit/test_row_guidance.py tests/unit/test_candidates.py && ruff format --check shortlist/engine`
Expected: no findings.

---

### Task 3: Server: the setting, the row field, the preview endpoint

**Files:**
- Modify: `shortlist/server/settings_store.py:19` (DEFAULTS), `shortlist/server/api/settings.py` (~296-375 VALIDATORS)
- Modify: `shortlist/server/api/collections.py` (`PosterIn` ~139 pattern, `CollectionIn` ~152/275, `CollectionOut` ~509, `_validate` ~577, create ~1300-1325, `_apply_patch` ~1480/1525, `_serialize` ~938/1057)
- Modify: `shortlist/server/services/context_builder.py` (`RowSpec(...)` ~1218, `EngineConfig(...)` ~1107)
- Modify: `shortlist/server/db/models.py:312` (comment only)
- Create: `shortlist/server/api/ai.py`; Modify: `shortlist/server/main.py:406-424` (router tuple)
- Modify: `web/openapi.snapshot.json` (regenerated)
- Test: `tests/integration/test_api_collections.py`, `tests/integration/test_api_settings.py`, new `tests/integration/test_api_ai.py`, `tests/unit/test_openapi_snapshot.py` (existing, must pass)

**Interfaces:**
- Consumes: `AiInstructions`, `INSTRUCTION_MODES`, `MAX_INSTRUCTIONS_CHARS`, `resolve_guidance` (Task 1); `web_system_prompt`, `builtin_guidance` (Task 1); `LLM_WEB_K` (Task 2); `RowSpec.ai_instructions`, `EngineConfig.web_instructions` (Task 2).
- Produces (HTTP): `CollectionIn.ai_instructions: {mode: "default"|"add"|"own", text: str ≤2000}`; `CollectionOut.ai_instructions: {mode, text}`; setting `llm_web.instructions: str ≤2000` (default `""`); `POST /api/ai/web-prompt-preview` body `{ai_instructions?: {mode, text}, server_text?: str}` → `{backend: str, system: str, builtin_guidance: str, inert: bool}`.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_api_collections.py`, modelled on `test_poster_config_round_trips_and_reaches_the_spec` (~1002):

```python
def test_ai_instructions_round_trip_and_reach_the_spec(client, ...):  # same fixtures as the poster test
    created = client.post("/api/collections", json={**_row_body(), "ai_instructions": {"mode": "add", "text": " No kids films. "}})
    assert created.status_code == 201
    assert created.json()["ai_instructions"] == {"mode": "add", "text": "No kids films."}
    rid = created.json()["id"]
    patched = client.patch(f"/api/collections/{rid}", json={"ai_instructions": {"mode": "own", "text": "Any decade."}})
    assert patched.json()["ai_instructions"] == {"mode": "own", "text": "Any decade."}
    spec = _spec_for(rid)  # build via ContextBuilder(...)._build_rows(...) exactly as the poster test does
    assert spec.ai_instructions == AiInstructions("own", "Any decade.")


def test_ai_instructions_need_words_unless_they_use_the_default(client, ...):
    bad = client.post("/api/collections", json={**_row_body(), "ai_instructions": {"mode": "own", "text": "   "}})
    assert bad.status_code == 422
    assert client.post("/api/collections", json={**_row_body(), "ai_instructions": {"mode": "nope", "text": "x"}}).status_code == 422
    assert client.post("/api/collections", json={**_row_body(), "ai_instructions": {"mode": "add", "text": "x" * 2001}}).status_code == 422


def test_a_row_saved_with_the_default_stores_nothing_new(client, db_session, ...):
    rid = client.post("/api/collections", json=_row_body()).json()["id"]
    assert db_session.get(Collection, rid).prompt == {}
    assert client.get("/api/collections").json()[...]["ai_instructions"] == {"mode": "default", "text": ""}


def test_a_legacy_prompt_value_reads_as_the_default(client, db_session, ...):
    rid = client.post("/api/collections", json=_row_body()).json()["id"]
    db_session.get(Collection, rid).prompt = {"tone": "warm", "guidance": "old curate setting"}
    db_session.commit()
    row = next(r for r in client.get("/api/collections").json() if r["id"] == rid)
    assert row["ai_instructions"] == {"mode": "default", "text": ""}


def test_changing_ai_instructions_is_audited(client, db_session, ...):
    rid = client.post("/api/collections", json=_row_body()).json()["id"]
    client.patch(f"/api/collections/{rid}", json={"ai_instructions": {"mode": "add", "text": "No kids films."}})
    event = db_session.query(Event).filter_by(scope="collection.ai_instructions").one()
    assert event.message["mode"] == "add" and event.message["chars"] == len("No kids films.")
```

In `tests/integration/test_api_settings.py`, modelled on the test at ~37:

```python
def test_ai_instructions_setting_accepts_text_up_to_2000_characters(client):
    assert client.put("/api/settings", json={"values": {"llm_web.instructions": "Favour classics."}}).status_code == 200
    assert client.put("/api/settings", json={"values": {"llm_web.instructions": "x" * 2001}}).status_code == 422
    assert client.put("/api/settings", json={"values": {"llm_web.instructions": 7}}).status_code == 422
```

Also assert the RowSpec/EngineConfig mapping of the setting: in the same file or `test_api_collections.py`, set `llm_web.instructions` and assert the built `EngineConfig.web_instructions == "Favour classics."` (use whatever helper the context-builder tests already use to build the config; `tests/unit/test_run_service_context.py` is the place if it builds `EngineConfig`).

New `tests/integration/test_api_ai.py` (owner client fixture as the other API tests):

```python
def test_preview_shows_the_builtin_prompt_when_nothing_is_set(client):
    body = client.post("/api/ai/web-prompt-preview", json={}).json()
    assert body["backend"] == "native"
    assert "Strongly prefer titles released in" in body["system"]
    assert body["builtin_guidance"].startswith("Based on what this person recently watched")
    assert body["inert"] is False


def test_preview_applies_a_rows_instructions_over_the_saved_default(client):
    client.put(
        "/api/settings", json={"values": {"llm_web.instructions": "Favour classics.", "llm_web.search_provider": "exa"}}
    )
    body = client.post(
        "/api/ai/web-prompt-preview", json={"ai_instructions": {"mode": "add", "text": "No kids films."}}
    ).json()
    assert body["backend"] == "exa"
    assert "Favour classics. Pick up to" in body["system"]
    assert "The server owner adds, for this row: No kids films." in body["system"]


def test_preview_can_try_unsaved_server_text(client):
    body = client.post("/api/ai/web-prompt-preview", json={"server_text": "Any decade."}).json()
    assert "Any decade. Give up to" in body["system"]


def test_exa_without_an_ai_provider_is_inert(client):
    client.put("/api/settings", json={"values": {"llm_web.search_provider": "exa", "curator.provider": "none"}})
    assert client.post("/api/ai/web-prompt-preview", json={}).json()["inert"] is True


def test_preview_needs_the_owner(anon_client):  # use the unauthenticated-client fixture other API tests use
    assert anon_client.post("/api/ai/web-prompt-preview", json={}).status_code in (401, 403)
```

If setting `curator.provider` through `PUT /api/settings` is refused by validation in tests, write it via the settings store fixture the other tests use.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_api_collections.py tests/integration/test_api_settings.py tests/integration/test_api_ai.py -q -p no:randomly -k "ai_instructions or preview or legacy_prompt"`
Expected: FAIL (unknown field / 404 route).

- [ ] **Step 3: Implement**

Settings: `DEFAULTS["llm_web.instructions"] = ""` beside `llm_web.search_provider`. In `api/settings.py` add a validator helper and register it:

```python
def _text_at_most(limit: int) -> Callable[[object], str | None]:
    """Free text up to ``limit`` characters (no other free-text setting caps its length yet)."""

    def check(value: object) -> str | None:
        if not isinstance(value, str):
            return "must be text"
        if len(value) > limit:
            return f"must be at most {limit} characters"
        return None

    return check
```

`"llm_web.instructions": _text_at_most(MAX_INSTRUCTIONS_CHARS),` (match the registry's actual callable signature; if validators take `(key, value)` adapt the inner function).

`api/collections.py`: copy the `PosterIn`/`PosterOut` pattern:

```python
class AiInstructionsIn(StrictRequestModel):
    """What AI web search should look for on this row (#138)."""

    mode: str = _closed_set(INSTRUCTION_MODES, "default", "AI instructions must be default, add or own")
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS)


class AiInstructionsOut(PassthroughModel):
    mode: str
    text: str
```

(Use `_closed_set`'s real signature as `PosterIn.mode` does.) `CollectionIn.ai_instructions: AiInstructionsIn = Field(default_factory=AiInstructionsIn)`; `CollectionOut.ai_instructions: AiInstructionsOut`.

Helpers:

```python
def _stored_instructions(body: AiInstructionsIn) -> dict[str, str]:
    """What `Collection.prompt` holds: {} for the default, so an untouched row stores exactly what it did."""
    if body.mode == "default":
        return {}
    return {"mode": body.mode, "text": body.text.strip()}


def _ai_instructions_view(stored: object) -> dict[str, str]:
    parsed = AiInstructions.from_stored(stored)
    return {"mode": parsed.mode, "text": parsed.text} if parsed else {"mode": "default", "text": ""}
```

`_validate`: `if body.ai_instructions.mode != "default" and not body.ai_instructions.text.strip(): raise HTTPException(422, "Write the AI instructions, or choose Use the default.")`.
Create (~1321): `prompt=_stored_instructions(body.ai_instructions),`.
`_apply_patch` (after the poster block ~1525-1537), same Event shape as the poster's:

```python
    if "ai_instructions" in sent:
        collection.prompt = _stored_instructions(body.ai_instructions)
        session.add(
            Event(
                scope="collection.ai_instructions",
                level="info",
                message={
                    "slug": collection.slug,
                    "mode": body.ai_instructions.mode,
                    "chars": len(body.ai_instructions.text.strip()),
                    "at": <same timestamp expression the poster event uses>,
                },
            )
        )
```

`_serialize` (~1057): `"ai_instructions": _ai_instructions_view(collection.prompt),`.
`db/models.py:312`: replace the dead-column note with `# AI web search instructions for this row (#138): {} or {"mode": "add"|"own", "text": str}. Held the curate settings until migration 0036 cleared it.`

`context_builder.py`: in `RowSpec(...)` add `ai_instructions=AiInstructions.from_stored(collection.prompt),`; in `EngineConfig(...)` add `web_instructions=str(store.get("llm_web.instructions") or ""),`.

New `shortlist/server/api/ai.py` (copy the router/auth/settings-store access pattern from `api/seasons.py` ~372 and `api/settings.py`):

```python
"""AI helpers for the editor: show exactly what AI web search would send (#138)."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from shortlist.engine.candidates import LLM_WEB_K
from shortlist.engine.curator.base import builtin_guidance, web_system_prompt
from shortlist.engine.web_guidance import MAX_INSTRUCTIONS_CHARS, AiInstructions, resolve_guidance
from shortlist.server.api.collections import AiInstructionsIn
# + the owner dependency and the settings-store accessor other routers use

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_owner)])


class WebPromptPreviewIn(StrictRequestModel):
    ai_instructions: AiInstructionsIn | None = None
    # Unsaved server-wide text from the Settings editor; None = the saved setting.
    server_text: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_CHARS)


class WebPromptPreviewOut(PassthroughModel):
    backend: str
    system: str
    builtin_guidance: str
    inert: bool


@router.post("/web-prompt-preview", response_model=WebPromptPreviewOut)
def web_prompt_preview(body: WebPromptPreviewIn, request: Request) -> dict:
    """The system prompt AI web search sends with these instructions. Reads settings; writes nothing."""
    store = <settings store for this request>
    backend = store.get("llm_web.search_provider") or "native"
    server_text = body.server_text if body.server_text is not None else (store.get("llm_web.instructions") or "")
    row = AiInstructions.from_stored(body.ai_instructions.model_dump()) if body.ai_instructions else None
    year = datetime.now(UTC).year
    return {
        "backend": backend,
        "system": web_system_prompt(backend, k=LLM_WEB_K, year=year, guidance=resolve_guidance(row, server_text)),
        "builtin_guidance": builtin_guidance(backend, k=LLM_WEB_K, year=year),
        # Exa with no AI provider uses Exa's own titles as found (candidates.py `_titles_as_proposals`).
        "inert": backend == "exa" and (store.get("curator.provider") or "none") == "none",
    }
```

Add `ai` to the router tuple in `main.py`.

Regenerate the OpenAPI snapshot (command from `tests/unit/test_openapi_snapshot.py:11-14`):

```bash
SHORTLIST_CONFIG=$(mktemp -d) python -c "import json,pathlib; from shortlist.server.main import create_app; pathlib.Path('web/openapi.snapshot.json').write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + '\n')"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/integration/test_api_collections.py tests/integration/test_api_settings.py tests/integration/test_api_ai.py tests/unit/test_openapi_snapshot.py tests/unit/test_settings_store.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 5: Lint**

Run: `ruff check shortlist/server tests/integration/test_api_ai.py && ruff format --check shortlist/server`
Expected: no findings.

---

### Task 4: Row editor: the AI instructions field

**Files:**
- Modify: `web/src/lib/api-schema.d.ts` (generated: `pnpm -C web gen:api`)
- Modify: `web/src/lib/api.ts` (add `previewWebPrompt`)
- Modify: `web/src/lib/collections.ts` (`blankInput` ~18, `toInput` ~91, `rowOverrides` ~316-323)
- Modify: `web/src/lib/row-kinds.ts` (`ROW_SETTING_KEYS` ~341, `SETTING_LABELS` ~360, `FILL_SETTINGS` ~509/523/538, `visibleSettings` ~581)
- Modify: `web/src/components/rows/row-draft-diff.ts` (`LABELS` ~12, `describe` ~73)
- Create: `web/src/components/rows/row-ai-instructions-field.tsx`
- Modify: `web/src/components/rows/row-contents-fields.tsx` (after `{recentCount}` ~180 and ~196)
- Modify: `web/src/lib/sources.ts:46` (stale "Settings → Finding titles")
- Test: new `web/src/test/row-ai-instructions-field.test.tsx`; extend `web/src/test/row-kinds.test.ts`, `web/src/test/collections.test.ts`

**Interfaces:**
- Consumes: HTTP from Task 3.
- Produces: `api.previewWebPrompt(body: {ai_instructions?: {mode, text}; server_text?: string}): Promise<{backend: string; system: string; builtin_guidance: string; inert: boolean}>` (Task 5 uses it); `RowAiInstructionsField` props `{ value: AiInstructions; onChange(v: AiInstructions): void; otherSources: string[]; backend: string; inert: boolean }` where `AiInstructions = Collection["ai_instructions"]`.

- [ ] **Step 1: Regenerate types and write the failing tests**

Run: `pnpm -C web gen:api` (falls back to `web/openapi.snapshot.json`).

`web/src/test/row-ai-instructions-field.test.tsx` (mock `@/lib/api` the way `row-sources-field.test.tsx` does; render with `QueryClientProvider`):

```tsx
it("shows the default text and no textarea when the row uses the default", async () => {
  renderField({ value: { mode: "default", text: "" } });
  expect(screen.getByRole("button", { name: "Use the default" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.queryByRole("textbox")).toBeNull();
});

it("switching to Add to the default reveals a 2000-character textarea and reports the change", async () => {
  const onChange = vi.fn();
  renderField({ value: { mode: "default", text: "" }, onChange });
  await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
  expect(onChange).toHaveBeenCalledWith({ mode: "add", text: "" });
});

it("names the sources that won't read the instructions", () => {
  renderField({ value: { mode: "add", text: "x" }, otherSources: ["TMDB similar", "Trakt"] });
  expect(screen.getByText(/TMDB similar and Trakt don't read these instructions/)).toBeInTheDocument();
});

it("says the instructions do nothing on Exa without an AI provider", () => {
  renderField({ value: { mode: "add", text: "x" }, backend: "exa", inert: true });
  expect(screen.getByText(/these instructions have no effect/)).toBeInTheDocument();
});

it("shows exactly what's sent when opened", async () => {
  api.previewWebPrompt.mockResolvedValue({ backend: "native", system: "SYSTEM TEXT", builtin_guidance: "", inert: false });
  renderField({ value: { mode: "add", text: "x" } });
  await userEvent.click(screen.getByText("Exactly what's sent"));
  expect(await screen.findByText(/SYSTEM TEXT/)).toBeInTheDocument();
  expect(api.previewWebPrompt).toHaveBeenCalledWith({ ai_instructions: { mode: "add", text: "x" } });
});
```

(Use the `aria-pressed`/selected attribute the real `Segmented` renders; read `components/segmented.tsx` and assert on that.)

`row-kinds.test.ts`: `visibleSettings` includes `"ai_instructions"` when the row's sources include `llm_web` and excludes it otherwise (mirror the existing `recent_count` visibility test).
`collections.test.ts` in `describe("rowOverrides")`: `{ai_instructions: {mode: "add", text: "x"}}` → contains `"AI instructions: adds to the default"`; `mode: "own"` → `"AI instructions: own"`; `mode: "default"` → neither.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm -C web exec vitest run src/test/row-ai-instructions-field.test.tsx src/test/row-kinds.test.ts src/test/collections.test.ts`
Expected: FAIL (module not found / missing labels).

- [ ] **Step 3: Implement**

`lib/api.ts`: `previewWebPrompt(body)` → `POST /api/ai/web-prompt-preview`, following the file's existing POST helpers and typed from `api-schema.d.ts` (`Schemas["WebPromptPreviewIn"]` / `["WebPromptPreviewOut"]`).

`lib/collections.ts`: `blankInput()` gains `ai_instructions: { mode: "default", text: "" }`; `toInput()` copies `collection.ai_instructions`; `rowOverrides` after the `recent_count` branch:

```ts
  if (collection.ai_instructions?.mode === "add") parts.push("AI instructions: adds to the default");
  if (collection.ai_instructions?.mode === "own") parts.push("AI instructions: own");
```

`lib/row-kinds.ts`: add `"ai_instructions"` to `ROW_SETTING_KEYS`, `SETTING_LABELS["ai_instructions"] = "AI instructions"`, beside `"recent_count"` in every `FILL_SETTINGS` list that has it, and in `visibleSettings` beside the `recent_count` line: `if (!rowSources(input, ctx).includes("llm_web")) shown.delete("ai_instructions");`.

`row-draft-diff.ts`: `LABELS.ai_instructions = "AI instructions"`; in `describe()` a case rendering mode changes as `Use the default → Add to the default` using `{default: "Use the default", add: "Add to the default", own: "Write your own"}`, and a text-only change as just the label.

`components/rows/row-ai-instructions-field.tsx`: one component, copy text exactly:

- Label "AI instructions"; help "What AI web search should look for in this row."
- `Segmented` options `default` "Use the default", `add` "Add to the default", `own` "Write your own"; `onChange(mode)` → `onChange({ mode, text: value.text })`.
- `default`: a mono block showing the saved server-wide text or, when it is empty, `builtin_guidance` from `previewWebPrompt({})` (fetched with TanStack Query), then "The default every row starts with. Change it in Settings → Defaults → Title sources." with a link to `/settings/defaults#sources`.
- `add`: label "Also tell the AI", `Textarea` (`maxLength={2000}`), help "Added after the default instructions, for this row only."
- `own`: label "Your instructions", `Textarea` (`maxLength={2000}`, ~6 rows), help "You can use {count} and {year}.", a "Reset to the default" button setting `{mode: "default", text: value.text}`, then a dashed note titled "Shortlist always adds these" reading "Today's year, and that the AI must search rather than answer from memory. The exact title and release year for every pick, so it can be found in your library. Only titles already out. The reply format."
- When `otherSources.length` (the row's effective sources minus `llm_web`, short names from `lib/sources.ts`): warning-coloured line "`{joined}` don't read these instructions, so this row will be a mix." where `joined` is "A", "A and B", or "A, B and C".
- Backend note (`backend` from settings `llm_web.search_provider`):
  - `inert`: "With no AI provider, Exa's titles are used as found, so these instructions have no effect. Add an AI provider in Settings → Connections."
  - `exa`: "You search with Exa. Exa's searches start from each person's recent watches and are shared between people, so these instructions decide which of Exa's titles the AI keeps, not what Exa looks for."
  - `searxng`: "You search with SearXNG. Its results start from each person's recent watches; these instructions decide which titles the AI picks from them."
  - `native`: no note.
- When mode ≠ default: help "Rows with different instructions can't share one AI web search, so this row may make its own AI call per person each run."
- `<details>` summary "Exactly what's sent": on open, `useQuery(["web-prompt-preview", value.mode, debouncedText], () => api.previewWebPrompt({ ai_instructions: value }))` (debounce 400 ms with the project's existing debounce helper if there is one, else `useDeferredValue`); show `system` in a mono `<pre>` (whitespace-pre-wrap, ligatures off per DESIGN.md), then "Then the person's 20 most recent watches and the titles found are added." Loading: a skeleton line; error: "Couldn't load the preview." with a Retry button.

`row-contents-fields.tsx`: `const aiInstructions = shown.has("ai_instructions") && (<div data-setting="ai_instructions"><RowAiInstructionsField value={draft.ai_instructions} onChange={(v) => set({ ai_instructions: v })} otherSources={...} backend={settings["llm_web.search_provider"] ?? "native"} inert={...} /></div>)`, rendered right after `{recentCount}` in both returns. Compute `otherSources` from `effectiveSources` (exported by `row-sources-field.tsx`) minus `llm_web`, mapped to short names; `inert` = backend `exa` and `settings["curator.provider"]` in `("none", "", undefined)`.

`lib/sources.ts:46`: replace "Pick which in Settings → Finding titles." with "Pick which in Settings → Connections → AI & Web search."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm -C web exec vitest run src/test/row-ai-instructions-field.test.tsx src/test/row-kinds.test.ts src/test/collections.test.ts src/test/row-editor.test.tsx src/test/row-sources-field.test.tsx`
Then: `pnpm -C web exec tsc -b --force`
Expected: PASS, no type errors.

---

### Task 5: Settings: the default instructions

**Files:**
- Modify: `web/src/components/settings/recommendations-section.tsx` (state ~122-148, `useAutosavedSettings` ~160, Web search disclosure ~212)
- Modify: `web/src/components/runs/run-stat-tiles.tsx:232` (stale "Settings → Finding titles")
- Test: `web/src/test/recommendations-section.test.tsx`

**Interfaces:**
- Consumes: `api.previewWebPrompt` (Task 4); setting `llm_web.instructions` (Task 3).

- [ ] **Step 1: Write the failing tests** (in `recommendations-section.test.tsx`, its existing render/mocks):

```tsx
it("shows Shortlist's built-in instructions until the owner writes their own", async () => {
  api.previewWebPrompt.mockResolvedValue({ backend: "exa", system: "", builtin_guidance: "BUILT IN TEXT", inert: false });
  renderSection({ "llm_web.instructions": "", "candidates.sources": ["tmdb_similar", "llm_web"] });
  expect(await screen.findByText("BUILT IN TEXT")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Write your own" }));
  expect(screen.getByLabelText("AI instructions")).toHaveValue("BUILT IN TEXT");
});

it("autosaves the owner's instructions", async () => {
  renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
  const box = screen.getByLabelText("AI instructions");
  await userEvent.type(box, " Any decade.");
  await waitFor(() =>
    expect(api.putSettings).toHaveBeenCalledWith(expect.objectContaining({ "llm_web.instructions": "Favour classics. Any decade." })),
  );
});

it("Reset to Shortlist's default clears the setting", async () => {
  renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
  await userEvent.click(screen.getByRole("button", { name: "Reset to Shortlist's default" }));
  await waitFor(() => expect(api.putSettings).toHaveBeenCalledWith(expect.objectContaining({ "llm_web.instructions": "" })));
});
```

(Match the exact argument shape `api.putSettings` is asserted with in the existing tests of this file.)

- [ ] **Step 2: Run to verify they fail**

Run: `pnpm -C web exec vitest run src/test/recommendations-section.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

State `const [aiInstructions, setAiInstructions] = useState<string>(String(settings["llm_web.instructions"] ?? ""))`; add `aiInstructions` to the first `useAutosavedSettings` object and `"llm_web.instructions": aiInstructions` to the second. Inside the Web search disclosure, after `<AiWebSearchCard …/>`, a hairline-separated block:

- Label "AI instructions"; help "What the AI looks for, on every row that doesn't have its own. A row can add to these or replace them in its editor, under What goes in."
- Empty value: mono block with `builtin_guidance` (from `api.previewWebPrompt({})` via TanStack Query; loading skeleton; on error "Couldn't load Shortlist's default instructions." + Retry) and an outline button "Write your own" that sets the value to that text.
- Non-empty: `Textarea` labelled "AI instructions" (`maxLength={2000}`), help "You can use {count} and {year}.", ghost button "Reset to Shortlist's default" (sets `""`), the same dashed "Shortlist always adds these" note as the row field, and "A change here rebuilds every row that uses AI web search, on that row's next run."

`run-stat-tiles.tsx:232`: "Turn AI sources off in Settings → Finding titles to lower it." → "Turn AI sources off in Settings → Defaults → Title sources to lower it."

- [ ] **Step 4: Run to verify they pass**

Run: `pnpm -C web exec vitest run src/test/recommendations-section.test.tsx src/test/run-stat-tiles.test.tsx` (the second only if it exists)
Then: `pnpm -C web exec tsc -b --force && pnpm -C web exec eslint src/components/settings/recommendations-section.tsx src/components/rows/row-ai-instructions-field.tsx`
Expected: PASS.

---

### Task 6: Docs, changelog and the stale descriptions

**Files:**
- Modify: `docs/guides/rows.md` (What goes in: an "AI instructions" subsection), `docs/reference/settings.md` (`### AI and web search` ~64: a `llm_web.instructions` row in the anchor format used there), `docs/reference/api.md` (`## Rows` ~200: `ai_instructions` on the collections line; `POST /api/ai/web-prompt-preview` beside `POST /api/seasons/preview`)
- Modify: `CHANGELOG.md`: an `## [Unreleased]` "Added" entry ("AI instructions: tell AI web search what to look for, server-wide in Settings → Defaults → Title sources and per row under What goes in."), and under `## [0.1.0-beta] - 2026-07-21` a line after the "Multiple rows" bullet: "*Later removed: per-row curation styles and prompts, the AI curator and "AI suggests from your library" were withdrawn before 0.1.0-beta.9, when ranking and reasons moved into code. Per-row AI instructions for AI web search returned in [Unreleased].*"
- Modify: `PRODUCT.md:43` ("optionally curate and explain with an LLM" → "optionally ask an AI to suggest titles through web search"), `.claude/CLAUDE.md:5` ("TMDB similar-titles → LLM curate/explain" → "TMDB similar-titles (+ optional AI web search) → ranking and reasons in code")
- Regenerate: `docs/llms-full.txt`, and `docs/feed.xml` if the changelog edit changes a feed entry

- [ ] **Step 1: Write the docs** (plain English, the voice of the surrounding pages; no environment details)
- [ ] **Step 2: Regenerate and check**

```bash
python scripts/build_llms_full.py
python scripts/build_feed.py
pytest tests/unit/test_llms_full.py tests/unit/test_feed.py -q -p no:randomly
```

Expected: PASS. If `build_feed.py` changes nothing, that is fine.

---

### Task 7: Full verification, then ask to commit

- [ ] **Step 1: Python suite, once, in the background** (clear stale `.coverage*` first; one pytest at a time on this host)

```bash
rm -f .coverage .coverage.*; pytest -q 2>&1 | tail -15
```

Expected: all pass, coverage gate met.

- [ ] **Step 2: Web suite and checks**

```bash
pnpm -C web test 2>&1 | tail -8
pnpm -C web exec tsc -b --force
pnpm -C web exec eslint .
pnpm -C web build 2>&1 | tail -3
ruff check . && ruff format --check .
```

Expected: all green.

- [ ] **Step 3: e2e (the row editor changed)**

```bash
pnpm -C web build && pytest -m e2e -q 2>&1 | tail -8
```

Expected: PASS.

- [ ] **Step 4: Ask the owner to commit.** Proposed commits (Conventional Commits, staged explicitly by path, never `git add -A`):
  1. `feat(engine): owner-written guidance for AI web search, per row and server-wide (#138)` (Tasks 1-2)
  2. `feat(server): AI instructions on rows, the llm_web.instructions setting and a prompt preview (#138)` (Task 3)
  3. `feat(web): AI instructions in the row editor and Settings → Defaults (#138)` (Tasks 4-5)
  4. `docs: AI instructions, and stop promising the removed per-row curation prompts (#138)` (Task 6)

## Deviations from the mockups (deliberate)

- Placeholders are `{count}` and `{year}` only; `{recent_watches}` / `{top_genres}` are dropped (the watch list is already in the message the AI gets, and the profile has no top-genre list to fill).
- "Exactly what's sent" shows the system prompt and says the watch list and found titles follow; a per-person rendering would need a run.
- Settings omits the "Rows with their own instructions" list; the Rows page badges cover it.
- The editor's warning does not suggest an AI row until phase 3 ships one.
