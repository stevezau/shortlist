"""Collection delivery: upsert, order, label, promote — touching only what Rowarr owns."""

from __future__ import annotations

from loguru import logger

from rowarr.engine.clients.plex_pms import PlexClient
from rowarr.engine.models import (
    SHARED_SLUG_PREFIX,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    RowSpec,
    UserProfile,
)

DEFAULT_ROW_NAME = "✨ Picked for You"

# Zero-width space / zero-width non-joiner. Both render as nothing.
_INVISIBLE = ("​", "‌")


def row_marker(plex_account_id: int) -> str:
    """An invisible per-account suffix that makes a row's title unique within its library.

    A Plex collection is a TAG on items, keyed by TITLE within a library — not an independent bag
    with its own membership. Two rows sharing a title in one library are therefore ONE membership,
    and every user's row shows the union of everyone's picks: on a live server a film picked for a
    single user turned up in another user's row, carrying one collection tag (SFLIX, 2026-07-13).
    "Picked for You" has to mean picked for YOU, so the titles must differ.

    They must also LOOK identical — nobody wants their own name stapled to their row — so the
    difference is invisible: the account id, written in zero-width characters. Verified against a
    real PMS: the suffix survives the round trip, and two titles differing only by it have separate
    memberships.

    The encoding is injective over the full 64-bit id, so distinct accounts always get distinct
    markers. Truncating it would quietly reintroduce the bug for any two ids congruent modulo the
    cutoff — a collision no test could see.
    """
    return "".join(_INVISIBLE[(plex_account_id >> bit) & 1] for bit in range(64))


def render_row_name(template: str, profile: UserProfile, picks: list[Pick]) -> str:
    """Render the row title as a HUMAN reads it — no marker. Used for reports and the UI.

    A cold-start user has no seed, so a `{top_seed}` template would otherwise render the
    dangling half-sentence "Because you watched" onto a real Plex Home screen.
    """
    top_seed = picks[0].seed_title if picks and picks[0].seed_title else ""
    if "{top_seed}" in template and not top_seed:
        return DEFAULT_ROW_NAME
    rendered = template.replace("{top_seed}", top_seed).replace("{user}", profile.username).strip()
    return rendered or DEFAULT_ROW_NAME


def _allowed_media(media: str) -> set[MediaType]:
    """Which libraries a row writes to. 'both' -> movies and shows; else that one type."""
    if media == "both":
        return {MediaType.MOVIE, MediaType.SHOW}
    return {MediaType(media)}


