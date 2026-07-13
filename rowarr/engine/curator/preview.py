"""Fixed sample inputs for the prompt preview — one profile + candidate set the UI curates against.

Lives in the engine so the recipe->prompt preview the owner sees is built from the same building
blocks a real run uses (``build_prompts``), never a router's ad-hoc copy of them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rowarr.engine.models import Candidate, MediaType, PromptConfig, Seed, UserProfile, UserType, WatchedItem


def sample_preview_inputs(prompt: PromptConfig) -> tuple[UserProfile, list[Candidate]]:
    """A representative (profile, candidates) pair for previewing ``prompt`` via ``build_prompts``.

    The profile carries the recipe under test and a small, PII-free watch history; the candidates
    are library-verified titles with seeds, so the preview shows exactly what a real curate call
    would receive.
    """
    watched_at = datetime(2026, 1, 1, tzinfo=UTC)
    seed = Seed(tmdb_id=146233, title="Prisoners", media_type=MediaType.MOVIE, weight=2.0)
    profile = UserProfile(
        username="Sarah",
        plex_account_id=0,
        user_type=UserType.SHARED,
        history=[
            WatchedItem("Prisoners", MediaType.MOVIE, watched_at, 146233, 2013, 1, 1.0),
            WatchedItem("Nightcrawler", MediaType.MOVIE, watched_at, 242582, 2014, 2, 1.0),
        ],
        prompt=prompt,
    )
    candidates = [
        Candidate(273481, "Sicario", MediaType.MOVIE, 2015, ["Thriller", "Crime"], 7.6, 8600, [seed], 10),
        Candidate(398978, "Wind River", MediaType.MOVIE, 2017, ["Thriller", "Mystery"], 7.4, 4200, [seed], 11),
    ]
    return profile, candidates
