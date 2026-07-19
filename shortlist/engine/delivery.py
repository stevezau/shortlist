"""Collection delivery: upsert, order, label, promote — touching only what Shortlist owns."""

from __future__ import annotations

import time
from dataclasses import replace

from loguru import logger

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.poster import PosterArtist
from shortlist.engine.models import (
    SHARED_SLUG_PREFIX,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    PosterSpec,
    RowSpec,
    UserProfile,
)

DEFAULT_ROW_NAME = "✨ Picked for You"

# When a row's update would remove at least this many items, rebuild the collection (delete + one
# batched create) instead of firing that many per-item removeItems DELETEs. plexapi has no bulk
# remove, and on a slow library each DELETE is expensive (SFLIX TV rows ~15s each), so a big turnover
# is far cheaper as a single create. Small deltas keep the in-place update path (no needless rebuild).
_REBUILD_MIN_REMOVES = 5

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


def strip_marker(title: str) -> str:
    """A collection's human title with the invisible per-account marker removed — for display in
    audits and for matching a delivered display name against what Plex stores.

    The marker is ALWAYS exactly 64 marker-chars (``row_marker``), so strip that fixed-width suffix
    rather than every trailing invisible char — a human title that legitimately ends in one is kept.
    """
    suffix = title[-64:]
    if len(suffix) == 64 and all(c in _INVISIBLE for c in suffix):
        return title[:-64]
    return title


def has_marker(title: str) -> bool:
    """Whether a title ends with a valid 64-char Shortlist marker — proof the collection is ours even
    when its ``shortlist_*`` label is missing (an orphan from an interrupted run). No other tool
    produces a 64-char zero-width suffix, so this is a safe ownership test (plex-safety rule 4)."""
    return strip_marker(title) != title


def marker_account(title: str) -> int | None:
    """Decode the Plex account id a marker encodes (inverse of ``row_marker``), or None if unmarked —
    so an unlabelled orphan can still be attributed to a user in the audit trail."""
    if not has_marker(title):
        return None
    suffix = title[-64:]
    return sum((1 << bit) for bit, c in enumerate(suffix) if c == _INVISIBLE[1])


def render_row_name(template: str, profile: UserProfile, picks: list[Pick], library_name: str = "") -> str:
    """Render the row title as a HUMAN reads it — no marker. Used for reports and the UI.

    ``library_name`` fills the ``{library_name}`` placeholder with the delivering library's own name,
    so the same row gets a distinct title per library (a privacy requirement: per-person rows share one
    label and are told apart only by title). Every caller that renders a title to MATCH a collection on
    the PMS — deliver, promote, mute/retire, rename — must pass the SAME library name delivery used, or
    it would look for a title delivery never wrote and silently no-op (a row could stay unhidden). With
    no library (a preview, or the row-level combined summary), the empty placeholder is collapsed away
    ("✨  Picked for You" -> "✨ Picked for You" == DEFAULT_ROW_NAME).

    A cold-start user has no seed, so a `{top_seed}` template would otherwise render the
    dangling half-sentence "Because you watched" onto a real Plex Home screen.
    """
    top_seed = picks[0].seed_title if picks and picks[0].seed_title else ""
    if "{top_seed}" in template and not top_seed:
        return DEFAULT_ROW_NAME
    rendered = (
        template.replace("{top_seed}", top_seed)
        .replace("{user}", profile.username)
        .replace("{library_name}", library_name)
    )
    # A {library_name} title with no (or a padding-adjacent) library leaves double spaces where the
    # placeholder was — collapse runs of whitespace so the human title reads clean either way.
    rendered = " ".join(rendered.split()) if "{library_name}" in template else rendered.strip()
    return rendered or DEFAULT_ROW_NAME


def render_poster_text(field_value: str, profile: UserProfile, picks: list[Pick], library_name: str) -> str:
    """Fill a poster text field's placeholders (``{user}``/``{library_name}``/``{top_seed}``) for the
    user and library it lands on, using the same helper delivery uses for titles.

    ``render_row_name`` substitutes DEFAULT_ROW_NAME for a ``{top_seed}`` template with no seed, so a
    blank/whitespace field or an unrenderable ``{top_seed}`` collapses to "" (dropped) rather than
    turning into "✨ Picked for You".
    """
    field_value = field_value.strip()
    if not field_value:
        return ""
    rendered = render_row_name(field_value, profile, picks, library_name=library_name)
    return "" if rendered == DEFAULT_ROW_NAME and "{top_seed}" in field_value else rendered