def deliver_rows(
    plex: PlexClient,
    profile: UserProfile,
    picks: list[Pick],
    config: EngineConfig,
    spec: RowSpec | None = None,
    *,
    sole_row: bool = True,
    dry_run: bool = False,
    stored_labels: dict[str, str] | None = None,
    diff: CollectionDiff | None = None,
) -> tuple[CollectionDiff, str | None]:
    """Deliver one row's picks as one collection per targeted library. Returns (diff, stored label).

    `spec` is the row being delivered; when omitted (the CLI and legacy callers) it defaults to the
    single per-person row, whose name falls through to the profile's / config's template.

    `stored_labels` and `diff` are caller-owned accumulators, written the moment the PMS confirms
    each library's row. A user gets a row per library, so delivery can half-succeed: if the second
    library raises after the first row was created and labelled, a local accumulator would be
    discarded with the exception — and that row would then be missing from `stored_labels`, so NO
    other user's share filter would exclude it. A live row nobody's filter hides is the exact leak
    this whole change exists to prevent, so partial progress has to survive the failure.

    The stored label is None when nothing was delivered — the caller must NOT treat the requested
    label as the stored one. Plex title-cases labels, the excludes written onto other users'
    shares are matched case-insensitively, and a wrongly-cased exclude would therefore look
    "already present" forever and never heal.

    Picks are split by media type because a Plex collection lives in exactly one library, and the
    share-filter excludes that hide it (`filterMovies` / `filterTelevision`) are applied per
    library. A collection holding the wrong type is matched by neither filter and is therefore
    impossible to hide — so "one collection per library" is a privacy requirement, not a nicety.

    A library the user has no picks for is LEFT ALONE: a row nobody wrote to this run still holds
    its items and its label, so the excludes on everyone else's share still hide it. It is merely
    stale, and deleting it would destroy an established row every time an upstream hiccup (a TMDB
    404, a lopsided candidate pool) left a library with no picks for one night.

    Broken rows are NOT this function's problem: `sweep_broken_rows` has already removed them,
    server-wide, before any of this ran — both the kind Plex cannot hide and the kind that shares
    a collection tag with other users' rows.
    """
    if spec is None:  # legacy/default caller: the one per-person row, name from profile/config
        spec = config.default_row_spec()
    # Per-person rows carry the user's shared label; shared rows carry their own. Shared rows use a
    # fixed marker (there's no single owner account) so they resolve to one stable membership.
    wanted_label = spec.label or f"{config.label_prefix}_{profile.slug}"
    marker = row_marker(0) if spec.shared else row_marker(profile.plex_account_id)
    template = spec.name_template or (profile.row_name_template or config.row_name_template)
    # The key `stored_labels` is filed under: per-person rows collapse to one entry per user (all
    # their rows share one label); a shared row files under its own `shared_<slug>` key.
    stored_key = f"{SHARED_SLUG_PREFIX}_{spec.slug}" if spec.shared else profile.slug

    targets = plex.sections_by_type()
    allowed = _allowed_media(spec.media)
    by_type: dict[MediaType, list[Pick]] = {}
    for pick in picks:
        by_type.setdefault(pick.media_type, []).append(pick)

    for kind in by_type:
        if kind not in targets:
            logger.warning("{}: no {} library on this server — dropping those picks", profile.username, kind.value)

    combined = diff if diff is not None else CollectionDiff()
    combined.collection_title = render_row_name(template, profile, picks)
    stored: str | None = None

    for kind, section in targets.items():
        if kind not in allowed:
            continue
        section_picks = by_type.get(kind, [])
        if not section_picks:
            continue
        one, stored = _deliver_one(
            plex, section, profile, section_picks, template, wanted_label, marker, sole_row, dry_run=dry_run
        )
        combined.added += one.added
        combined.removed += one.removed
        combined.kept += one.kept
        combined.deleted += one.deleted
        combined.created = combined.created or one.created
        # Recorded the instant the PMS confirms the label — if the NEXT library blows up, this
        # row still gets excluded on every other user's share this run.
        if stored_labels is not None and not dry_run:
            stored_labels[stored_key] = stored

    return combined, stored


def remove_row(
    plex: PlexClient,
    profile: UserProfile,
    config: EngineConfig,
    spec: RowSpec,
    *,
    dry_run: bool,
    diff: CollectionDiff,
) -> None:
    """Delete a user's collection for a row they've muted, in every targeted library.

    Muting means "you don't get this row" — but a row delivered BEFORE the mute still exists on the
    server, so it must be removed, not merely skipped on the next run. Deleting only makes the server
    strictly more private (the row's `rowarr_<slug>` label keeps it excluded on every other share
    until it's gone), so this is always safe. A row whose title depends on its picks (a `{top_seed}`
    template) can't be reconstructed without them, so it's left for a later sweep; static-titled rows
    — the default row and most custom rows — match exactly and are removed here.
    """
    wanted_label = spec.label or f"{config.label_prefix}_{profile.slug}"
    marker = row_marker(0) if spec.shared else row_marker(profile.plex_account_id)
    template = spec.name_template or (profile.row_name_template or config.row_name_template)
    display = render_row_name(template, profile, [])
    title = display + marker
    for section in plex.sections_by_type().values():
        for collection in plex.find_owned_collections(section, wanted_label):
            if collection.title != title:
                continue
            if dry_run:
                logger.info(
                    "[dry-run] {}: would remove muted row '{}' in '{}'", profile.username, display, section.title
                )
            else:
                plex.delete_owned_collection(collection, config.label_prefix)
                logger.info("{}: removed muted row '{}' in '{}'", profile.username, display, section.title)
            diff.deleted.append(display)


