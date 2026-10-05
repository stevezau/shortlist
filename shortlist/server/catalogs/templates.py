"""Authoritative row-template catalog shared with the frontend and assistants."""

from __future__ import annotations

from copy import deepcopy

from shortlist.engine.seasons import PRESETS

from .models import Capability, RowTemplateDefinition, SeasonPresetReference

# Mirrors the complete CollectionInput shape used for a new row. Template values are deliberately
# partial; effective_values resolves them over this mapping for assistant planning.
ROW_INPUT_DEFAULTS: dict[str, object] = {
    "name": "",
    "defer_rename": False,
    "build": "per_person",
    "audience": "everyone",
    "audience_user_ids": [],
    "enabled": True,
    "schedule": "30 3 * * *",
    "size": 15,
    "media": "both",
    "sort_order": 0,
    "name_template": "",
    "fallback_name": "",
    "description": "",
    "sort_title_prefix": "",
    "min_watchers": 2,
    "request_tag": "",
    "candidate_sources": [],
    "library_keys": [],
    "watched_pct": None,
    "rewatch": False,
    "rewatch_cooldown_days": 30,
    "requests_row": False,
    "requests_window_days": 90,
    "requests_tag_pattern": "",
    "unstarted_only": False,
    "refresh_days": None,
    "idle_hold_days": None,
    "recency": None,
    "recent_count": None,
    "max_seeds": None,
    "max_runtime": None,
    "min_year": None,
    "max_year": None,
    "min_rating": None,
    "cold_start": None,
    "req_min_rating": None,
    "req_min_votes": None,
    "req_min_demand": None,
    "req_min_year": None,
    "req_max_year": None,
    "req_auto_send": None,
    "req_auto_min_demand": None,
    "req_auto_min_rating": None,
    "req_max_per_row": None,
    "req_radarr_quality_profile_id": None,
    "req_radarr_root_folder": None,
    "req_sonarr_quality_profile_id": None,
    "req_sonarr_root_folder": None,
    "req_sonarr_monitor": None,
    "req_language_mode": None,
    "req_preferred_languages": None,
    "req_min_rating_other": None,
    "req_auto_user_tag": None,
    "seed_window": 1,
    "pick_order": "best",
    "placement": "both",
    "placement_friends": "both",
    "show_days": [],
    "seasons": [],
    "season_lead_days": 30,
    "season_after_days": 0,
    "pin_top": False,
    "hub_anchor": {},
    "poster": {"mode": "", "title": "", "subtitle": "", "style": ""},
    "ai_instructions": {"mode": "default", "text": ""},
    "theme_id": None,
    "theme_mode": "fixed",
    "explore_brief": "",
    "theme_days": None,
    "refresh_share": None,
    "repeat_cooldown_days": None,
    "avoid_rows": None,
}


