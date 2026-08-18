"""Collection delivery: upsert, order, label, promote — touching only what Shortlist owns."""

from __future__ import annotations

import time
from dataclasses import replace

from loguru import logger

from shortlist.engine.clients.plex_pms import PlexClient, log_title
from shortlist.engine.clients.poster import PosterArtist
from shortlist.engine.models import (
    LABEL_PREFIX,
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


def _rename_or_keep(collection, title: str, profile: UserProfile, section_title: str) -> None:
    """Rename a row in place, keeping its old name if Plex refuses the new one.

    A Plex collection is keyed by TITLE within a library, so a rename onto a title that already
    exists there answers 409 Conflict. The rebuild path a few lines below already knows this and
    deletes first to avoid it; this path did not, and an unguarded `editTitle` took the whole PERSON
    down with it — recorded on a real server (run 4, 2026-08-15):

        BadRequest: (409) conflict; …title.value=🎯 Because you watched Ted Lasso…&type=18

    That user got no rows at all that night, over a name. A `{top_seed}` row renames itself whenever
    the seed it is named after changes, so it is the one row whose title moves onto ground another
    row of the same person's may already be standing on.

    Keeping the old title is the safe failure: it still carries this account's marker, so nothing
    becomes visible to anyone new, and the row's MEMBERSHIP — the part that matters — is written by
    the caller either way. A stale name for one night beats an empty row.
    """
    try:
        collection.editTitle(title)
    except Exception as exc:  # plexapi raises BadRequest; the status is only in the message
        # `startswith`, NOT `"409" in`. plexapi formats the message as
        # `f'({status}) {codename}; {url} {errtext}'` (`plexapi/server.py:752`), and for `editTitle`
        # that url carries `id=<ratingKey>` — so a substring test matches the COLLECTION'S OWN KEY.
        # Measured: a 500 on ratingKey 40953, a 401 on ratingKey 1409 and a 503 on ratingKey 24091
        # were all swallowed, each logging "409 — a collection there already has that title", which
        # is a lie about a failure that then went unreported. The status is always the leading
        # token, so anchoring it is exact.
        if not str(exc).startswith("(409)"):
            raise
        logger.warning(
            "{}: Plex refused to rename '{}' to '{}' in '{}' (409 — a collection there already has "
            "that title). Keeping the old name; the row's titles are still updated.",
            profile.username,
            log_title(collection.title),
            log_title(title),
            section_title,
        )


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


def top_seed_of(picks: list[Pick]) -> str:
    """The title `{top_seed}` renders to: the best-matching pick that actually HAS a seed.

    "Best-matching" is the lowest `rank`, not `picks[0]`. Those were the same thing until a row could
    choose its own display order: `picks` arrives in the order it is written to Plex, so reading
    position 0 renamed a `{top_seed}` row after whichever pick happened to sort first — a shuffled row
    would have picked a new name most nights, rewriting the title on Plex each time. `rank` is stamped
    before ordering and still means "how good a match".

    Skipping the UNSEEDED picks is issue #84. It used to take the single best pick and use its seed
    "if it has one" — so a row whose top pick came from a source that seeds nothing (trending, popular
    on this server, a web-search suggestion) rendered no seed AT ALL and fell back to the default
    title, with a dozen perfectly good seeded picks sitting right behind it. The reporter saw it on
    every account on their server, including ones with years of history, which is what "no seed" was
    never meant to mean: it is supposed to mean a cold start.
    """
    seeded = [p for p in picks if p.seed_title]
    return min(seeded, key=lambda p: p.rank).seed_title if seeded else ""


def seed_source(section_picks: list[Pick], row_picks: list[Pick]) -> list[Pick]:
    """Which picks name a row IN ONE LIBRARY: its own when they carry a seed, else the whole row's.

    ONE function because two modules need the identical answer. `delivery._deliver_one` renders the
    title Plex is given; `rows._run_user` re-renders it to stamp `placement_titles`, which is how the
    promote phase finds the collection it just wrote. Disagree by one character and promote looks up a
    title delivery never created — the row keeps whatever placement it had, or drops to the no-spec
    default. Two copies of a four-value rule (both seeded / this library seedless / row seedless /
    no row picks at all) is how that drift happens, so there is only one copy.

    A library uses its OWN seed first: a `{top_seed}` row spanning two libraries genuinely follows a
    different watch in each and its titles should say so (pinned by
    test_pipeline.py::TestPlacement::test_a_top_seed_row_records_a_placement_title_per_library).
    Borrowing is the step BEFORE giving up and using the default name — issue #84, where a
    `movies & shows` row whose seeds were all films delivered the seeded title to Movies and
    "✨ Picked for You" to TV, so one row appeared twice under two names on the same person's Plex.
    """
    return section_picks if top_seed_of(section_picks) else row_picks


def _fill(template: str, profile: UserProfile, top_seed: str, library_name: str) -> str:
    """Substitute the placeholders and tidy the spacing. No fallbacks, no opinions."""
    rendered = (
        template.replace("{top_seed}", top_seed)
        .replace("{user}", profile.display_name)
        .replace("{library_name}", library_name)
    )
    # A {library_name} title with no (or a padding-adjacent) library leaves double spaces where the
    # placeholder was — collapse runs of whitespace so the human title reads clean either way.
    return " ".join(rendered.split()) if "{library_name}" in template else rendered.strip()


def render_row_name(
    template: str,
    profile: UserProfile,
    picks: list[Pick],
    library_name: str = "",
    fallback_name: str = "",
) -> str:
    """Render the row title as a HUMAN reads it — no marker. **"" means this row has no name.**

    ``library_name`` fills the ``{library_name}`` placeholder with the delivering library's own name,
    so the same row gets a distinct title per library (a privacy requirement: per-person rows share one
    label and are told apart only by title). Every caller that renders a title to MATCH a collection on
    the PMS — deliver, promote, mute/retire, rename — must pass the SAME library name delivery used, or
    it would look for a title delivery never wrote and silently no-op (a row could stay unhidden).

    The seed comes from the best-matching pick that HAS one — see `top_seed_of`.

    **Returning "" is the point of this function** (issue #84). A `{top_seed}` template needs a pick
    that traces back to something the person watched, and someone below the history threshold has
    none — their picks come from the cold-start fill and carry no seed at all. This used to answer
    with the hardcoded DEFAULT_ROW_NAME, which on a 22-user server with a French row-name template
    put "✨ Picked for You" on 19 people's Plex: not the operator's words, and a claim about a watch
    that never happened. `fallback_name` is the operator's own answer to that — and when they have
    not given one, the honest result is no name, which the caller must read as "do not build this row
    for this person". Nothing here invents a title. `render_poster_text` has always worked this way;
    row titles simply never did.
    """
    top_seed = top_seed_of(picks)
    unfillable = "{top_seed}" in template and not top_seed
    rendered = "" if unfillable else _fill(template, profile, top_seed, library_name)
    if rendered:
        return rendered
    # The row's own name could not be produced — a `{top_seed}` with nothing to name, or a template
    # that is blank once rendered. Fall back only to what the OPERATOR wrote, and only if that itself
    # can be rendered: a fallback that also needs a seed is no fallback at all.
    if fallback_name and "{top_seed}" not in fallback_name:
        return _fill(fallback_name, profile, "", library_name)
    return ""


def render_poster_text(field_value: str, profile: UserProfile, picks: list[Pick], library_name: str) -> str:
    """Fill a poster text field's placeholders (``{user}``/``{library_name}``/``{top_seed}``) for the
    user and library it lands on, using the same helper delivery uses for titles.

    ``render_row_name`` returns "" when a field cannot be filled — a blank one, or a ``{top_seed}``
    with no seed — so the text is dropped rather than becoming "✨ Picked for You". This was the one
    place that already refused a substitute; row titles now behave the same way (issue #84), so the
    special-casing that used to be needed here is gone.
    """
    field_value = field_value.strip()
    if not field_value:
        return ""
    return render_row_name(field_value, profile, picks, library_name=library_name)


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
    delivered_keys: dict[str, int] | None = None,
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

    `delivered_keys` is {section key -> ratingKey} for THIS row and user, from the delivery ledger. It
    is how a title that no longer renders is recognised as this row's rather than orphaned and rebuilt
    — see `_deliver_one`. Empty is always safe: delivery falls back to matching by title.

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
    wanted_label = spec.label or f"{LABEL_PREFIX}_{profile.slug}"
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
    combined.collection_title = render_row_name(template, profile, picks, fallback_name=spec.fallback_name)
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
            # The row's whole pick list for the TITLE; `this_section` is the content. See _deliver_one.
            title_picks=picks,
            # Must be the SAME value every other renderer of this row's title uses — remove, the
            # placement stamp, promote, rename. A site that forgets it renders a different title, and
            # then looks for a collection delivery never wrote (issue #84).
            fallback_name=spec.fallback_name if spec else "",
            # This library's entry only: a row has one collection per library, and a key from a
            # DIFFERENT library must never be allowed to match here.
            delivered_key=(delivered_keys or {}).get(str(section.key)),
            dry_run=dry_run,
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
                    # The ledger's handle on this collection. Everything else in this entry describes
                    # what CHANGED; this says WHICH Plex object it changed, which is the one thing a
                    # later reconcile cannot recompute — a `{top_seed}` title is different every run.
                    "rating_key": one.rating_key,
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
                            # The run page renders THIS blob, not the picks table — so provenance
                            # has to be here too, or "why was this picked?" is unanswerable on the
                            # one screen built to answer it.
                            "sources": list(p.sources),
                            "affinity": p.affinity,
                            # Release year and TMDB score, for the same reason as provenance above:
                            # the run page renders this blob, and "is this an old title, and is it
                            # any good?" is the first thing asked of a row that looks wrong.
                            "year": p.year,
                            "rating": p.rating,
                        }
                        for p in this_section
                    ],
                }
            )
        # Recorded the instant the PMS confirms the label — if the NEXT library blows up, this
        # row still gets excluded on every other user's share this run.
        #
        # `and stored`, because a row that wrote NOTHING must not touch this. `stored_key` is the
        # person's slug, shared by every one of their rows, so a row that could not be named would
        # otherwise overwrite the real label a NAMEABLE row just recorded with "" — and
        # `desired_excludes` would merge that empty string into every other account's share filter as
        # `label!=Shortlist_bob,,Shortlist_mike`: malformed, and no exclude at all for that person
        # while it stands. This function's own docstring already promised it ("The stored label is
        # None when nothing was delivered"); the code did not keep the promise.
        if stored_labels is not None and not dry_run and stored:
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
    delivered_keys: dict[str, int] | None = None,
) -> list[str]:
    """Delete a user's collection for a row they've muted or that a cold start skips, in every library.

    Muting means "you don't get this row" — but a row delivered BEFORE the mute still exists on the
    server, so it must be removed, not merely skipped on the next run. The same is true of a row whose
    `cold_start` is "skip" for someone whose history has thinned out. Deleting only makes the server
    strictly more private (the row's `shortlist_<slug>` label keeps it excluded on every other share
    until it's gone), so this is always safe.

    Static-titled rows — the default row and most custom rows — match on their rendered title. A row
    whose title depends on its picks (a `{top_seed}` template) renders to NOTHING with no picks, and
    per-person rows share one label and are told apart by title ONLY — so there is no title to match
    on at all. It used to render the bare `DEFAULT_ROW_NAME`, and matching on THAT would find and
    delete whatever else was titled that: the user's live default row, in every library, every run.
    `_retired_rows` guards the identical collision for
    DISABLED rows (`context_builder._retired_rows`).

    ``delivered_keys`` ({section key -> ratingKey}, from the delivery ledger) is how such a row is
    removed anyway: Plex IDENTITY, not a computed title, which is the only handle on a row whose title
    was different every run. Same mechanism `remove_row_collections` uses, and still scoped to
    ``wanted_label``, so identity narrows the search and never widens ownership. Without it an
    unrenderable row is left for a later sweep — which has to mean left ALONE.

    It is used ONLY for an unrenderable title, never as a second matcher for a row that titles fine.
    ratingKeys are rowids that Plex reuses, and no delete path here prunes the ledger, so a stale key
    can name a live object; scoped to this label that object would be one of this user's OTHER rows.
    Restricting the key to the case that has no other handle bounds that to rows whose title genuinely
    cannot be computed, where doing nothing is the only alternative. `context_builder._delivered_keys`
    additionally drops any ratingKey two rows both claim, so an ambiguous key selects nothing at all.

    Returns the section keys a collection was actually deleted in, so the caller can have those ledger
    entries forgotten — a key whose collection is gone must not be re-presented on the next run.
    Empty in a dry run: nothing was deleted, so nothing may be forgotten.
    """
    removed_in: list[str] = []
    wanted_label = spec.label or f"{LABEL_PREFIX}_{profile.slug}"
    marker = row_marker(0) if spec.shared else row_marker(profile.plex_account_id)
    template = resolve_row_template(spec, profile, config)

    # Look in every library, not just the row's current targets: if its library_keys changed, an
    # earlier copy may linger in a library it no longer targets, and a muted row must leave them all.
    scan = sections if sections is not None else list(plex.sections_by_type().values())
    for section in scan:
        # Render the title with THIS library's name so a {library_name} row matches its own per-library
        # collection (delivery wrote "✨ Movies Picked for You" in Movies, "✨ TV Shows …" in TV).
        display = render_row_name(
            template, profile, [], library_name=getattr(section, "title", "") or "", fallback_name=spec.fallback_name
        )
        # `or None`: a ledger key of 0 means "the PMS never gave us one" (a dry run records 0), and
        # `_rating_key` also returns 0 for a collection carrying no key — so a 0 would match every
        # keyless collection under this label. Only a real key may ever select an object for deletion.
        ledger_key = (delivered_keys or {}).get(str(section.key)) or None
        # "" IS the unrenderable signal now — `render_row_name` no longer answers with a substitute
        # name, so this no longer has to infer "unnameable" from a title that merely LOOKS like the
        # default. That inference was always slightly wrong: a row deliberately titled exactly
        # "✨ Picked for You" was treated as unnameable and left for a sweep.
        # A `{top_seed}` template is unrenderable HERE whatever its fallback says. This function
        # renders with no picks, so a row with a fallback renders the FALLBACK title — but a seeded
        # user's collection wears "Because you watched X", so matching on it finds nothing (a muted
        # or cold-skipped row silently stops being removed) or finds a SIBLING row that happens to
        # be titled the fallback and deletes that instead. The ledger key is the only handle that
        # survives a title which differs per person, which is exactly what a fallback creates.
        unrenderable = not display or "{top_seed}" in template
        if unrenderable and ledger_key is None:
            # This row has no title to match on — a `{top_seed}` template (which renders per person,
            # so no title computed here is anyone's) or one that renders blank. Per-person rows share
            # one label and are told apart by title ONLY, so matching on a title we invented would
            # find and DELETE whatever else wears it, in this library, every run.
            #
            # Tested per LIBRARY, not once up front: a `{library_name}` template renders to the bare
            # default only when there is no library name, and here there always is — so a legitimate
            # "✨ Movies Picked for You" removal still happens.
            logger.debug(
                "{}: row '{}' has no renderable title in '{}' and no ledger key — left for a sweep "
                "rather than matched, which would delete a different row",
                profile.username,
                spec.slug,
                section.title,
            )
            continue
        title = display + marker
        for collection in plex.find_owned_collections(section, wanted_label):
            # EITHER/OR, never both. The ledger is the fallback for a title that cannot be computed —
            # it is not a second chance at a row whose title renders fine. Letting a key match those
            # too would mean a STALE key (ratingKeys are rowids and Plex reuses them) could select a
            # sibling collection under this same label — the user's live default row — and delete it,
            # logged as an ordinary removal. `removed_in` below is what keeps a key from GOING stale.
            if unrenderable:
                if _rating_key(collection) != ledger_key:
                    continue
            elif collection.title != title:
                continue
            # The collection's OWN title, not the computed one — for a `{top_seed}` row matched by
            # identity the computed name is the bare default, which would misreport what was deleted.
            removed_title = strip_marker(collection.title)
            if dry_run:
                logger.info(
                    "[dry-run] {}: would remove row '{}' in '{}'", profile.username, removed_title, section.title
                )
            else:
                plex.delete_owned_collection(collection, LABEL_PREFIX)
                logger.info("{}: removed row '{}' in '{}'", profile.username, removed_title, section.title)
                # Only on a REAL delete: a dry run leaves the collection there, so its ledger entry
                # is still the truth and forgetting it would blind the next real reconcile.
                removed_in.append(str(section.key))
            diff.deleted.append(removed_title)
    return removed_in