def _create_labelled_collection(
    plex: PlexClient,
    section,
    profile: UserProfile,
    picks: list[Pick],
    *,
    title: str,
    label: str,
    display: str,
) -> str:
    """Create the collection, apply its label, and delete it if the label doesn't stick.

    A collection with no rowarr_* label is invisible to every lookup we have — all of them key off
    that prefix — so nothing would ever find it again, no filter could hide it, and it would be
    visible to everyone forever. Create and label must therefore succeed together or not at all.
    Returns the stored (Plex title-cased) label.
    """
    items = plex.fetch_items([p.rating_key for p in picks])
    collection = plex.create_collection(section, title, items)
    try:
        stored = plex.stored_label(collection, label)
    except Exception:
        # An unlabelled row must not be allowed to outlive this call.
        logger.error("{}: could not label the new row in '{}' — removing it", profile.username, section.title)
        try:
            collection.delete()
        except Exception:
            # Two PMS failures back to back. Name the orphan loudly: it is unlabelled, so no
            # future run can find it, and only a human with this ratingKey can remove it.
            logger.critical(
                "{}: ORPHANED COLLECTION — '{}' (ratingKey {}) in '{}' exists with NO rowarr "
                "label. Rowarr cannot find or remove it and no share filter can hide it. "
                "Delete it in Plex (find it by ratingKey — the title carries invisible "
                "characters and will not match a search).",
                profile.username,
                display,
                getattr(collection, "ratingKey", "?"),
                section.title,
            )
        raise
    logger.info(
        "{}: delivered '{}' to '{}' ({} items, label {})",
        profile.username,
        display,
        section.title,
        len(picks),
        stored,
    )
    return stored


