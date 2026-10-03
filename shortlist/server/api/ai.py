"""AI helpers for the editor: show exactly what AI web search would send (#138)."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from shortlist.engine.candidates import EXTERNAL_SEARCH_MODES, LLM_WEB_K
from shortlist.engine.curator.base import builtin_guidance, builtin_template, web_system_prompt
from shortlist.engine.web_guidance import MAX_INSTRUCTIONS_CHARS, AiInstructions, resolve_guidance
from shortlist.server.api.collections import AiInstructionsIn
from shortlist.server.api.schemas import PassthroughModel, StrictRequestModel
from shortlist.server.auth import require_owner
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_owner)])

# Providers whose curator can search the web itself (`supports_native_web_search`). Mirrors the SPA's
# `hasNativeWebSearch` (`web/src/lib/sources.ts`).
_NATIVE_SEARCH_PROVIDERS = ("anthropic", "openai", "google")


class WebPromptPreviewIn(StrictRequestModel):
    """What to preview: a row's instructions and/or unsaved server-wide text; either may be omitted."""

    ai_instructions: AiInstructionsIn | None = None
    # Unsaved server-wide text from the Settings editor; None = the saved setting.
    server_text: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_CHARS)


class WebPromptPreviewOut(PassthroughModel):
    """The prompt AI web search would send, the built-in wording, and whether instructions do anything."""

    backend: str
    system: str
    builtin_guidance: str
    # The same passage with `{count}`, `{year}` and `{last_year}` unfilled: what "Write your own" starts from.
    builtin_template: str
    inert: bool


@router.post("/web-prompt-preview", response_model=WebPromptPreviewOut)
def web_prompt_preview(body: WebPromptPreviewIn, request: Request) -> dict:
    """The system prompt AI web search sends with these instructions. Reads settings; writes nothing."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        backend = store.get("llm_web.search_provider") or "native"
        saved_text = store.get("llm_web.instructions") or ""
        provider = store.get("curator.provider") or "none"  # empty means none
    server_text = body.server_text if body.server_text is not None else saved_text
    row = AiInstructions.from_stored(body.ai_instructions.model_dump()) if body.ai_instructions else None
    year = datetime.now(UTC).year
    return {
        "backend": backend,
        "system": web_system_prompt(backend, k=LLM_WEB_K, year=year, guidance=resolve_guidance(row, server_text)),
        "builtin_guidance": builtin_guidance(backend, k=LLM_WEB_K, year=year),
        "builtin_template": builtin_template(backend),
        # With no AI provider, Exa uses its own titles as found (candidates.py `_titles_as_proposals`) and
        # native/SearXNG AI web search does not run, so instructions do nothing on any backend. Native search
        # also needs a provider with its own search tool; any other backend is native (`_web_search_capable`).
        "inert": provider == "none"
        or (backend not in EXTERNAL_SEARCH_MODES and provider not in _NATIVE_SEARCH_PROVIDERS),
    }
