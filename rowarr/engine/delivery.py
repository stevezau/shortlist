"""Collection delivery: upsert, order, label, promote — touching only what Rowarr owns."""

from __future__ import annotations

from loguru import logger

from rowarr.engine.clients.plex import PlexClient
from rowarr.engine.models import CollectionDiff, EngineConfig, Pick, UserProfile


def render_row_name(template: str, profile: UserProfile, picks: list[Pick]) -> str:
    top_seed = picks[0].seed_title if picks and picks[0].seed_title else ""
    return template.replace("{top_seed}", top_seed).replace("{user}", profile.username).strip() or "Picked for You"


def deliver_row(
    plex: PlexClient,
    section,
    profile: UserProfile,
    picks: list[Pick],
    config: EngineConfig,
    *,
    dry_run: bool = False,
) -> tuple[CollectionDiff, str]:
    """Upsert the user's collection to exactly `picks`, in order. Returns (diff, stored_label).

    The collection is found by label — never by title — so renames from dynamic templates
    can't orphan it, and foreign (e.g. Kometa) collections are never touched.
    """
    template = profile.row_name_template or config.row_name_template
    title = render_row_name(template, profile, picks)
    label = f"{config.label_prefix}_{profile.slug}"
    collection = plex.find_owned_collection(section, config.label_prefix, profile.slug)

    wanted_titles = [p.title for p in picks]
    if collection is None:
        diff = CollectionDiff(added=wanted_titles, collection_title=title, created=True)
        if dry_run:
            logger.info("[dry-run] {}: would create '{}' with {} items", profile.username, title, len(picks))
            return diff, label
        items = plex.fetch_items([p.rating_key for p in picks])
        collection = plex.create_collection(section, title, items)
    else:
        current_titles = [i.title for i in collection.items()]
        diff = CollectionDiff(
            added=[t for t in wanted_titles if t not in current_titles],
            removed=[t for t in current_titles if t not in wanted_titles],
            kept=[t for t in wanted_titles if t in current_titles],
            collection_title=title,
        )
        if dry_run:
            logger.info(
                "[dry-run] {}: would update '{}' (+{} -{} ={})",
                profile.username,
                title,
                len(diff.added),
                len(diff.removed),
                len(diff.kept),
            )
            return diff, label
        if collection.title != title:
            collection.editTitle(title)
        plex.set_items(collection, plex.fetch_items([p.rating_key for p in picks]))

    stored = plex.stored_label(collection, label)
    # Promotion is deliberately NOT done here: the pipeline promotes only after every user's
    # share filters have been merged, so a new row is never visible before its exclusions exist.
    logger.info("{}: delivered '{}' ({} items, label {})", profile.username, title, len(picks), stored)
    return diff, stored