def _deliver_one(
    plex: PlexClient,
    section,
    profile: UserProfile,
    picks: list[Pick],
    template: str,
    wanted_label: str,
    marker: str,
    sole_row: bool,
    *,
    dry_run: bool,
) -> tuple[CollectionDiff, str]:
    """Upsert one library's collection to exactly `picks`, in order. Returns (diff, stored_label).

    A user can have several rows, all carrying their label and told apart by title, so the right one
    is the labelled collection whose title matches. When this is the user's ONLY row (`sole_row`) and
    exactly one labelled collection exists, a title mismatch is treated as an in-place rename — so a
    changed name template updates the row rather than orphaning it. With more than one row that guess
    is unsafe (which row was renamed?), so a mismatch builds a fresh row and the stale one, still
    labelled, stays hidden. Foreign (e.g. Kometa) collections never carry our label, so are untouched.
    """
    display = render_row_name(template, profile, picks)
    # What Plex is told to call it: the same thing, plus an invisible marker that makes it unique
    # in this library. Without it, every user's row is the same collection tag and holds everyone's
    # picks. Users see `display`; only the PMS ever sees the marker.
    title = display + marker
    label = wanted_label
    owned = plex.find_owned_collections(section, label)
    collection = next((c for c in owned if c.title == title), None)
    if collection is None and sole_row and len(owned) == 1 and owned[0].title.endswith(marker):
        # The sole row was renamed by a template change but still carries this account's marker, so
        # its membership is its own: update it in place rather than leave a stale duplicate. Only
        # when there's exactly one (otherwise we can't tell which row moved) and only a MARKED row —
        # a pre-marker row shares its tag with others and must be rebuilt, never renamed.
        collection = owned[0]

    if collection is not None and not plex.matches_section(collection, section):
        # The sweep already deleted this one (or, in a dry run, already reported that it would),
        # so treat it as gone and build a fresh, correctly-typed row in its place. Plex will not
        # re-type a collection: swapping its contents leaves the old subtype, and the row goes on
        # being visible to everyone. It must be rebuilt, never edited.
        logger.info(
            "{}: rebuilding their row in '{}' — the old one was the wrong type", profile.username, section.title
        )
        collection = None

    wanted_titles = [p.title for p in picks]
    if collection is None:
        diff = CollectionDiff(added=wanted_titles, collection_title=display, created=True)
        if dry_run:
            logger.info(
                "[dry-run] {}: would create '{}' in '{}' with {} items",
                profile.username,
                display,
                section.title,
                len(picks),
            )
            return diff, label
        stored = _create_labelled_collection(plex, section, profile, picks, title=title, label=label, display=display)
        return diff, stored

    current_titles = [i.title for i in collection.items()]
    diff = CollectionDiff(
        added=[t for t in wanted_titles if t not in current_titles],
        removed=[t for t in current_titles if t not in wanted_titles],
        kept=[t for t in wanted_titles if t in current_titles],
        collection_title=display,  # the human title: the marker is Plex's business, not the owner's
    )
    if dry_run:
        logger.info(
            "[dry-run] {}: would update '{}' in '{}' (+{} -{} ={})",
            profile.username,
            display,
            section.title,
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
    logger.info(
        "{}: delivered '{}' to '{}' ({} items, label {})", profile.username, display, section.title, len(picks), stored
    )
    return diff, stored


def sweep_broken_rows(
    plex: PlexClient,
    config: EngineConfig,
    *,
    markers: dict[str, str] | None = None,
    dry_run: bool = False,
    deleted: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Delete every Rowarr row on the SERVER that is broken beyond repair-in-place.

    Two kinds, and both are only fixable by rebuilding:

    * **Unhidable** — its type doesn't match its library, so neither `filterMovies` nor
      `filterTelevision` can match it and EVERY account can see it.
    * **Shared-tag** — its title lacks its owner's marker, so it shares a collection tag with the
      other rows in that library and holds their picks as well as its owner's. Its owner opens
      "Picked for You" and reads other people's recommendations.

    `markers` maps slug -> the invisible marker that row's title must end with. A slug that isn't
    in it belongs to an account Rowarr can't identify — it could not rebuild that row, so it leaves
    it alone rather than destroy something it cannot replace.

    Returns slug -> titles.

    `deleted` lets the caller own the accumulator, so that what was ALREADY deleted survives an
    exception part-way through the walk. Deleting rows and then losing the record of it because
    the next PMS call timed out would leave "whose row did you delete at 03:31" unanswerable —
    which is the one question the audit trail exists to answer (plex-safety rule 10).

    A row whose type doesn't match its library is matched by neither `filterMovies` nor
    `filterTelevision`. Its `label!=` exclude does nothing, so it is visible to EVERY user on the
    server for as long as it exists — that is how the rows an older version stranded in the wrong
    library keep leaking, and removing them is the only reason this deletes anything. A well-typed
    row is never touched: it still carries its label, so the excludes still hide it. It is stale,
    not leaking.

    Two things about the scope, both load-bearing:

    It walks the SERVER, not tonight's user list. Whether a row can be hidden has nothing to do
    with whether its owner is enabled, paused, or included in this run — so a leak belonging to a
    user we are not processing (or a run where `paused_all` means we process nobody) must still be
    cleaned up. Scoping this to `users` would make one click of "pause" turn a leak permanent.

    It runs BEFORE anything that can fail. Recommendations depend on TMDB, Tautulli and the PMS,
    any of which can raise — and a leaking row must not survive the night because a rate limit
    stopped us from computing what to put in the row that replaces it.
    """
    prefix = f"{config.label_prefix}_".lower()
    markers = markers or {}
    deleted = {} if deleted is None else deleted
    for section in plex.sections():
        for collection in section.collections():
            label = next((t.tag for t in collection.labels if t.tag.lower().startswith(prefix)), None)
            if label is None:  # not ours — Kometa and friends are none of our business (rule 4)
                continue
            slug = label[len(prefix) :].lower()
            marker = markers.get(slug)
            unhidable = not plex.matches_section(collection, section)
            shares_tag = marker is not None and not collection.title.endswith(marker)
            if not unhidable and not shares_tag:
                continue
            reason = (
                "it is the wrong type for that library, so no share filter can hide it and every user can see it"
                if unhidable
                else "it shares a collection tag with other users' rows, so it holds their picks too"
            )
            logger.warning(
                "{}{}: removing their row in '{}' — {}", "[dry-run] " if dry_run else "", slug, section.title, reason
            )
            # Delete THEN record, so the audit says what actually happened: recording first would
            # report a deletion that a failing PMS call never made. (Read the title first — after
            # the delete the object no longer refers to anything on the server.)
            title = collection.title
            if not dry_run:
                plex.delete_owned_collection(collection, config.label_prefix)
            deleted.setdefault(slug, []).append(title)
    return deleted