# Poster modes that produce an image from text (vs "upload", which carries its own bytes). Each maps
# to a render engine the injected artist understands. "generate" is the pre-text-engine name for "ai".
_POSTER_TEXT_ENGINES = {"text": "text", "ai": "ai", "generate": "ai"}


def apply_poster(
    plex: PlexClient,
    collection,
    poster: PosterSpec | None,
    profile: UserProfile,
    picks: list[Pick],
    *,
    library_name: str,
    artist: PosterArtist | None,
    dry_run: bool,
) -> None:
    """Set a row's custom poster on its Plex collection — best-effort and cosmetic.

    Only ever touches the artwork of a collection Shortlist owns, does not promote or change any
    filter, and NEVER raises into delivery: a failed poster leaves the row exactly as it was, just
    with Plex's own artwork. "text" always works (Pillow, no key); "ai" needs an image-capable
    provider and is quietly skipped otherwise so the row still delivers.
    """
    if poster is None or not poster.mode:
        return
    try:
        if dry_run:
            logger.info("[dry-run] {}: would set a {} poster on this row", profile.username, poster.mode)
            return
        if poster.mode == "upload":
            image = poster.image
        elif poster.mode in _POSTER_TEXT_ENGINES:
            if artist is None:
                logger.debug("{}: no poster artist available — skipping poster in '{}'", profile.username, library_name)
                return
            image = artist.render(
                title=render_poster_text(poster.title, profile, picks, library_name),
                subtitle=render_poster_text(poster.subtitle, profile, picks, library_name),
                style=poster.style,
                engine=_POSTER_TEXT_ENGINES[poster.mode],
            )
        else:
            return
        if not image:
            logger.debug("{}: {} poster produced no image — leaving Plex artwork", profile.username, poster.mode)
            return
        plex.upload_poster(collection, image)
        logger.info("{}: set a {} poster on this row in '{}'", profile.username, poster.mode, library_name)
    except Exception as exc:  # cosmetic: a poster must never break delivery
        # Log only the exception TYPE, not its message — a provider auth error can carry a key fragment.
        logger.warning("{}: couldn't set the poster ({})", profile.username, type(exc).__name__)


def resolve_row_template(spec: RowSpec, profile: UserProfile, config: EngineConfig) -> str:
    """The row-name template to render, most-specific wins: the row's own template, else the user's
    per-user override, else the global default.

    This MUST be the single source of truth for that precedence. The promote phase renders the
    delivered collection's title from the same resolution to find the row it just wrote; if a caller
    resolved the template differently, promote would look for a title delivery never created and the
    row's placement/privacy promotion would silently no-op (plex-safety: a row could stay unhidden).
    """
    return spec.name_template or (profile.row_name_template or config.row_name_template)


def _allowed_media(media: str) -> set[MediaType]:
    """Which media types a row writes to. 'both' -> movies and shows; else that one type."""
    if media == "both":
        return {MediaType.MOVIE, MediaType.SHOW}
    return {MediaType(media)}


def section_kind(section) -> MediaType:
    return MediaType.MOVIE if section.type == "movie" else MediaType.SHOW


def sections_for_keys(sections: list, library_keys) -> list:
    """The sections a row's ``library_keys`` name, in ``sections`` order.

    str() on BOTH sides is load-bearing: the pool-narrowing half of the decision
    (rows.row_library_index) coerces too, so if either side ever compared an int the two would
    silently disagree — the row would curate fine and land in no library at all.
    """
    wanted = {str(key) for key in library_keys}
    return [s for s in sections if str(s.key) in wanted]


