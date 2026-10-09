"""Authoritative descriptive registry for public Shortlist settings."""

from __future__ import annotations

from pydantic import JsonValue

from shortlist.engine.candidates import KNOWN_SOURCES
from shortlist.engine.clients.search import EXA_SEARCH_TYPES
from shortlist.engine.models import (
    LANGUAGE_MODES,
    MAX_REFRESH_DAYS,
    MAX_ROW_SIZE,
    MIN_ROW_SIZE,
    REQUEST_TARGETS,
    SONARR_MONITOR_MODES,
)
from shortlist.server.scheduler import DEFAULT_CRONS
from shortlist.server.services.notify import EVENTS as NOTIFICATION_EVENTS
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS

from .models import (
    EFFECT_CAPABILITIES,
    Capability,
    Effect,
    EffectReference,
    EffectTiming,
    NumericRange,
    ResetBehavior,
    SettingDefinition,
    SettingGroup,
    SettingOption,
    SettingPrerequisite,
    ValueType,
)


def _effect(kind: Effect, timing: EffectTiming, description: str) -> EffectReference:
    return EffectReference(kind=kind, timing=timing, description=description)


LOCAL = _effect(Effect.LOCAL_CONFIG, EffectTiming.IMMEDIATE, "Saves configuration in Shortlist.")
FUTURE_ROW = _effect(
    Effect.PLEX_WRITE,
    EffectTiming.FUTURE_RUN,
    "Can change row contents or placement on a later real run.",
)
FUTURE_REQUEST = _effect(
    Effect.ACQUISITION_WRITE,
    EffectTiming.FUTURE_RUN,
    "Can change which titles a later real run sends to the configured request service.",
)


def _options(
    values: tuple[str, ...] | list[str] | set[str], labels: dict[str, str] | None = None
) -> tuple[SettingOption, ...]:
    labels = labels or {}
    return tuple(SettingOption(value=value, label=labels.get(value, value)) for value in values)


def _prerequisite(key: str, values: tuple[JsonValue, ...], description: str) -> SettingPrerequisite:
    return SettingPrerequisite(key=key, values=values, description=description)


def _value_type(key: str, explicit: ValueType | None) -> ValueType:
    if explicit is not None:
        return explicit
    value = DEFAULTS.get(key, "")
    if isinstance(value, bool):
        return ValueType.BOOLEAN
    if isinstance(value, int):
        return ValueType.INTEGER
    if isinstance(value, float):
        return ValueType.NUMBER
    if isinstance(value, list):
        return ValueType.STRING_LIST
    if isinstance(value, dict):
        return ValueType.OBJECT
    return ValueType.STRING


def _capabilities(effects: tuple[EffectReference, ...], extra: tuple[Capability, ...]) -> tuple[Capability, ...]:
    capabilities = [Capability.CONFIG_WRITE]
    for effect in effects:
        capabilities.extend(EFFECT_CAPABILITIES[effect.kind])
    capabilities.extend(extra)
    return tuple(dict.fromkeys(capabilities))


def _setting(
    key: str,
    label: str,
    description: str,
    group: SettingGroup,
    *,
    value_type: ValueType | None = None,
    options: tuple[SettingOption, ...] = (),
    range: NumericRange | None = None,
    nullable: bool = False,
    reset_behavior: ResetBehavior | None = None,
    prerequisites: tuple[SettingPrerequisite, ...] = (),
    assistant_writable: bool = True,
    effects: tuple[EffectReference, ...] = (LOCAL,),
    capabilities: tuple[Capability, ...] = (),
) -> SettingDefinition:
    has_default = key in DEFAULTS
    secret = key in SECRET_KEYS
    if reset_behavior is None:
        reset_behavior = ResetBehavior.RESTORE_DEFAULT if has_default else ResetBehavior.UNAVAILABLE
    return SettingDefinition(
        key=key,
        label=label,
        description=description,
        value_type=_value_type(key, value_type),
        group=group,
        has_default=has_default,
        default=DEFAULTS.get(key),
        options=options,
        range=range,
        nullable=nullable,
        reset_behavior=reset_behavior,
        prerequisites=prerequisites,
        secret=secret,
        assistant_writable=assistant_writable and not secret,
        effects=effects,
        required_capabilities=_capabilities(effects, capabilities),
    )