def remove_row_collections(
    plex: PlexClient,
    config: EngineConfig,
    *,
    label: str,
    displays: set[str] | None,
    dry_run: bool,
    in_sections: set[str] | None = None,
    rating_keys: set[int] | None = None,
) -> list[str]:
    """Delete Shortlist collections carrying ``label`` — an on-demand reconcile OUTSIDE a run (a
    config change, or a manual "remove from Plex").

    ``displays`` pins WHICH collections go: with a set, only those whose human title (marker stripped)
    is in it — a specific per-person row, since all of a user's rows share their label and differ only
    by title. With ``None``, every collection under the label — a shared row's own label, or a user's
    whole label when the user is removed.

    ``rating_keys`` matches by Plex IDENTITY instead, from the delivery ledger, and is unioned with
    ``displays`` rather than replacing it. It is the only thing that can find a ``{top_seed}`` row,
    whose title is different every run and so matches no computed ``displays`` entry. Both are still
    scoped to ``label``, so neither can reach another user's row or a foreign (Kometa) collection —
    identity narrows the search, it never widens ownership.

    ``in_sections`` (section keys) limits WHERE: used when a row is narrowed rather than removed —
    its ``media`` changed from both to movie, or a library was dropped from ``library_keys`` — so only
    the collections in libraries it no longer targets go, and the ones it still uses stay. ``None``
    means every library, which is right for a removal.

    Removal only — it never creates or promotes, so it can never leak: deleting a row can only make
    the server more private. Scans EVERY library by default,
    so a copy left in a library the row no longer targets is still removed. ``delete_owned_collection``
    refuses anything without a ``shortlist_`` label, so a foreign (Kometa) collection is never touched.
    Returns the display titles removed (or, in a dry run, that would be).
    """
    if not label.lower().startswith(f"{LABEL_PREFIX}_"):
        # Lowercased because `find_owned_collections` matches case-insensitively and `User.label`
        # stores the Plex TITLE-CASED form ("Shortlist_sarah") — a caller passing that would
        # otherwise get a silent empty list back and leave the collections on Plex for ever.
        # This function DELETES, and `find_owned_collections` matches a tag exactly — so the bare
        # `shortlist` label every row now carries would select every Shortlist collection on the
        # server and remove the lot. No caller builds that label today; this is the guard that keeps
        # it that way, and it is the one place where getting it wrong is unrecoverable.
        logger.warning("refusing to remove collections under a non-row label {!r}", label)
        return []
    removed: list[str] = []
    for section in plex.sections():
        if in_sections is not None and str(section.key) not in in_sections:
            continue
        for collection in plex.find_owned_collections(section, label):
            display = strip_marker(collection.title)
            by_key = bool(rating_keys) and _rating_key(collection) in rating_keys
            if displays is not None and display not in displays and not by_key:
                continue
            removed.append(display)
            if dry_run:
                logger.info("[dry-run] would remove '{}' in '{}' (label {})", display, section.title, label)
            else:
                plex.delete_owned_collection(collection, LABEL_PREFIX)
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
    if not label.lower().startswith(f"{LABEL_PREFIX}_"):
        # Lowercased for the same reason as the removal guard: `User.label` stores Plex's TITLE-CASED
        # form ("Shortlist_sarah"), and a case-sensitive test returns [] for it — indistinguishable
        # from "nothing matched", so a rename would leave the row under its old name with nothing but
        # a log line. Defensive: no caller passes the title-cased form today, and this is what keeps
        # one from silently no-opping if it ever does.
        # The UNDERSCORE matters (rule 4): every row now also carries the bare `shortlist` label, and
        # `find_owned_collections` matches a tag exactly — so a caller passing the constant label
        # would select every Shortlist collection on the server rather than one row's.
        # Belt-and-suspenders: only ever retitle under one of OUR labels, matching the delete
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
    if not label.lower().startswith(f"{LABEL_PREFIX}_"):
        # Lowercased like the other two guards — Plex stores the label title-cased, and a
        # case-sensitive test would turn a legitimate reset into a silent no-op. Defensive: both
        # callers build the label lowercase today.
        # Underscore-scoped for the same reason as the rename guard: the bare constant label matches
        # every Shortlist collection on the server, not one row's.
        logger.warning("refusing to reset posters under a non-Shortlist row label {!r}", label)
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