def target_sections(sections: list, spec: RowSpec) -> list:
    """The libraries this row delivers into: the specific ones it named (``library_keys``), else
    every library of an allowed media type. A named key that no longer exists is simply skipped."""
    allowed = _allowed_media(spec.media)
    candidates = [s for s in sections if section_kind(s) in allowed]
    return sections_for_keys(candidates, spec.library_keys) if spec.library_keys else candidates


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
    sections: list | None = None,
    section_index: dict[str, dict[int, int]] | None = None,
    section_picks: dict[str, list[Pick]] | None = None,
    breakdown: list[dict] | None = None,
    poster_artist: PosterArtist | None = None,
    order_work: list[tuple] | None = None,
) -> tuple[CollectionDiff, str | None]:
    """Deliver one row's picks as one collection per targeted library. Returns (diff, stored label).

    `spec` is the row being delivered; when omitted (legacy callers) it defaults to the
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
    template = resolve_row_template(spec, profile, config)
    # The key `stored_labels` is filed under: per-person rows collapse to one entry per user (all
    # their rows share one label); a shared row files under its own `shared_<slug>` key.
    stored_key = f"{SHARED_SLUG_PREFIX}_{spec.slug}" if spec.shared else profile.slug

    # The libraries this row targets: the ones it named (library_keys), else all of the allowed
    # type. Fall back to sections_by_type() (one per type) for a legacy caller that passed neither.
    all_sections = sections if sections is not None else list(plex.sections_by_type().values())
    idx = section_index if section_index is not None else {}
    targets = target_sections(all_sections, spec)

    by_type: dict[MediaType, list[Pick]] = {}
    for pick in picks:
        by_type.setdefault(pick.media_type, []).append(pick)

    combined = diff if diff is not None else CollectionDiff()
    combined.collection_title = render_row_name(template, profile, picks)
    stored: str | None = None

    for section in targets:
        kind = section_kind(section)
        # Remap each pick to THIS library's ratingKey — a Plex collection can only hold its own
        # library's items. A pick this library doesn't have is skipped (delivered wherever it does
        # live). With no per-section index (legacy caller), fall back to the pick's existing key.
        keys = idx.get(section.key)
        # When the caller curated PER LIBRARY (section_picks), deliver this library its own list;
        # otherwise fall back to splitting the one pick list by media type (legacy/shared callers).
        source_picks = section_picks.get(section.key, []) if section_picks is not None else by_type.get(kind, [])
        this_section = [
            (replace(p, rating_key=keys[p.tmdb_id]) if keys is not None else p)
            for p in source_picks
            if keys is None or p.tmdb_id in keys
        ]
        if not this_section:
            continue
        # Per-library timing: this is the one place we can see that (e.g.) a TV row costs 6x a Movies
        # row, which points straight at removeItems (one DELETE per item) on a full-turnover row. The
        # PMS timing adapter breaks each of those calls down further (perf diag 2026-07-19).
        _one_start = time.monotonic()
        one, stored = _deliver_one(
            plex,
            section,
            profile,
            this_section,
            template,
            wanted_label,
            marker,
            sole_row,
            dry_run=dry_run,
            label_prefix=config.label_prefix,
            poster=spec.poster if spec else None,
            artist=poster_artist,
            order_work=order_work,
        )
        logger.debug(
            "{}: delivered library '{}' (+{} -{} ={}) in {:.1f}s",
            profile.username,
            section.title,
            len(one.added),
            len(one.removed),
            len(one.kept),
            time.monotonic() - _one_start,
        )
        combined.added += one.added
        combined.removed += one.removed
        combined.kept += one.kept
        combined.deleted += one.deleted
        combined.created = combined.created or one.created
        # Per-(row, library) breakdown for the UI: what changed in THIS library and its own picks,
        # so a run shows "added X to Movies, Y to TV" rather than one merged list.
        if breakdown is not None:
            breakdown.append(
                {
                    "row_slug": spec.slug,
                    "row_title": one.collection_title,
                    "library_key": str(section.key),
                    "library_title": getattr(section, "title", str(section.key)),
                    "added": list(one.added),
                    "removed": list(one.removed),
                    "kept": list(one.kept),
                    "deleted": list(one.deleted),
                    "created": one.created,
                    "picks": [
                        {
                            "rank": p.rank,
                            "title": p.title,
                            "reason": p.reason,
                            "seed_title": p.seed_title,
                            "tmdb_id": p.tmdb_id,
                            "media_type": p.media_type.value,
                        }
                        for p in this_section
                    ],
                }
            )
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
    sections: list | None = None,
) -> None:
    """Delete a user's collection for a row they've muted, in every targeted library.

    Muting means "you don't get this row" — but a row delivered BEFORE the mute still exists on the
    server, so it must be removed, not merely skipped on the next run. Deleting only makes the server
    strictly more private (the row's `shortlist_<slug>` label keeps it excluded on every other share
    until it's gone), so this is always safe. A row whose title depends on its picks (a `{top_seed}`
    template) can't be reconstructed without them, so it's left for a later sweep; static-titled rows
    — the default row and most custom rows — match exactly and are removed here.
    """
    wanted_label = spec.label or f"{config.label_prefix}_{profile.slug}"
    marker = row_marker(0) if spec.shared else row_marker(profile.plex_account_id)
    template = resolve_row_template(spec, profile, config)
    # Look in every library, not just the row's current targets: if its library_keys changed, an
    # earlier copy may linger in a library it no longer targets, and a muted row must leave them all.
    scan = sections if sections is not None else list(plex.sections_by_type().values())
    for section in scan:
        # Render the title with THIS library's name so a {library_name} row matches its own per-library
        # collection (delivery wrote "✨ Movies Picked for You" in Movies, "✨ TV Shows …" in TV).
        display = render_row_name(template, profile, [], library_name=getattr(section, "title", "") or "")
        title = display + marker
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


def remove_row_collections(
    plex: PlexClient,
    config: EngineConfig,
    *,
    label: str,
    displays: set[str] | None,
    dry_run: bool,
) -> list[str]:
    """Delete Shortlist collections carrying ``label`` — an on-demand reconcile OUTSIDE a run (a
    config change, or a manual "remove from Plex").

    ``displays`` pins WHICH collections go: with a set, only those whose human title (marker stripped)
    is in it — a specific per-person row, since all of a user's rows share their label and differ only
    by title. With ``None``, every collection under the label — a shared row's own label, or a user's
    whole label when the user is removed.

    Removal only — it never creates or promotes, so it can never leak: deleting a row can only make
    the server more private. Scans EVERY library,
    so a copy left in a library the row no longer targets is still removed. ``delete_owned_collection``
    refuses anything without a ``shortlist_`` label, so a foreign (Kometa) collection is never touched.
    Returns the display titles removed (or, in a dry run, that would be).
    """
    removed: list[str] = []
    for section in plex.sections():
        for collection in plex.find_owned_collections(section, label):
            display = strip_marker(collection.title)
            if displays is not None and display not in displays:
                continue
            removed.append(display)
            if dry_run:
                logger.info("[dry-run] would remove '{}' in '{}' (label {})", display, section.title, label)
            else:
                plex.delete_owned_collection(collection, config.label_prefix)
                logger.info("removed '{}' in '{}' (label {})", display, section.title, label)
    return removed


def rename_row_collections(
    plex: PlexClient,
    config: EngineConfig,
    *,
    label: str,
    marker: str,
    old_display: str,
    new_display: str,
    dry_run: bool,
) -> list[str]:
    """Rename this account's row collection IN PLACE — ``old_display`` → ``new_display``, keeping the
    invisible account marker — across every library that holds it. An on-demand reconcile OUTSIDE a
    run (the owner renamed a row): a multi-row user's renamed row is updated rather than orphaned with
    a new copy (single-row users are already renamed seamlessly by the next run's delivery).

    Privacy-neutral: the filter that hides a row is keyed on its LABEL, which is untouched here, so
    changing only the human title can never make the server less private (it neither creates,
    promotes, nor alters a share filter).
    Matches only collections under ``label`` whose marker-stripped title equals ``old_display``; a
    foreign (Kometa) collection never carries our label and ``find_owned_collections`` only returns
    ours. Returns the library titles renamed (or, in a dry run, that would be).
    """
    if not label.startswith(config.label_prefix):
        # Belt-and-suspenders (rule 4): only ever retitle under one of OUR labels, matching the delete
        # path's ownership re-check. find_owned_collections already scopes to this label, so this only
        # guards against a caller ever passing a foreign one.
        logger.warning("refusing to rename under a non-Shortlist label {!r}", label)
        return []
    renamed: list[str] = []
    new_title = new_display + marker
    for section in plex.sections():
        for collection in plex.find_owned_collections(section, label):
            if strip_marker(collection.title) != old_display or collection.title == new_title:
                continue  # not this row, or already carries the new title
            renamed.append(section.title)
            if dry_run:
                logger.info("[dry-run] would rename '{}' → '{}' in '{}'", old_display, new_display, section.title)
            else:
                collection.editTitle(new_title)
                logger.info("renamed '{}' → '{}' in '{}'", old_display, new_display, section.title)
    return renamed


def reset_row_posters(
    plex: PlexClient,
    config: EngineConfig,
    *,
    label: str,
    displays: set[str] | None,
    dry_run: bool,
) -> list[str]:
    """Revert a row's collection(s) to Plex's own artwork — used when a row switches back to 'Plex
    default' after having had a custom poster. Cosmetic and privacy-neutral (the hiding label and
    promotion are untouched). Matches only OUR-labelled collections; ``displays`` limits to those
    marker-stripped titles (per-person rows), or ``None`` resets every collection under ``label``
    (a shared row's single membership). Returns the library titles reset (or that would be)."""
    if not label.startswith(config.label_prefix):
        logger.warning("refusing to reset posters under a non-Shortlist label {!r}", label)
        return []
    reset: list[str] = []
    for section in plex.sections():
        for collection in plex.find_owned_collections(section, label):
            display = strip_marker(collection.title)
            if displays is not None and display not in displays:
                continue
            reset.append(section.title)
            if dry_run:
                logger.info("[dry-run] would reset poster on '{}' in '{}'", display, section.title)
            else:
                plex.reset_poster(collection)
                logger.info("reset poster on '{}' in '{}'", display, section.title)
    return reset


def _create_labelled_collection(
    plex: PlexClient,
    section,
    profile: UserProfile,
    picks: list[Pick],
    *,
    title: str,
    label: str,
    display: str,
    poster: PosterSpec | None = None,
    artist: PosterArtist | None = None,
    order_work: list[tuple] | None = None,
) -> str:
    """Create the collection, apply its label, and delete it if the label doesn't stick.

    A collection with no shortlist_* label is invisible to every lookup we have — all of them key off
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
                "{}: ORPHANED COLLECTION — '{}' (ratingKey {}) in '{}' exists with NO shortlist "
                "label. Shortlist cannot find or remove it and no share filter can hide it. "
                "Delete it in Plex (find it by ratingKey — the title carries invisible "
                "characters and will not match a search).",
                profile.username,
                display,
                getattr(collection, "ratingKey", "?"),
                section.title,
            )
        raise
    if order_work is not None:
        order_work.append((collection, [p.rating_key for p in picks]))
    apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=False)
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
    label_prefix: str = "shortlist",
    poster: PosterSpec | None = None,
    artist: PosterArtist | None = None,
    order_work: list[tuple] | None = None,
) -> tuple[CollectionDiff, str]:
    """Upsert one library's collection to exactly `picks`, in order. Returns (diff, stored_label).

    A user can have several rows, all carrying their label and told apart by title, so the right one
    is the labelled collection whose title matches. When this is the user's ONLY row (`sole_row`) and
    exactly one labelled collection exists, a title mismatch is treated as an in-place rename — so a
    changed name template updates the row rather than orphaning it. With more than one row that guess
    is unsafe (which row was renamed?), so a mismatch builds a fresh row and the stale one, still
    labelled, stays hidden. Foreign (e.g. Kometa) collections never carry our label, so are untouched.
    """
    # This library's own name fills {library_name}; every match/promote/retire caller renders with the
    # same section title, so the titles stay in lockstep (a mismatch would leave a row unhidden).
    display = render_row_name(template, profile, picks, library_name=getattr(section, "title", "") or "")
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
            apply_poster(plex, None, poster, profile, picks, library_name=section.title, artist=artist, dry_run=True)
            return diff, label
        stored = _create_labelled_collection(
            plex,
            section,
            profile,
            picks,
            title=title,
            label=label,
            display=display,
            poster=poster,
            artist=artist,
            order_work=order_work,
        )
        return diff, stored

    existing_items = collection.items()  # ONE read of current membership, reused for the diff AND set_items
    current_titles = [i.title for i in existing_items]
    diff = CollectionDiff(
        added=[t for t in wanted_titles if t not in current_titles],
        removed=[t for t in current_titles if t not in wanted_titles],
        kept=[t for t in wanted_titles if t in current_titles],
        collection_title=display,  # the human title: the marker is Plex's business, not the owner's
    )
    wanted_keys = [p.rating_key for p in picks]
    current_keys = {i.ratingKey for i in existing_items}
    to_add_keys = [k for k in wanted_keys if k not in current_keys]
    wanted_set = set(wanted_keys)
    to_remove_count = sum(1 for i in existing_items if i.ratingKey not in wanted_set)

    if dry_run:
        # Say what a real run WOULD do: a big turnover rebuilds (delete + recreate), not an in-place
        # update — a dry-run reviewer should see the row would be rebuilt (rule 8).
        verb = "would rebuild" if to_remove_count >= _REBUILD_MIN_REMOVES else "would update"
        logger.info(
            "[dry-run] {}: {} '{}' in '{}' (+{} -{} ={})",
            profile.username,
            verb,
            display,
            section.title,
            len(diff.added),
            len(diff.removed),
            len(diff.kept),
        )
        apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=True)
        return diff, label

    # Large turnover: per-item removeItems DELETEs are the dominant delivery cost on a slow library
    # (plexapi has no bulk remove, and SFLIX TV rows cost ~15s PER delete). Rebuilding replaces N
    # deletes with ONE batched create. Delete the old collection FIRST, then create+label a fresh one:
    # delete-first avoids a duplicate-title 409 (two collections can't share the marked title) and is
    # leak-safe — nothing exists between the two steps (nothing to leak), and the brief create->label
    # window is the same one the normal first-create path already has. (perf: SFLIX 2026-07-19)
    if to_remove_count >= _REBUILD_MIN_REMOVES:
        logger.info(
            "{}: rebuilding '{}' in '{}' (+{} -{}) — avoids {} per-item removes",
            profile.username,
            display,
            section.title,
            len(to_add_keys),
            to_remove_count,
            to_remove_count,
        )
        plex.delete_owned_collection(collection, label_prefix)
        stored = _create_labelled_collection(
            plex,
            section,
            profile,
            picks,
            title=title,
            label=label,
            display=display,
            poster=poster,
            artist=artist,
            order_work=order_work,
        )
        return diff, stored

    if collection.title != title:
        collection.editTitle(title)

    if not to_add_keys and to_remove_count == 0:
        # Membership already IS the wanted set — skip the add/remove/sortUpdate writes entirely. An
        # unchanged row used to fire a sortUpdate every run (a real write on a slow library, for
        # nothing). The deferred order pass still runs via order_work, so a freshness re-rank is still
        # applied and the collection keeps its custom sort from prior runs. (perf: SFLIX 2026-07-19)
        if order_work is not None:
            order_work.append((collection, wanted_keys))
        apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=False)
        stored = plex.stored_label(collection, label)
        logger.info(
            "{}: '{}' in '{}' unchanged ({} items) — no membership write",
            profile.username,
            display,
            section.title,
            len(picks),
        )
        return diff, stored

    # Fetch ONLY the items being added (the delta), not all N picks — most are already in the
    # collection on a steady run, so this is a handful of items instead of the whole row. Skip the
    # fetch entirely when nothing is new (fetch_items([]) raises NotFound on a real PMS).
    add_items = plex.fetch_items(to_add_keys) if to_add_keys else []
    plex.set_items(collection, existing_items, add_items, wanted_keys)
    if order_work is not None:
        order_work.append((collection, wanted_keys))
    apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=False)

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
    """Delete every Shortlist row on the SERVER that is broken beyond repair-in-place.

    Two kinds, and both are only fixable by rebuilding:

    * **Unhidable** — its type doesn't match its library, so neither `filterMovies` nor
      `filterTelevision` can match it and EVERY account can see it.
    * **Shared-tag** — its title lacks its owner's marker, so it shares a collection tag with the
      other rows in that library and holds their picks as well as its owner's. Its owner opens
      "Picked for You" and reads other people's recommendations.

    `markers` maps slug -> the invisible marker that row's title must end with. A slug that isn't
    in it belongs to an account Shortlist can't identify — it could not rebuild that row, so it leaves
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
    slug_by_marker = {marker: slug for slug, marker in markers.items()}  # attribute an unlabelled orphan
    deleted = {} if deleted is None else deleted
    for section in plex.sections():
        for collection in section.collections():
            label = next((t.tag for t in collection.labels if t.tag.lower().startswith(prefix)), None)
            if label is None:
                # No shortlist label. If the title still carries our invisible marker, it's an ORPHAN —
                # a per-user row whose label write never landed (an interrupted run). With no label, NO
                # `label!=` share filter can hide it, so EVERY user sees it: the exact leak that stranded
                # unlabelled "Picked for You" rows on SFLIX. The marker proves it's ours, so delete it;
                # the next successful run rebuilds the owner's row, labelled. A collection with no marker
                # is genuinely foreign (Kometa and friends) — leave it alone (rule 4).
                if not has_marker(collection.title):
                    continue
                orphan_slug = slug_by_marker.get(collection.title[-64:]) or f"orphan:{marker_account(collection.title)}"
                logger.warning(
                    "{}{}: removing an UNLABELLED orphan row in '{}' — no label, so no share filter can "
                    "hide it (visible to everyone)",
                    "[dry-run] " if dry_run else "",
                    orphan_slug,
                    section.title,
                )
                title = collection.title
                if not dry_run:
                    plex.delete_owned_collection(collection, config.label_prefix)
                deleted.setdefault(orphan_slug, []).append(title)
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