_CONNECTION_EFFECTS = (
    LOCAL,
    _effect(Effect.EXTERNAL_READ, EffectTiming.FUTURE_RUN, "Future checks and runs can contact this service."),
)
_CREDENTIAL_EFFECTS = (
    LOCAL,
    _effect(Effect.CREDENTIAL_CHANGE, EffectTiming.IMMEDIATE, "Changes a write-only saved credential."),
    _effect(Effect.EXTERNAL_READ, EffectTiming.FUTURE_RUN, "Future checks and runs can authenticate to this service."),
)
_PROVIDER_EFFECTS = (
    LOCAL,
    _effect(Effect.PROVIDER_SPEND, EffectTiming.FUTURE_RUN, "Future AI authoring can consume provider credits."),
    _effect(
        Effect.PERSONAL_DATA_DISCLOSURE,
        EffectTiming.FUTURE_RUN,
        "Authorized future runs can disclose approved history-derived data to this provider.",
    ),
)
_NOTIFY_EFFECTS = (
    LOCAL,
    _effect(Effect.NOTIFICATION_SEND, EffectTiming.FUTURE_RUN, "Matching future events can send an outbound webhook."),
)


_SETTINGS: tuple[SettingDefinition, ...] = (
    _setting(
        "plex.url",
        "Plex server address",
        "The HTTP or HTTPS address Shortlist uses for Plex Media Server.",
        SettingGroup.PLEX,
        assistant_writable=False,
        effects=_CONNECTION_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "plex.token",
        "Plex token",
        "The encrypted owner credential used to authenticate to Plex; its value is never returned.",
        SettingGroup.PLEX,
        assistant_writable=False,
        effects=_CREDENTIAL_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "plex.orphan_confirm_delay_s",
        "Orphan confirmation delay",
        "Seconds between two independent missing-label reads before Shortlist may delete an owned orphan collection.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=0, maximum=300, unit="seconds"),
        effects=(
            LOCAL,
            _effect(Effect.PLEX_WRITE, EffectTiming.FUTURE_RUN, "Changes deletion timing in future cleanup work."),
        ),
        capabilities=(Capability.MAINTENANCE_EXECUTE,),
    ),
    _setting(
        "plex.timeout_s",
        "Plex request timeout",
        "Seconds Shortlist waits for one Plex Media Server call before retrying or failing it.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=5, maximum=300, unit="seconds"),
    ),
    _setting(
        "plextv.throttle_s",
        "plex.tv write spacing",
        "Minimum seconds between plex.tv writes; adaptive rate-limit backoff still applies.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=0, maximum=60, unit="seconds"),
        effects=(
            LOCAL,
            _effect(
                Effect.PLEX_PRIVACY_WRITE, EffectTiming.FUTURE_RUN, "Changes pacing of future share-filter writes."
            ),
        ),
        capabilities=(Capability.AUDIENCES_WRITE,),
    ),
    _setting(
        "tautulli.url",
        "Tautulli address",
        "The Tautulli server address used for friendly account names.",
        SettingGroup.METADATA,
        assistant_writable=False,
        effects=_CONNECTION_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "tautulli.apikey",
        "Tautulli API key",
        "The encrypted API key used to read account names from Tautulli; its value is never returned.",
        SettingGroup.METADATA,
        assistant_writable=False,
        effects=_CREDENTIAL_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "tmdb.apikey",
        "TMDB API key",
        "The encrypted credential Shortlist uses for title metadata and recommendation candidates.",
        SettingGroup.METADATA,
        assistant_writable=False,
        effects=_CREDENTIAL_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "trakt.client_id",
        "Trakt client ID",
        "The encrypted Trakt client ID used by the optional related-title source.",
        SettingGroup.METADATA,
        assistant_writable=False,
        effects=_CREDENTIAL_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "curator.provider",
        "AI provider",
        "The provider used to author AI themes; none disables provider-backed authoring.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(
            ("none", "anthropic", "openai", "google", "openai_compatible", "ollama", ""),
            {
                "none": "None",
                "anthropic": "Anthropic",
                "openai": "OpenAI",
                "google": "Google Gemini",
                "openai_compatible": "OpenAI-compatible",
                "ollama": "Ollama (legacy name)",
                "": "None (legacy value)",
            },
        ),
        effects=_PROVIDER_EFFECTS,
    ),
    _setting(
        "curator.model",
        "AI model",
        "The model identifier used when Shortlist asks the configured provider to author a theme.",
        SettingGroup.RECOMMENDATIONS,
        prerequisites=(
            _prerequisite(
                "curator.provider",
                ("anthropic", "openai", "google", "openai_compatible", "ollama"),
                "Requires an AI provider.",
            ),
        ),
        effects=_PROVIDER_EFFECTS,
    ),
    _setting(
        "curator.api_key",
        "AI provider API key",
        "The encrypted credential used with the configured hosted AI provider; its value is never returned.",
        SettingGroup.RECOMMENDATIONS,
        assistant_writable=False,
        effects=_CREDENTIAL_EFFECTS + _PROVIDER_EFFECTS[1:],
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "curator.ollama_url",
        "Local AI server address",
        "The Ollama or compatible local server address used for provider-backed theme authoring.",
        SettingGroup.RECOMMENDATIONS,
        prerequisites=(
            _prerequisite(
                "curator.provider", ("ollama", "openai_compatible"), "Used by local or compatible providers."
            ),
        ),
        assistant_writable=False,
        effects=_CONNECTION_EFFECTS + _PROVIDER_EFFECTS[1:],
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "curator.openai_base_url",
        "OpenAI-compatible API address",
        "The API root, usually ending in /v1, for an OpenAI-compatible model server.",
        SettingGroup.RECOMMENDATIONS,
        prerequisites=(
            _prerequisite("curator.provider", ("openai_compatible",), "Used only by an OpenAI-compatible provider."),
        ),
        assistant_writable=False,
        effects=_CONNECTION_EFFECTS + _PROVIDER_EFFECTS[1:],
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "row.name_template",
        "Default row name",
        "The Plex title for the built-in per-person row; {library_name} is replaced by each library's name.",
        SettingGroup.ROW_DEFAULTS,
        effects=(
            LOCAL,
            _effect(Effect.PLEX_WRITE, EffectTiming.QUEUED, "Renames the built-in row's owned Plex collections."),
        ),
    ),
    _setting(
        "row.size",
        "Default row size",
        "How many titles the built-in row aims to deliver for each person and library.",
        SettingGroup.ROW_DEFAULTS,
        range=NumericRange(minimum=MIN_ROW_SIZE, maximum=MAX_ROW_SIZE, unit="titles"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "rows.manage_shelf_order",
        "Manage Recommended shelf order",
        "Whether Shortlist re-applies its owned rows' positions on Plex's Recommended shelf after a run.",
        SettingGroup.ROW_DEFAULTS,
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "candidates.sources",
        "Title sources",
        "The candidate sources gathered for ordinary recommendation rows; more sources widen recall.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(tuple(sorted(KNOWN_SOURCES))),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "llm_web.search_provider",
        "Web-search provider",
        "The single search backend used by the AI web-search title source.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(
            ("native", "exa", "searxng"), {"native": "AI provider's native search", "exa": "Exa", "searxng": "SearXNG"}
        ),
        prerequisites=(
            _prerequisite(
                "candidates.sources", ("llm_web",), "Relevant when AI web search is an enabled title source."
            ),
        ),
        effects=_PROVIDER_EFFECTS,
    ),
    _setting(
        "llm_web.instructions",
        "Web-search guidance",
        "Owner guidance that replaces the built-in AI web-search instructions for rows without their own instructions.",
        SettingGroup.RECOMMENDATIONS,
        prerequisites=(
            _prerequisite(
                "candidates.sources", ("llm_web",), "Relevant when AI web search is an enabled title source."
            ),
        ),
        effects=_PROVIDER_EFFECTS,
    ),
    _setting(
        "exa.apikey",
        "Exa API key",
        "The encrypted credential used by the Exa web-search source; its value is never returned.",
        SettingGroup.RECOMMENDATIONS,
        assistant_writable=False,
        prerequisites=(
            _prerequisite("llm_web.search_provider", ("exa",), "Required when Exa is the web-search provider."),
        ),
        effects=(
            *_CREDENTIAL_EFFECTS,
            _effect(Effect.PROVIDER_SPEND, EffectTiming.FUTURE_RUN, "Future Exa searches can incur usage charges."),
        ),
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "exa.search_type",
        "Exa search depth",
        "How thoroughly Exa searches for each seed, affecting recall, latency, and per-search cost.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(tuple(EXA_SEARCH_TYPES)),
        prerequisites=(_prerequisite("llm_web.search_provider", ("exa",), "Used only by Exa web search."),),
        effects=_PROVIDER_EFFECTS,
    ),
    _setting(
        "searxng.url",
        "SearXNG address",
        "The self-hosted SearXNG address whose JSON search API Shortlist queries.",
        SettingGroup.RECOMMENDATIONS,
        assistant_writable=False,
        prerequisites=(
            _prerequisite("llm_web.search_provider", ("searxng",), "Required when SearXNG is the web-search provider."),
        ),
        effects=(
            *_CONNECTION_EFFECTS,
            _effect(
                Effect.PERSONAL_DATA_DISCLOSURE,
                EffectTiming.FUTURE_RUN,
                "History-derived search queries can be sent to SearXNG and its upstream engines.",
            ),
        ),
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "searxng.username",
        "SearXNG username",
        "Optional reverse-proxy username sent to the configured SearXNG service.",
        SettingGroup.RECOMMENDATIONS,
        assistant_writable=False,
        prerequisites=(_prerequisite("llm_web.search_provider", ("searxng",), "Used only by SearXNG web search."),),
        effects=_CONNECTION_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "searxng.password",
        "SearXNG password",
        "The encrypted reverse-proxy password sent to SearXNG; its value is never returned.",
        SettingGroup.RECOMMENDATIONS,
        assistant_writable=False,
        prerequisites=(_prerequisite("llm_web.search_provider", ("searxng",), "Used only by SearXNG web search."),),
        effects=_CREDENTIAL_EFFECTS,
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "recommendations.watched_pct",
        "Already-watched allowance",
        "Maximum share of an ordinary row that may contain titles a person has already finished.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=1, unit="fraction"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.refresh_days",
        "Refresh cadence",
        "Days between scheduled row rebuilds; zero keeps a row fixed apart from watched-title replacement.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=MAX_REFRESH_DAYS, unit="days"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.idle_hold_days",
        "Idle refresh hold",
        "Maximum days a due row may wait for new watch activity before Shortlist rebuilds it anyway; "
        "zero disables the hold.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=MAX_REFRESH_DAYS, unit="days"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.recency",
        "Recent-release preference",
        "How strongly newer releases are favoured while ranking; zero ignores age and one applies the full weight.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=1, unit="weight"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.genre_avoidance",
        "Genre variety",
        "How strongly ranking avoids repeating genres already selected for the same row.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=1, unit="weight"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.franchise",
        "Franchise affinity",
        "How strongly shared franchises influence recommendation ranking; zero disables this signal.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=1, unit="weight"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.cast",
        "Cast affinity",
        "How strongly shared cast members influence recommendation ranking; zero disables this signal.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=1, unit="weight"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.recent_count",
        "Web-search watch count",
        "How many recent watches the AI web-search source searches for each row.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=1, maximum=25, unit="watched titles"),
        effects=(*_PROVIDER_EFFECTS, FUTURE_ROW),
    ),
    _setting(
        "recommendations.max_seeds",
        "Recommendation seed count",
        "Maximum watched titles used to seed all candidate sources for a row.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=5, maximum=100, unit="watched titles"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.rating_source",
        "Recommendation rating source",
        "The service whose score is used when a row sorts by highest rated.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(
            ("tmdb", "imdb", "trakt", "tomatoes", "metacritic"),
            {
                "tmdb": "TMDB",
                "imdb": "IMDb",
                "trakt": "Trakt",
                "tomatoes": "Rotten Tomatoes",
                "metacritic": "Metacritic",
            },
        ),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.min_history",
        "Minimum watch history",
        "Watched-title count required before Shortlist uses personal taste instead of the cold-start behavior.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=1, maximum=100, unit="watched titles"),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.cold_start",
        "Cold-start behavior",
        "What a person below the history threshold receives: server-popular titles or no row.",
        SettingGroup.RECOMMENDATIONS,
        options=_options(("popular", "skip"), {"popular": "Popular titles", "skip": "No row"}),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.blocked_shared_seeds",
        "Blocked shared-row seeds",
        "TMDB IDs that may not seed a shared row for the whole server.",
        SettingGroup.RECOMMENDATIONS,
        value_type=ValueType.INTEGER_LIST,
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.use_plex_ratings",
        "Use Plex ratings",
        "Whether low Plex ratings stop a watched title from seeding personal recommendations.",
        SettingGroup.RECOMMENDATIONS,
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "recommendations.dislike_threshold",
        "Plex dislike threshold",
        "A Plex rating at or below this value is treated as a dislike when rating-based filtering is enabled.",
        SettingGroup.RECOMMENDATIONS,
        range=NumericRange(minimum=0, maximum=6, unit="Plex rating"),
        prerequisites=(
            _prerequisite("recommendations.use_plex_ratings", (True,), "Used only when Plex ratings affect seeds."),
        ),
        effects=(LOCAL, FUTURE_ROW),
    ),
    _setting(
        "privacy.hide_shared_from_disabled",
        "Hide shared rows from disabled people",
        "Whether disabling a person also installs exclusions that hide every Shortlist shared row from that account.",
        SettingGroup.ROW_DEFAULTS,
        effects=(
            LOCAL,
            _effect(
                Effect.PLEX_PRIVACY_WRITE,
                EffectTiming.QUEUED,
                "Queues share-filter reconciliation for affected accounts.",
            ),
        ),
        capabilities=(Capability.AUDIENCES_WRITE,),
    ),
    _setting(
        "requests.enabled",
        "Find missing titles",
        "Whether recommendation runs may create candidates for titles not yet available in Plex.",
        SettingGroup.REQUESTS,
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.target",
        "Request destination",
        "Where Shortlist files acquisition requests: Radarr/Sonarr directly or Overseerr/Jellyseerr.",
        SettingGroup.REQUESTS,
        options=_options(tuple(REQUEST_TARGETS), {"arr": "Radarr and Sonarr", "overseerr": "Overseerr or Jellyseerr"}),
        prerequisites=(_prerequisite("requests.enabled", (True,), "Used when missing-title requests are enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.rating_source",
        "Request rating source",
        "The score source used by request thresholds; non-TMDB sources require MDBList.",
        SettingGroup.REQUESTS,
        options=_options(
            ("tmdb", "imdb", "trakt", "tomatoes", "metacritic"),
            {
                "tmdb": "TMDB",
                "imdb": "IMDb",
                "trakt": "Trakt",
                "tomatoes": "Rotten Tomatoes",
                "metacritic": "Metacritic",
            },
        ),
        prerequisites=(_prerequisite("requests.enabled", (True,), "Used when missing-title requests are enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
    ),
    _setting(
        "requests.mdblist.apikey",
        "MDBList API key",
        "The encrypted credential used to fetch IMDb, Trakt, Rotten Tomatoes, or Metacritic scores.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        effects=(*_CREDENTIAL_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.min_rating",
        "Minimum request rating",
        "Lowest score a missing title may have on the selected rating source.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=10, unit="rating"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.language_mode",
        "Request language behavior",
        "Whether preferred original languages are ignored, preferred with a lower bar, or required.",
        SettingGroup.REQUESTS,
        options=_options(
            tuple(LANGUAGE_MODES),
            {"any": "Any language", "prefer": "Prefer selected languages", "only": "Only selected languages"},
        ),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.preferred_languages",
        "Preferred request languages",
        "ISO 639-1 original-language codes used by prefer and only language modes.",
        SettingGroup.REQUESTS,
        prerequisites=(
            _prerequisite(
                "requests.language_mode", ("prefer", "only"), "Used when request language filtering is active."
            ),
        ),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.min_rating_other",
        "Other-language rating floor",
        "Minimum rating for non-preferred-language titles; null follows the main floor plus 1.5.",
        SettingGroup.REQUESTS,
        value_type=ValueType.NUMBER,
        range=NumericRange(minimum=0, maximum=10, unit="rating"),
        nullable=True,
        reset_behavior=ResetBehavior.LITERAL_NULL,
        prerequisites=(_prerequisite("requests.language_mode", ("prefer",), "Used only by preferred-language mode."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.min_votes",
        "Minimum request votes",
        "Minimum vote count a title needs on the selected rating source before it can qualify.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=1_000_000, unit="votes"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.min_demand",
        "Minimum request demand",
        "Distinct people who must want a title before it can enter the request queue.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=1, maximum=1000, unit="people"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.min_year",
        "Earliest request year",
        "Oldest release or first-air year that may qualify; zero removes the lower bound.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=2100, unit="year"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.max_year",
        "Latest request year",
        "Newest release or first-air year that may qualify; zero removes the upper bound.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=2100, unit="year"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.max_per_run",
        "Maximum requests per run",
        "Hard cap on titles a real run may send across all rows and people; zero sends none.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=100, unit="titles"),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.auto_send",
        "Automatically send strong requests",
        "Whether candidates clearing the higher automatic thresholds are sent without manual approval.",
        SettingGroup.REQUESTS,
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.hold_genres",
        "Genres never requested automatically",
        "TMDB movie genre ids. A movie in any of them waits in the request inbox instead of being sent "
        "automatically; the owner can still send it from there.",
        SettingGroup.REQUESTS,
        value_type=ValueType.INTEGER_LIST,
        prerequisites=(_prerequisite("requests.auto_send", (True,), "Used only when automatic sending is enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.hold_tags",
        "TMDB tags never requested automatically",
        'TMDB tag (keyword) ids mapped to their names, e.g. {"156205": "concert film"}. A movie carrying any '
        "of them waits in the request inbox instead of being sent automatically.",
        SettingGroup.REQUESTS,
        prerequisites=(_prerequisite("requests.auto_send", (True,), "Used only when automatic sending is enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.auto_min_demand",
        "Automatic request demand",
        "Distinct people who must want a title before automatic sending is allowed.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=1, maximum=1000, unit="people"),
        prerequisites=(_prerequisite("requests.auto_send", (True,), "Used only when automatic sending is enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.auto_min_rating",
        "Automatic request rating",
        "Minimum selected-source score a title needs before automatic sending is allowed.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=10, unit="rating"),
        prerequisites=(_prerequisite("requests.auto_send", (True,), "Used only when automatic sending is enabled."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.tag",
        "Request tag",
        "Tag applied to every title Shortlist adds to Radarr or Sonarr; blank applies no common tag.",
        SettingGroup.REQUESTS,
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.auto_user_tag",
        "Tag requests by person",
        "Whether Radarr or Sonarr requests also receive the wanting person's Shortlist slug as a tag.",
        SettingGroup.REQUESTS,
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.overseerr.url",
        "Overseerr address",
        "The Overseerr or Jellyseerr address used when that request destination is selected.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("overseerr",), "Used by the Overseerr request destination."),),
        effects=(*_CONNECTION_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.overseerr.apikey",
        "Overseerr API key",
        "The encrypted credential used to submit and inspect Overseerr or Jellyseerr requests.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("overseerr",), "Used by the Overseerr request destination."),),
        effects=(*_CREDENTIAL_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.overseerr.request_as_user_id",
        "Overseerr requesting account",
        "Overseerr user ID used to file requests; zero uses the API key's own account.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=1_000_000, unit="user ID"),
        prerequisites=(_prerequisite("requests.target", ("overseerr",), "Used by the Overseerr request destination."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.radarr.url",
        "Radarr address",
        "The Radarr server address used for missing films.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr/Sonarr requests."),),
        effects=(*_CONNECTION_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.radarr.apikey",
        "Radarr API key",
        "The encrypted credential used to submit and inspect Radarr requests.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr/Sonarr requests."),),
        effects=(*_CREDENTIAL_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.radarr.quality_profile_id",
        "Radarr quality profile",
        "Radarr quality-profile ID assigned to films submitted by Shortlist.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=1_000_000, unit="profile ID"),
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr requests."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.radarr.root_folder",
        "Radarr root folder",
        "Radarr root-folder path assigned to films submitted by Shortlist.",
        SettingGroup.REQUESTS,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr requests."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.sonarr.url",
        "Sonarr address",
        "The Sonarr server address used for missing series.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr/Sonarr requests."),),
        effects=(*_CONNECTION_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.sonarr.apikey",
        "Sonarr API key",
        "The encrypted credential used to submit and inspect Sonarr requests.",
        SettingGroup.REQUESTS,
        assistant_writable=False,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Radarr/Sonarr requests."),),
        effects=(*_CREDENTIAL_EFFECTS, FUTURE_REQUEST),
        capabilities=(Capability.CONNECTIONS_MANAGE, Capability.REQUESTS_SEND),
    ),
    _setting(
        "requests.sonarr.quality_profile_id",
        "Sonarr quality profile",
        "Sonarr quality-profile ID assigned to series submitted by Shortlist.",
        SettingGroup.REQUESTS,
        range=NumericRange(minimum=0, maximum=1_000_000, unit="profile ID"),
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Sonarr requests."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.sonarr.root_folder",
        "Sonarr root folder",
        "Sonarr root-folder path assigned to series submitted by Shortlist.",
        SettingGroup.REQUESTS,
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Sonarr requests."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "requests.sonarr.monitor",
        "Sonarr monitoring mode",
        "How much of a newly added series Sonarr monitors for acquisition.",
        SettingGroup.REQUESTS,
        options=_options(tuple(SONARR_MONITOR_MODES)),
        prerequisites=(_prerequisite("requests.target", ("arr",), "Used by direct Sonarr requests."),),
        effects=(LOCAL, FUTURE_REQUEST),
        capabilities=(Capability.REQUESTS_SEND,),
    ),
    _setting(
        "notify.webhook.enabled",
        "Send webhook notifications",
        "Whether selected Shortlist alerts are also sent to the configured webhook.",
        SettingGroup.NOTIFICATIONS,
        effects=_NOTIFY_EFFECTS,
    ),
    _setting(
        "notify.webhook.url",
        "Webhook address",
        "The encrypted bearer URL that receives selected alerts; its value is never returned.",
        SettingGroup.NOTIFICATIONS,
        assistant_writable=False,
        prerequisites=(
            _prerequisite("notify.webhook.enabled", (True,), "Required when webhook notifications are enabled."),
        ),
        effects=_CREDENTIAL_EFFECTS + _NOTIFY_EFFECTS[1:],
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "notify.webhook.events",
        "Webhook events",
        "The alert event types Shortlist sends to the webhook.",
        SettingGroup.NOTIFICATIONS,
        options=_options(tuple(NOTIFICATION_EVENTS)),
        prerequisites=(
            _prerequisite("notify.webhook.enabled", (True,), "Used when webhook notifications are enabled."),
        ),
        effects=_NOTIFY_EFFECTS,
    ),
    _setting(
        "notify.webhook.auth_header_name",
        "Webhook authentication header",
        "Optional HTTP header name sent with webhook requests; blank sends no authentication header.",
        SettingGroup.NOTIFICATIONS,
        prerequisites=(
            _prerequisite("notify.webhook.enabled", (True,), "Used when webhook notifications are enabled."),
        ),
        effects=_NOTIFY_EFFECTS,
    ),
    _setting(
        "notify.webhook.auth_header_value",
        "Webhook authentication value",
        "The encrypted header value sent with webhook requests; its value is never returned.",
        SettingGroup.NOTIFICATIONS,
        assistant_writable=False,
        prerequisites=(
            _prerequisite("notify.webhook.enabled", (True,), "Used when webhook notifications are enabled."),
        ),
        effects=_CREDENTIAL_EFFECTS + _NOTIFY_EFFECTS[1:],
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "notifications.dismissed",
        "Dismissed alerts",
        "Internal IDs of alerts the owner dismissed; each ID is tied to the state that produced it.",
        SettingGroup.NOTIFICATIONS,
        assistant_writable=False,
        effects=(_effect(Effect.LOCAL_STATE, EffectTiming.IMMEDIATE, "Changes only the owner's local alert state."),),
    ),
    _setting(
        "runs.retention",
        "Run-history retention",
        "Months of detailed run and per-person trace history to retain; zero keeps it forever.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=0, maximum=24, unit="months"),
    ),
    _setting(
        "events.retention",
        "Audit-event retention",
        "Months of audit events to retain; zero keeps the audit trail forever.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=0, maximum=24, unit="months"),
    ),
    _setting(
        "sync.watch_full_days",
        "Missing-title reconciliation interval",
        "Days between passes that act on titles absent from a complete Plex library read.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=1, maximum=90, unit="days"),
        effects=(
            LOCAL,
            _effect(
                Effect.PLEX_WRITE,
                EffectTiming.RECURRING,
                "Future reconciliation can remove owned stale state after complete reads.",
            ),
        ),
        capabilities=(Capability.MAINTENANCE_EXECUTE,),
    ),
    _setting(
        "backup.max_keep",
        "Backups to keep",
        "Maximum number of automatic Shortlist database backups retained on disk.",
        SettingGroup.SYSTEM,
    ),
    _setting(
        "jobs.max_parallel_readonly",
        "Parallel read-only jobs",
        "Maximum read-only jobs that may run together; Plex-writing jobs always remain exclusive.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=1, maximum=8, unit="jobs"),
    ),
    _setting(
        "run.concurrency",
        "People processed concurrently",
        "Maximum people whose reads and AI work overlap in a run; all Plex and plex.tv writes remain serial.",
        SettingGroup.SYSTEM,
        range=NumericRange(minimum=1, maximum=16, unit="people"),
    ),
    _setting(
        "log.level",
        "Log detail",
        "Container log verbosity: TRACE includes full prompts, DEBUG includes run details, and INFO or above is "
        "quieter.",
        SettingGroup.SYSTEM,
        options=_options(("TRACE", "DEBUG", "INFO", "WARNING", "ERROR")),
    ),
    _setting(
        "paused_all",
        "Pause all runs",
        "Stops scheduled and manual runs without disabling people or deleting rows.",
        SettingGroup.SYSTEM,
        effects=(
            LOCAL,
            _effect(
                Effect.SCHEDULER_CHANGE, EffectTiming.IMMEDIATE, "Prevents run execution while the pause is active."
            ),
        ),
        capabilities=(Capability.SCHEDULES_WRITE,),
    ),
    _setting(
        "setup.completed",
        "Setup complete",
        "Internal wizard state recording whether the installation completed initial setup.",
        SettingGroup.SETUP,
        assistant_writable=False,
        effects=(_effect(Effect.LOCAL_STATE, EffectTiming.IMMEDIATE, "Changes setup workflow state."),),
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "setup.step",
        "Setup step",
        "Internal wizard state recording the current initial-setup step.",
        SettingGroup.SETUP,
        assistant_writable=False,
        effects=(_effect(Effect.LOCAL_STATE, EffectTiming.IMMEDIATE, "Changes setup workflow state."),),
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
    _setting(
        "setup.state",
        "Setup form state",
        "Internal structured state retained while the owner completes initial setup.",
        SettingGroup.SETUP,
        assistant_writable=False,
        effects=(_effect(Effect.LOCAL_STATE, EffectTiming.IMMEDIATE, "Changes setup workflow state."),),
        capabilities=(Capability.CONNECTIONS_MANAGE,),
    ),
)


_CRON_DETAILS: dict[str, tuple[str, str, tuple[EffectReference, ...], tuple[Capability, ...]]] = {
    "sync.watch_cron": (
        "Watch-history sync schedule",
        "Cron schedule for refreshing Plex watch history used by recommendation rows.",
        (LOCAL, _effect(Effect.PLEX_READ, EffectTiming.RECURRING, "Reads watch history on the configured schedule.")),
        (Capability.SCHEDULES_WRITE,),
    ),
    "sync.users_cron": (
        "People sync schedule",
        "Cron schedule for refreshing the Plex account roster and related person metadata.",
        (
            LOCAL,
            _effect(
                Effect.PLEX_READ, EffectTiming.RECURRING, "Reads the Plex account roster on the configured schedule."
            ),
        ),
        (Capability.SCHEDULES_WRITE,),
    ),
    "backup.cron": (
        "Backup schedule",
        "Cron schedule for creating a local Shortlist database backup.",
        (
            LOCAL,
            _effect(
                Effect.SCHEDULER_CHANGE, EffectTiming.RECURRING, "Creates local backups on the configured schedule."
            ),
        ),
        (Capability.SCHEDULES_WRITE,),
    ),
    "privacy.sync_cron": (
        "Privacy reconciliation schedule",
        "Cron schedule for merging required Shortlist exclusions into Plex share filters.",
        (
            LOCAL,
            _effect(Effect.PLEX_PRIVACY_WRITE, EffectTiming.RECURRING, "Can merge protective share-filter exclusions."),
        ),
        (Capability.SCHEDULES_WRITE, Capability.AUDIENCES_WRITE),
    ),
    "rows.visibility_cron": (
        "Seasonal visibility schedule",
        "Cron schedule for showing and hiding owned seasonal Plex rows as their windows open and close.",
        (LOCAL, _effect(Effect.PLEX_WRITE, EffectTiming.RECURRING, "Can change visibility of owned seasonal rows.")),
        (Capability.SCHEDULES_WRITE,),
    ),
    "sync.check_cron": (
        "Drift-check schedule",
        "Cron schedule for checking and correcting owned Plex state; a stored blank switches this job off.",
        (
            LOCAL,
            _effect(Effect.PLEX_WRITE, EffectTiming.RECURRING, "Can correct owned Plex state after complete reads."),
        ),
        (Capability.SCHEDULES_WRITE, Capability.MAINTENANCE_EXECUTE),
    ),
    "maintenance.prune_cron": (
        "History pruning schedule",
        "Cron schedule for deleting run and event records older than their retention settings.",
        (LOCAL, _effect(Effect.LOCAL_STATE, EffectTiming.RECURRING, "Deletes expired local history records.")),
        (Capability.SCHEDULES_WRITE,),
    ),
    "themes.rotate_cron": (
        "Explore theme rotation schedule",
        "Cron schedule for rotating due Explore rows to their next saved or newly authored theme.",
        (
            LOCAL,
            _effect(
                Effect.PROVIDER_SPEND,
                EffectTiming.RECURRING,
                "Can author due Explore themes through the configured AI provider.",
            ),
        ),
        (Capability.SCHEDULES_WRITE, Capability.AI_GENERATE),
    ),
}

_SETTINGS += tuple(
    _setting(
        key,
        label,
        description,
        SettingGroup.SCHEDULES,
        nullable=True,
        reset_behavior=ResetBehavior.RESTORE_DEFAULT,
        effects=effects,
        capabilities=capabilities,
    )
    for key, (label, description, effects, capabilities) in _CRON_DETAILS.items()
)


def _validate_registry() -> None:
    expected = (set(DEFAULTS) | SECRET_KEYS) - PRIVATE_KEYS
    actual = {definition.key for definition in _SETTINGS}
    duplicates = len(_SETTINGS) - len(actual)
    if actual != expected or duplicates:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            f"settings catalog coverage mismatch: missing={missing}, extra={extra}, duplicates={duplicates}"
        )
    if set(_CRON_DETAILS) != set(DEFAULT_CRONS):
        raise RuntimeError(
            f"schedule catalog coverage mismatch: missing={sorted(set(DEFAULT_CRONS) - set(_CRON_DETAILS))}, "
            f"extra={sorted(set(_CRON_DETAILS) - set(DEFAULT_CRONS))}"
        )


_validate_registry()


def get_settings_catalog() -> tuple[SettingDefinition, ...]:
    """Return independent typed copies of every public settings definition.

    Returns:
        Settings in stable product order. Private server state is excluded, while write-only public
        credentials are included with ``secret=True`` and no credential value.
    """
    return tuple(definition.model_copy(deep=True) for definition in _SETTINGS)


def get_setting_definition(key: str) -> SettingDefinition:
    """Return a classified setting or fail closed for an unknown key.

    Args:
        key: Exact settings-store key.

    Returns:
        The setting definition.

    Raises:
        KeyError: If the setting lacks catalog and effect classification.
    """
    for definition in _SETTINGS:
        if definition.key == key:
            return definition.model_copy(deep=True)
    raise KeyError(f"unknown setting {key!r}; deny the operation until it is classified")