def _rating_key(collection) -> int:
    """A collection's Plex ratingKey as an int, or 0 when the PMS didn't give one.

    Never raises: the ledger is a convenience for later reconciles, and failing a delivery over a
    missing key would trade a real row for a bookkeeping detail.
    """
    try:
        return int(getattr(collection, "ratingKey", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _apply_shortlist_label(plex: PlexClient, collection, username: str) -> None:
    """Add the constant ``shortlist`` label, alongside the row's own ``shortlist_<user>`` one.

    One label a co-managing tool can be pointed at. Agregarr and Kometa both take a list of labels to
    leave alone, and ours are per PERSON — a 46-account server has 46 of them, plus one per shared
    row, and the list goes stale the moment somebody joins or leaves. This one never changes.

    **`addLabel` is not additive on the wire.** plexapi builds the new tag list as
    ``getattr(self, "labels", []) + items`` (``mixins/edit.py:294``) and PUTs it as an ABSOLUTE set.
    The owner label therefore survives only because it is re-sent from the client's in-memory
    ``collection.labels``. If that list were empty — rule 4's read that succeeds carrying no
    ``<Label>`` — this call would PUT exactly ``shortlist`` and DELETE ``shortlist_<user>``. No
    ``label!=shortlist_<user>`` exclude would match the row any more, so it would be visible to every
    shared account until the next run's sweep removed it, and nothing verifies hiding after the fact.

    Hence the guard: the collection must already show its owner label in memory. That is a free
    check — the caller has just run `stored_label` on the same object, which guarantees it — and it
    turns a silent leak into a skipped cosmetic label if that ever stops being true.

    Never fatal. A row is found, hidden and managed entirely through ``shortlist_<user>``; without
    THIS one all that happens is a co-managing tool keeps reordering this one row. Swallowing is not
    about the create path's delete-on-failure (this runs outside that ``try``) — it is that a label
    which only affects shelf tidiness must never fail a delivery that already reached Plex.
    """
    owner_prefix = f"{LABEL_PREFIX}_".lower()
    known = [t.tag for t in getattr(collection, "labels", []) or []]
    if not any(t.lower().startswith(owner_prefix) for t in known):
        # Either a genuinely unlabelled collection (which we never create) or an empty label read.
        # Both mean the PUT below would drop whatever is really on the row.
        logger.warning(
            "{}: not adding the '{}' label to '{}' — its owner label is not in the labels Plex "
            "returned, and the write would replace the label set rather than add to it",
            username,
            LABEL_PREFIX,
            getattr(collection, "title", "?"),
        )
        return
    if any(t.lower() == LABEL_PREFIX.lower() for t in known):
        return  # already there — no write, and no log line every run for every row
    try:
        plex.stored_label(collection, LABEL_PREFIX)
        # Said out loud because the FIRST run after upgrading applies this to every existing
        # collection, one write each — and an unexplained slow run is its own bug report. How much
        # slower is NOT measured: the ~17s figure this codebase quotes elsewhere is a MEMBERSHIP
        # write on a large TV library, and a label is a metadata PUT, which may be far cheaper. The
        # log line is what turns "the first night was long" into an answerable question.
        logger.info("{}: added the '{}' label to '{}'", username, LABEL_PREFIX, getattr(collection, "title", "?"))
    except Exception as e:
        logger.warning(
            "{}: could not add the '{}' label to '{}' ({}) — the row is fine, but a co-managing "
            "tool may keep reordering it",
            username,
            LABEL_PREFIX,
            getattr(collection, "title", "?"),
            type(e).__name__,
        )


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
) -> tuple[str, int]:
    """Create the collection, apply its label, and delete it if the label doesn't stick.

    A collection with no shortlist_* label is invisible to every lookup we have — all of them key off
    that prefix — so nothing would ever find it again, no filter could hide it, and it would be
    visible to everyone forever. Create and label must therefore succeed together or not at all.
    Returns the stored (Plex title-cased) label and the new collection's ratingKey — the ledger's
    handle on it, and the only one that survives a title the next run renders differently.
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
    _apply_shortlist_label(plex, collection, profile.username)
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
    return stored, _rating_key(collection)


def _find_this_rows_collection(
    plex: PlexClient,
    section,
    owned: list,
    title: str,
    marker: str,
    delivered_key: int | None,
    sole_row: bool,
    who: str,
) -> object | None:
    """Which (if any) of this user's OWNED collections in `section` is this row.

    A user can have several rows, all carrying their label and told apart by title, so the right one is
    the labelled collection whose title matches. When it does NOT match — a changed name template, a
    renamed library, a nickname edit — the row has to be recognised some other way or it is orphaned
    and rebuilt, leaving a stale duplicate nothing sweeps.

    Two answers, in order, once the title match fails:

    1. ``delivered_key`` — the ratingKey the LEDGER says this row put in this library. An identity, so
       it is right for a multi-row user and cannot be confused by a title that no longer renders.
    2. ``sole_row`` and exactly one labelled collection — the legacy fallback, for rows delivered
       before the ledger existed (nothing backfills it; there is no source to backfill from), for
       direct engine/CLI runs, and whenever a ledger entry was dropped as ambiguous. Safe only because
       `sole_row` now counts every row that could HAVE a collection here. Counting was the sole answer
       once, and it was wrong three ways: it read the run's SCOPE rather than the user's rows, it
       ignored muted rows whose unrenderable collections cannot be removed, and it could not help a
       multi-row user at all. See jobs-and-runs-design.md §16-§17.

    The key is exactly as right as the ledger is — it is not magic. Both fallbacks bound the damage to
    ANOTHER ROW OF THE SAME PERSON (every candidate comes from `owned`, this user's own label), never
    another user's row and never a foreign collection.

    Finally, a match of the wrong Plex type (the sweep already caught it, or will) is treated as no
    match at all: the caller must rebuild, since Plex won't re-type a collection in place.

    Args:
        plex: The Plex client, used only for `matches_section`.
        section: The library section this row is being delivered to.
        owned: This user's currently OWNED (our-label) collections in `section`.
        title: The exact marked title this row would be given (display name + invisible marker).
        marker: This user's invisible per-account marker — bounds the ledger/sole-row fallbacks to
            collections already exclusively theirs.
        delivered_key: The ratingKey the ledger recorded for this row in this library, or None.
        sole_row: Whether this is the only row this user could have in this library.
        who: The username, for the log lines only. A run narrates 45 people through this function and
            every one of them can render the same row title, so the log is unreadable without it.

    Returns:
        The matching Collection, or None when this row has no existing collection here (the caller
        must create one).
    """
    collection = next((c for c in owned if c.title == title), None)
    if collection is None and delivered_key:
        # The ledger names the exact object this row built here. If it is still under our label, it IS
        # this row — whatever it is currently called, and however many rows the user has.
        #
        # `endswith(marker)` is the same clause the sole_row branch below insists on, and for the same
        # reason: a pre-marker collection shares its tag with other users, so retitling one would hand
        # this person exclusive ownership of an object holding several people's picks. The sweep
        # removes those before delivery, but that guarantee lives in another module — restate it here.
        collection = next((c for c in owned if _rating_key(c) == delivered_key and c.title.endswith(marker)), None)
        if collection is not None:
            logger.debug(
                "{}: matched '{}' in '{}' by ledger ratingKey {} — retitling in place",
                who,
                title,
                section.title,
                delivered_key,
            )
    if collection is None and sole_row and len(owned) == 1 and owned[0].title.endswith(marker):
        # The sole row was renamed by a template change but still carries this account's marker, so
        # its membership is its own: update it in place rather than leave a stale duplicate. Only
        # when there's exactly one (otherwise we can't tell which row moved) and only a MARKED row —
        # a pre-marker row shares its tag with others and must be rebuilt, never renamed.
        collection = owned[0]

    if collection is not None and not plex.matches_section(collection, section):
        # The sweep already deleted this one (or, in a dry run, already reported that it would),
        # so treat it as gone — the caller builds a fresh, correctly-typed row in its place. Plex will
        # not re-type a collection: swapping its contents leaves the old subtype, and the row goes on
        # being visible to everyone. It must be rebuilt, never edited.
        logger.info("{}: rebuilding a row in '{}' — the old one was the wrong type", who, section.title)
        return None

    return collection


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
    title_picks: list[Pick] | None = None,
    fallback_name: str = "",
    delivered_key: int | None = None,
    dry_run: bool,
    label_prefix: str = LABEL_PREFIX,
    poster: PosterSpec | None = None,
    artist: PosterArtist | None = None,
    order_work: list[tuple] | None = None,
) -> tuple[CollectionDiff, str]:
    """Upsert one library's collection to exactly `picks`, in order. Returns (diff, stored_label).

    Finds this row's existing collection via `_find_this_rows_collection` (title match, then the
    ledger's ratingKey, then the sole-row fallback — see its docstring for why, in that order), then
    applies whichever of four write strategies fits: create, rebuild (large turnover), in-place
    update, or a no-op when membership already matches. `wanted_label` is this user's own label, so
    every candidate the identity match can land on is one of THEIR rows — never another user's row
    and never a foreign (e.g. Kometa) collection, which never carries our label at all.
    """
    # This library's own name fills {library_name}; every match/promote/retire caller renders with the
    # same section title, so the titles stay in lockstep (a mismatch would leave a row unhidden).
    #
    # This library's OWN picks name the row when they carry a seed — a `{top_seed}` row spanning two
    # libraries follows a different watch in each, and each title says which (pinned by
    # test_pipeline.py::TestPlacement::test_a_top_seed_row_records_a_placement_title_per_library).
    #
    # `title_picks` — the ROW's whole pick list — is the fallback BEFORE the default title, and that
    # is issue #84. Rendering only from `picks` meant a `movies & shows` row whose seeds were all
    # films got "Car vous avez regardé Conjuring" in Movies and the bare English default in TV, from
    # ONE row: two differently-titled collections for the same person, the second colliding with the
    # title every other seedless row already carries. `{top_seed}` names something the PERSON
    # watched, and what they watched is not confined to the library a pick happens to live in — so
    # borrowing the row's own seed is truer than giving up and calling it "Picked for You".
    seed_picks = seed_source(picks, title_picks if title_picks is not None else picks)
    display = render_row_name(
        template, profile, seed_picks, library_name=getattr(section, "title", "") or "", fallback_name=fallback_name
    )
    if not display:
        # This row has no name for this person and the operator has given none, so it does not get
        # built for them (issue #84). Returning an empty diff rather than raising: their OTHER rows
        # must still be delivered, and a person who cannot be named is a configuration answer, not a
        # failure.
        #
        # A copy an earlier version wrote is LEFT IN PLACE, deliberately. Deleting rows as a
        # side-effect of a naming change is what made the first attempt at this issue unshippable
        # (reverted in 33ba725). It keeps its label, so it stays hidden from everyone else; it simply
        # stops being updated. Muting the row, or setting it to skip a cold start, removes it through
        # the paths that exist for removing things.
        logger.info(
            "{}: row has no name for them in '{}' — not built (its name needs a watch and they have "
            "none; set a fallback name on the row to give them one)",
            profile.username,
            getattr(section, "title", "?"),
        )
        return CollectionDiff(), ""
    # What Plex is told to call it: the same thing, plus an invisible marker that makes it unique
    # in this library. Without it, every user's row is the same collection tag and holds everyone's
    # picks. Users see `display`; only the PMS ever sees the marker.
    title = display + marker
    label = wanted_label
    owned = plex.find_owned_collections(section, label)
    collection = _find_this_rows_collection(
        plex, section, owned, title, marker, delivered_key, sole_row, profile.username
    )

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
        stored, diff.rating_key = _create_labelled_collection(
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
    wanted_keys = [p.rating_key for p in picks]
    current_keys = {i.ratingKey for i in existing_items}
    wanted_set = set(wanted_keys)
    # Diff by ratingKey — the identity Plex actually writes on — never by title. A show's Plex title can
    # carry a year suffix ("Archer (2009)") the pick's title ("Archer") lacks, so a by-title diff would
    # report the SAME show as removed AND re-added every run even though membership never changed. The
    # write below already diffs by key (so nothing actually churned), but the run stats and per-row diff
    # were showing that phantom turnover. Titles are only for the human-readable report.
    title_by_key = {p.rating_key: p.title for p in picks}
    diff = CollectionDiff(
        added=[title_by_key.get(k, str(k)) for k in wanted_keys if k not in current_keys],
        removed=[i.title for i in existing_items if i.ratingKey not in wanted_set],
        kept=[title_by_key.get(k, str(k)) for k in wanted_keys if k in current_keys],
        collection_title=display,  # the human title: the marker is Plex's business, not the owner's
    )
    to_add_keys = [k for k in wanted_keys if k not in current_keys]
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
        stored, diff.rating_key = _create_labelled_collection(
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
        _rename_or_keep(collection, title, profile, section.title)

    if not to_add_keys and to_remove_count == 0:
        # Membership already IS the wanted set — skip the add/remove/sortUpdate writes entirely. An
        # unchanged row used to fire a sortUpdate every run (a real write on a slow library, for
        # nothing). The deferred order pass still runs via order_work, so a refresh-night re-rank is still
        # applied and the collection keeps its custom sort from prior runs. (perf: SFLIX 2026-07-19)
        if order_work is not None:
            order_work.append((collection, wanted_keys))
        apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=False)
        stored = plex.stored_label(collection, label)
        _apply_shortlist_label(plex, collection, profile.username)
        diff.rating_key = _rating_key(collection)
        logger.info(
            "{}: '{}' in '{}' unchanged ({} items) — no membership write",
            profile.username,
            display,
            section.title,
            len(picks),
        )
        return diff, stored

    # Fetch ONLY the items being added (the delta), not all N picks — most are already in the
    # collection on a steady run, so this is a handful of items instead of the whole row. An empty
    # delta short-circuits inside `fetch_items`, which also absorbs the case where every key in the
    # delta has been deleted from the library since the picks were made.
    add_items = plex.fetch_items(to_add_keys)
    plex.set_items(collection, existing_items, add_items, wanted_keys)
    if order_work is not None:
        order_work.append((collection, wanted_keys))
    apply_poster(plex, collection, poster, profile, picks, library_name=section.title, artist=artist, dry_run=False)

    stored = plex.stored_label(collection, label)
    _apply_shortlist_label(plex, collection, profile.username)
    diff.rating_key = _rating_key(collection)
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
    prefix = f"{LABEL_PREFIX}_".lower()
    markers = markers or {}
    slug_by_marker = {marker: slug for slug, marker in markers.items()}  # attribute an unlabelled orphan
    deleted = {} if deleted is None else deleted
    # Whether ANY of our labels came back at all this pass. The per-collection re-read below defeats a
    # transient miss, but not a SYSTEMIC one: a PMS mid library-index rebuild, or a version that stops
    # serving labels, answers both reads the same way, and then every marked row looks like an orphan
    # and this loop deletes the lot. So the aggregate is the second guard, exactly as the privacy sync
    # already reasons ("an EMPTY enumeration is not evidence of absence"): if the server has rows of
    # ours and NONE of them read as labelled, that is a read failure, not a server full of orphans.
    walked: list[tuple] = []
    labelled_seen = 0
    for section in plex.sections():
        for collection in section.collections():
            label = next((t.tag for t in collection.labels if t.tag.lower().startswith(prefix)), None)
            labelled_seen += label is not None
            walked.append((section, collection, label))
    orphan_candidates = sum(1 for _s, c, label in walked if label is None and has_marker(c.title))
    trust_labels = labelled_seen > 0 or orphan_candidates <= 1
    if not trust_labels:
        logger.error(
            "the PMS returned NO labels for any of {} collection(s) that are ours by title — treating "
            "that as a failed read, NOT as {} orphans. Nothing will be deleted this pass.",
            orphan_candidates,
            orphan_candidates,
        )

    for section, collection, label in walked:
        if label is None:
            # No shortlist label. If the title still carries our invisible marker, it's an ORPHAN —
            # a per-user row whose label write never landed (an interrupted run). With no label, NO
            # `label!=` share filter can hide it, so EVERY user sees it: the exact leak that stranded
            # unlabelled "Picked for You" rows on SFLIX. The marker proves it's ours, so delete it;
            # the next successful run rebuilds the owner's row, labelled. A collection with no marker
            # is genuinely foreign (Kometa and friends) — leave it alone (rule 4).
            if not has_marker(collection.title) or not trust_labels:
                continue
            # Ask the server again before destroying anything. A real PMS returns no labels in
            # the section listing, so the `collection.labels` above is only populated by a
            # silent plexapi re-read — and a read that SUCCEEDS carrying no <Label> is
            # indistinguishable from a genuinely unlabelled row. `confirm_unlabelled` is an
            # explicit second read whose failure means "leave it".
            if not plex.confirm_unlabelled(collection, LABEL_PREFIX):
                logger.warning(
                    "{}: looked unlabelled in the collection list but the server says it is labelled — NOT deleting it",
                    log_title(collection.title),
                )
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
                plex.delete_owned_collection(collection, LABEL_PREFIX)
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
            plex.delete_owned_collection(collection, LABEL_PREFIX)
        deleted.setdefault(slug, []).append(title)
    return deleted
