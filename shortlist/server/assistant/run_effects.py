"""Pure projection of the provider and acquisition effects one run may invoke."""

from dataclasses import asdict

from shortlist.engine.clients.search import DEFAULT_EXA_SEARCH_TYPE, EXA_SEARCH_TYPES
from shortlist.engine.provider_calls import ProviderCall
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.rows import effective_row_sources
from shortlist.server.services.request_actions import _destination, _target_snapshot

from .generation import provider_destination
from .policy import ChangeError


def paid_effect_contract(config, store, intent, *, config_hash):
    """Resolve finite allowed descriptors without constructing any network client."""
    provider = str(store.get("curator.provider") or "none")
    if provider == "ollama":
        provider = "openai_compatible"
    mode = str(store.get("llm_web.search_provider") or "native")
    selected = [row for row in config.rows if config.should_build(row)]
    web = any("llm_web" in effective_row_sources(row, config.candidate_sources) for row in selected)
    images = not config.dry_run and any(row.poster and row.poster.mode in {"ai", "generate"} for row in selected)
    descriptors = []
    managed_search = False

    def append(kind, name, destination, model=None):
        descriptors.append(asdict(ProviderCall(kind, name, destination, model)))

    if web:
        if intent.max_provider_calls == 0:
            raise ChangeError("budget_exceeded", "AI generation and search require an explicit provider-call budget.")
        if mode in {"exa", "searxng"}:
            if mode == "exa":
                if not store.get("exa.apikey"):
                    raise ChangeError("invalid_selection", "Configure the selected Exa connection before a paid run.")
                search_type = str(store.get("exa.search_type") or DEFAULT_EXA_SEARCH_TYPE)
                search_type = search_type if search_type in EXA_SEARCH_TYPES else DEFAULT_EXA_SEARCH_TYPE
                append("external_search", "exa", "https://api.exa.ai", search_type)
            else:
                append("external_search", "searxng", _destination(str(store.get("searxng.url") or "")))
                if provider in {"none", "null", ""}:
                    raise ChangeError("invalid_selection", "SearXNG recommendations also require a configured model.")
        elif provider in {"none", "null", ""}:
            raise ChangeError("invalid_selection", "Configure a native-search provider before a paid run.")
        if provider not in {"none", "null", ""}:
            if provider != "openai_compatible" and not store.get("curator.api_key"):
                raise ChangeError("invalid_selection", "Configure the selected provider credential before a paid run.")
            from shortlist.engine.curator.anthropic import DEFAULT_MODEL as ANTHROPIC_MODEL
            from shortlist.engine.curator.google import DEFAULT_MODEL as GOOGLE_MODEL
            from shortlist.engine.curator.openai import DEFAULT_MODEL as OPENAI_MODEL

            model = str(store.get("curator.model") or "")
            if provider == "openai_compatible" and not model:
                raise ChangeError(
                    "invalid_selection", "Configure an explicit compatible-provider model before a paid run."
                )
            model = model or {"openai": OPENAI_MODEL, "anthropic": ANTHROPIC_MODEL, "google": GOOGLE_MODEL}.get(
                provider, ""
            )
            if not model:
                raise ChangeError("invalid_selection", "The configured provider has no bounded model contract.")
            kind = "completion" if mode in {"exa", "searxng"} else "native_search"
            if kind == "native_search" and provider == "openai_compatible":
                raise ChangeError(
                    "invalid_selection", "Compatible providers require a configured external search backend."
                )
            if kind == "native_search" and provider == "google":
                if not intent.allow_provider_managed_search:
                    raise ChangeError(
                        "missing_permission", "Google native search requires explicit provider-managed-search approval."
                    )
                managed_search = True
            append(kind, provider, provider_destination(store), model)
    if images:
        if intent.max_provider_calls == 0 or intent.max_images == 0:
            raise ChangeError("budget_exceeded", "AI posters require explicit provider-call and image budgets.")
        from shortlist.server.services.poster_service import GOOGLE_IMAGE_MODEL, OPENAI_IMAGE_MODEL

        if provider not in {"openai", "google"}:
            raise ChangeError("invalid_selection", "The configured provider cannot generate bounded AI posters.")
        if not store.get("curator.api_key"):
            raise ChangeError("invalid_selection", "Configure the image provider credential before a paid run.")
        append(
            "image",
            provider,
            provider_destination(store),
            OPENAI_IMAGE_MODEL if provider == "openai" else GOOGLE_IMAGE_MODEL,
        )

    acquisitions = []
    queue_requests = False
    if not config.dry_run and config.requests is not None and config.requests.enabled:
        for row in selected:
            if row.shared:
                continue
            queue_requests = True
            cfg = resolve_request_config(config.requests, row.request_overrides)
            if not cfg.auto_send or cfg.max_per_run <= 0 or cfg.max_per_row == 0:
                continue
            for media in ("movie", "show") if row.media == "both" else (row.media,):
                target = _target_snapshot(cfg, media)
                if target["configured"]:
                    acquisitions.append(
                        {
                            "row_slug": row.slug,
                            "media": media,
                            "destination": target["destination"],
                            "config_hash": config_hash(cfg),
                        }
                    )
        if acquisitions and intent.max_acquisitions == 0:
            raise ChangeError(
                "budget_exceeded", "Configured automatic requests require an explicit acquisition budget."
            )
    return {
        "providers": descriptors,
        "acquisitions": acquisitions,
        "queue_requests": queue_requests,
        "provider_managed_search": managed_search,
    }