_TEMPLATES: tuple[dict[str, object], ...] = (
    {
        "id": "picked-for-you",
        "kind": "picked",
        "emoji": "✨",
        "title": "Picked for You",
        "summary": "A little of everything they love",
        "description": "The everyday row. Blends someone's whole recent history into a general set of suggestions.",
        "highlights": ("One row each", "15 picks", "Other settings from your defaults"),
        "values": {"name": "✨ {library_name} Picks", "build": "per_person", "size": 15},
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("At least one permitted Plex library and audience member.",),
        "audience_behavior": "Creates a separate recommendation row for each selected person.",
    },
    {
        "id": "because-you-watched",
        "kind": "byw",
        "emoji": "🎯",
        "title": "Because you watched…",
        "summary": "One favourite leads to another",
        "description": (
            "Names one recent film and fills the row with things like it. The title tells them why it's there."
        ),
        "highlights": ("Films only", "All sources: 1 watch", "Follows their latest watch"),
        "values": {
            "name": "🎯 Because you watched {top_seed}",
            "build": "per_person",
            "max_seeds": 1,
            "recent_count": 1,
            "media": "movie",
            "size": 20,
            "refresh_days": 1,
            "seed_window": 1,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("Each recipient needs at least one recent film in watch history.",),
        "audience_behavior": "Creates one film row per selected person from that person's latest watch.",
    },
    {
        "id": "seen-it-already",
        "kind": "again",
        "emoji": "☕",
        "title": "Watch it again",
        "summary": "Favourites worth another look",
        "description": (
            "Films and shows they've already finished, put back in front of them — a shelf of old favourites "
            "rather than new suggestions."
        ),
        "highlights": ("Rewatches come first", "Changes slowly"),
        "values": {
            "name": "☕ {library_name} you've already seen",
            "build": "per_person",
            "rewatch": True,
            "watched_pct": 1,
            "refresh_days": 11,
            "size": 15,
        },
        "required_services": ("Plex",),
        "prerequisites": ("Recipients need completed titles in watch history.",),
        "audience_behavior": "Creates a separate rewatch row from each selected person's completed titles.",
    },
    {
        "id": "your-requests",
        "kind": "requests",
        "emoji": "📬",
        "title": "Your requests",
        "summary": "What they asked for, ready to watch",
        "description": (
            "What they asked for in Overseerr, once it's on Plex. Each title leaves once they've watched it."
        ),
        "highlights": ("Only what they asked for", "Newest first", "Overseerr or Radarr/Sonarr tags"),
        "values": {
            "name": "📬 {library_name} you asked for",
            "build": "per_person",
            "requests_row": True,
            "requests_window_days": 90,
            "size": 20,
        },
        "required_services": ("Plex", "Overseerr or Radarr/Sonarr request tags"),
        "prerequisites": ("Request attribution must be available from Overseerr or request tags.",),
        "audience_behavior": "Creates one row per person containing titles attributed to that person's requests.",
    },
    {
        "id": "fresh-finds",
        "kind": "picked",
        "emoji": "🌱",
        "title": "Fresh finds",
        "summary": "Something new, every evening",
        "description": (
            "Titles refresh every night, nothing they've seen. For people who want something new each evening."
        ),
        "highlights": ("Refreshes nightly", "Nothing already watched"),
        "values": {
            "name": "🌱 New {library_name} to try",
            "build": "per_person",
            "refresh_days": 1,
            "idle_hold_days": 0,
            "watched_pct": 0,
            "size": 15,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("At least one permitted Plex library and audience member.",),
        "audience_behavior": "Creates a separate nightly row for each selected person.",
    },
    {
        "id": "seasonal",
        "kind": "seasonal",
        "emoji": "🗓️",
        "title": "Seasonal",
        "summary": "The right films at the right time",
        "description": (
            "One shared row of the most-watched seasonal films. Follows the holidays you choose and stays hidden "
            "between seasons."
        ),
        "highlights": (
            "Shared with everyone",
            "Needs 2 watchers",
            "Halloween, Christmas & Valentine's, or your own",
            "Shows a month before",
            "Rebuilt nightly",
        ),
        "values": {
            "name": "{season_emoji} {season} picks",
            "build": "shared",
            "min_watchers": 2,
            "media": "movie",
            "size": 15,
            "seasons": ["valentines", "halloween", "christmas"],
            "season_lead_days": 30,
            "season_after_days": 0,
            "refresh_days": 1,
            "recency": 0,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("The selected film library needs titles matching the chosen seasons.",),
        "audience_behavior": "Creates one shared row for the explicit audience; it is hidden outside active seasons.",
    },
    {
        "id": "from-the-vault",
        "kind": "picked",
        "emoji": "🕰️",
        "title": "From the vault",
        "summary": "A shelf that takes its time",
        "description": (
            "Built once and never re-picked on a schedule. A shelf that stays put apart from titles they've watched, "
            "which are replaced."
        ),
        "highlights": ("Never refreshes on its own", "Only moves as they watch it"),
        "values": {
            "name": "🕰️ {library_name} from the vault",
            "build": "per_person",
            "refresh_days": 0,
            "watched_pct": 0,
            "size": 20,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("At least one permitted Plex library and audience member.",),
        "audience_behavior": "Creates a stable row for each selected person and replaces watched titles.",
    },
    {
        "id": "popular-here",
        "kind": "popular",
        "emoji": "👥",
        "title": "Popular on this server",
        "summary": "What everyone\N{RIGHT SINGLE QUOTATION MARK}s watching",
        "description": (
            "One row everybody sees, built only from titles several people have watched. Nothing personal in it."
        ),
        "highlights": ("Shared with everyone", "Needs 3 watchers"),
        "values": {
            "name": "👥 Popular {library_name} on this server",
            "build": "shared",
            "min_watchers": 3,
            "size": 20,
        },
        "required_services": ("Plex",),
        "prerequisites": ("At least three people must have watched a title before it qualifies.",),
        "audience_behavior": "Creates one non-personal shared row for the explicit audience.",
    },
    {
        "id": "movie-night",
        "kind": "picked",
        "emoji": "🍿",
        "title": "Movie night",
        "summary": "Ten films. One good evening.",
        "description": "Ten films picked for each person, refreshed weekly. A short list for their next movie night.",
        "highlights": ("Movies only", "10 picks", "Weekly"),
        "values": {
            "name": "🍿 Tonight's {library_name}",
            "build": "per_person",
            "media": "movie",
            "size": 10,
            "refresh_days": 7,
            "idle_hold_days": 0,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("At least one permitted film library and audience member.",),
        "audience_behavior": "Creates a separate weekly film row for each selected person.",
    },
    {
        "id": "more-tv",
        "kind": "picked",
        "emoji": "📺",
        "title": "More TV to watch",
        "summary": "Their next series starts here",
        "description": "Series they have never opened — not one they're part-way through. A shelf of things to start.",
        "highlights": ("TV only", "Never started"),
        "values": {
            "name": "📺 More {library_name} to watch",
            "build": "per_person",
            "media": "show",
            "unstarted_only": True,
            "watched_pct": 0,
            "size": 10,
        },
        "required_services": ("Plex", "TMDB"),
        "prerequisites": ("At least one permitted TV library and audience member.",),
        "audience_behavior": "Creates a separate unstarted-series row for each selected person.",
    },
    {
        "id": "describe-a-row",
        "kind": "ai",
        "emoji": "🪄",
        "title": "Describe a row",
        "summary": "Say what you want. The AI builds the list.",
        "description": (
            "Describe a row in your own words, like “films with a twist ending”. The AI writes the list once from "
            "your words, and Shortlist picks from it for each person every run. No more AI after that."
        ),
        "highlights": ("One row each", "The AI writes the list once", "Live like any other row"),
        "values": {
            "name": "{theme_emoji} {theme}",
            "build": "per_person",
            "enabled": True,
            "size": 15,
            "recency": 0,
        },
        "required_services": ("Plex", "TMDB", "Configured AI provider"),
        "prerequisites": ("An AI provider must be configured to author the initial theme.",),
        "audience_behavior": "Creates a separate row for each selected person from one AI-authored title pool.",
        "required_capabilities": (Capability.ROWS_CREATE, Capability.AI_GENERATE),
    },
)


def _template(definition: dict[str, object]) -> RowTemplateDefinition:
    values = deepcopy(definition["values"])
    effective = deepcopy(ROW_INPUT_DEFAULTS)
    effective.update(values)
    metadata = {key: value for key, value in definition.items() if key != "values"}
    return RowTemplateDefinition(
        **metadata,
        values=values,
        effective_values=effective,
        changed_fields=tuple(values),
        editable_fields=tuple(ROW_INPUT_DEFAULTS),
    )


def get_template_catalog() -> tuple[RowTemplateDefinition, ...]:
    """Return independent typed copies of every row template.

    Returns:
        Templates in the frontend gallery's display order. Mutating a returned nested value cannot
        change later calls.
    """
    return tuple(_template(definition) for definition in _TEMPLATES)


def get_template_definition(template_id: str) -> RowTemplateDefinition:
    """Return a row template or fail closed for an unknown ID.

    Args:
        template_id: Stable template identifier returned by the catalog.

    Returns:
        The requested row template.

    Raises:
        KeyError: If the template is not classified in this catalog.
    """
    for template in get_template_catalog():
        if template.id == template_id:
            return template
    raise KeyError(f"unknown row template {template_id!r}")


def get_season_preset_catalog() -> tuple[SeasonPresetReference, ...]:
    """Return descriptive references to the existing engine-owned season presets."""
    return tuple(
        SeasonPresetReference(
            key=preset.key,
            label=preset.label,
            description=preset.description,
            category=preset.category,
            note=preset.note,
        )
        for preset in PRESETS
    )
