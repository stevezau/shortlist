"""Per-user pipeline orchestration: history → candidates → filter → rank → curate → deliver → privacy."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

from rowarr.engine import candidates as candidates_mod
from rowarr.engine import ranking
from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import Curator, CuratorError, NullCurator
from rowarr.engine.delivery import deliver_rows, row_marker, sweep_broken_rows
from rowarr.engine.history import HistorySource, derive_seeds
from rowarr.engine.models import (
    SHARED_SLUG_PREFIX,
    Candidate,
    CollectionDiff,
    EngineConfig,
    MediaType,
    Pick,
    RowSpec,
    RunReport,
    UserProfile,
    UserRunReport,
    UserType,
    WatchedItem,
)
from rowarr.engine.privacy import SnapshotStore, sync_user_restrictions


@dataclass
class EngineContext:
    """Everything one run needs; adapters (CLI/server) build this once."""

    config: EngineConfig
    plex: PlexClient
    plextv: PlexTvClient
    tmdb: TmdbClient
    history_source: HistorySource
    curator: Curator
    snapshots: SnapshotStore
    # slug -> {(tmdb_id, media_type)}: the staleness guard. Keyed on the PAIR because TMDB ids
    # are unique only within a namespace — movie 550 and TV 550 are different titles.
    recent_picks: dict[str, set[tuple[int, MediaType]]] = field(default_factory=dict)
    # plex account id -> the slug Rowarr assigned that account, for EVERY user it knows (not just
    # tonight's). This is how "whose row is this?" is answered. It cannot be answered from a name:
    # people rename themselves, and two display names can slugify to the same string — either
    # would silently hand one account another's row.
    known_slugs: dict[int, str] = field(default_factory=dict)
    progress: Callable[[str, str, dict], None] | None = None  # (user_slug, stage, counts) -> None


def _emit(ctx: EngineContext, slug: str, stage: str, counts: dict) -> None:
    if ctx.progress is not None:
        try:
            ctx.progress(slug, stage, counts)
        except Exception:  # a broken progress listener must never fail a run
            logger.exception("progress callback failed")


def run(ctx: EngineContext, users: list[UserProfile]) -> RunReport:
    """Run the pipeline for every enabled user. Users are independent — one failure never
    stops the run (per-user try/except; plex-safety rule 6 resume-safety).

    Write ordering is leak-safe: rows are created/updated UNPROMOTED, then every user's
    share filters are merged, and only then are rows promoted onto shared Home — so a new
    collection is never visible to anyone before the exclusions that hide it exist.
    """
    report = RunReport(started_at=datetime.now(UTC), dry_run=ctx.config.dry_run)

    sections = ctx.plex.sections()
    targets = ctx.plex.sections_by_type()
    target_keys = {section.key for section in targets.values()}

    # Two indexes, because they answer different questions.
    #
    # `seed_index` covers EVERY library: it turns what a user WATCHED into a TMDB id, and people
    # watch films in "4K Movies" too. Narrowing it would silently give those users no seeds, no
    # candidates, and an empty row — while the run still reported success.
    #
    # `library_index` covers only the libraries we deliver to: it decides what may be RECOMMENDED,
    # and a pick from a library that never gets a collection could never be shown to anyone.
    seed_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    library_index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    # Only when there is someone to recommend to. Both indexes walk every item in every library,
    # and both are read only inside _run_user — so with no users this is thousands of PMS reads
    # thrown away, in front of the sweep, on the one path (a closed gate) where the sweep is the
    # entire point and must not be preceded by anything that can fail.
    for section in sections if users else []:
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        index = ctx.plex.build_library_index(section)
        seed_index[kind].update(index)
        if section.key in target_keys:
            library_index[kind].update(index)
        else:
            logger.info(
                "library '{}': rows are built in '{}' instead, but watches here still count",
                section.title,
                targets[kind].title,
            )

    # BEFORE ANY USER WORK: delete every row on the server that Plex cannot hide.
    #
    # This is server-wide, not per-user, and it is deliberately not inside the loop below. A row's
    # hideability has nothing to do with whether its owner is enabled tonight, so scoping the
    # sweep to `users` would let one click of "pause" — or `paused_all`, which makes `users` empty
    # — turn a live leak into a permanent one, silently, with every run reporting green.
    #
    # It also runs before anything that can fail. TMDB rate-limits, Tautulli disappears, the PMS
    # times out; none of that may leave a row visible to everyone for another night.
    try:
        # The accumulator is the report's own dict, so rows deleted before any mid-walk failure
        # are still audited — a destructive write must never go unrecorded (rule 10).
        sweep_broken_rows(
            ctx.plex,
            ctx.config,
            # slug -> the marker its row's title must carry. Without this a shared-tag row would
            # survive for any user who gets no picks tonight, still showing them other people's.
            markers={slug: row_marker(account_id) for account_id, slug in ctx.known_slugs.items()},
            dry_run=ctx.config.dry_run,
            deleted=report.swept_rows,
        )
    except Exception as e:
        # Fail closed. We cannot prove the server has no unhidable rows, so we do not write more.
        report.error = f"unhidable-row sweep failed: {type(e).__name__}: {e}"
        report.finished_at = datetime.now(UTC)
        logger.exception("could not sweep unhidable rows — refusing to write anything this run")
        return report

    # Preload label casing + collection ids from the PMS — the source of truth survives
    # restarts and covers users whose delivery fails this run.
    stored_labels = {slug: row.label for slug, row in ctx.plex.owned_collections(ctx.config.label_prefix).items()}

    to_promote: list[UserProfile] = []
    for user in users:
        user_report = UserRunReport(username=user.username, slug=user.slug)
        # A row swept for this user is part of their story this run — but the swept dict is the
        # run-level record, so a paused user's deletion is never lost just because they have no
        # UserRunReport.
        swept_titles = report.swept_rows.get(user.slug, [])
        report.users.append(user_report)
        started = time.monotonic()
        try:
            delivered = _run_user(ctx, user, seed_index, library_index, stored_labels, user_report)
            if delivered:
                to_promote.append(user)
        except Exception as e:
            user_report.status = "error"
            user_report.error = f"{type(e).__name__}: {e}"
            logger.exception("{}: pipeline failed", user.username)
        finally:
            if swept_titles:
                if user_report.diff is None:
                    user_report.diff = CollectionDiff(deleted=list(swept_titles))
                else:
                    user_report.diff.deleted = list(swept_titles) + user_report.diff.deleted
            user_report.duration_s = round(time.monotonic() - started, 2)

    # Shared "popular on this server" rows: built once from aggregate history, delivered UNPROMOTED
    # like the per-person rows so promotion still happens only after the filters are merged.
    shared_to_promote: list[tuple[RowSpec, UserProfile]] = []
    for spec in ctx.config.shared_rows() if users else []:
        started = time.monotonic()
        shared_report = None
        try:
            agg = _run_shared(ctx, spec, users, seed_index, library_index, stored_labels, report)
            shared_report = report.users[-1]
            if agg is not None:
                shared_to_promote.append((spec, agg))
        except Exception as e:
            shared_report = report.users[-1] if report.users and report.users[-1].slug.startswith("shared_") else None
            if shared_report is not None:
                shared_report.status = "error"
                shared_report.error = f"{type(e).__name__}: {e}"
            logger.exception("shared row '{}': failed", spec.slug)
        finally:
            if shared_report is not None:
                shared_report.duration_s = round(time.monotonic() - started, 2)

    # The PMS is the source of truth for which rows exist, so ask it again before writing any
    # share filter. Whatever happened above — a delivery that failed half-way, a crash, a row left
    # by an older version — every row that EXISTS must be excluded on every other user's share.
    # A row missing from this map is a row nobody's filter hides.
    sync_failed = False
    if not ctx.config.dry_run:
        try:
            stored_labels.update(
                {slug: row.label for slug, row in ctx.plex.owned_collections(ctx.config.label_prefix).items()}
            )
        except Exception as e:
            # We can no longer enumerate what exists, so we cannot promise the filters cover it.
            # Sync with what we know, but promote nothing: an unpromoted row is not on anyone's
            # Home screen, and the next run will put this right.
            sync_failed = True
            report.error = f"could not re-read collections before the privacy sync: {type(e).__name__}: {e}"
            logger.exception("could not re-read collections before the privacy sync — nothing will be promoted")

    # Sync EVERY account that shares this server — not the users we happened to process.
    #
    # A row is visible to anyone whose share filter doesn't exclude it. Plex does not care that we
    # consider its owner "not enabled in Rowarr" or "not in tonight's run". Syncing only the
    # processed users is how, on a live server, 45 of 48 accounts ended up able to see three other
    # people's private rows: only the three Rowarr managed had excludes written at all. It is also
    # why `rowarr run --user <slug>` — the documented rollout command — used to mint a row that
    # nobody's filter hid.
    #
    # We ask plex.tv who can see the server rather than trusting our own user table, because the
    # audience is Plex's fact, not ours.
    try:
        roster = {remote.id: remote for remote in ctx.plextv.list_users()}
    except Exception as e:
        # The sweep above has already DELETED rows. Returning the report (rather than letting this
        # escape) is what keeps those deletions in the audit trail (rule 10).
        report.error = f"could not read the plex.tv user list: {type(e).__name__}: {e}"
        report.finished_at = datetime.now(UTC)
        logger.exception("could not read the plex.tv user list — no filters written, nothing promoted")
        return report

    # Whose row is whose, by ACCOUNT ID. Never by name: people rename themselves, and two display
    # names can slugify to the same string — either would quietly hand one account another's row.
    # The profiles the adapter handed us are authoritative (their slug is the one in Rowarr's own
    # records); `known_slugs` covers everyone else Rowarr knows but isn't processing tonight. An
    # account in neither owns no row, and is therefore excluded from every one of them.
    own_slugs = {**ctx.known_slugs, **{u.plex_account_id: u.slug for u in users}}

    audience = _server_audience(users, roster, own_slugs)
    # Restricted shared rows: label -> the account ids allowed to see it. Public shared rows are
    # absent (never excluded). This makes the exclusion audience-aware: a shared-to-some row is
    # hidden from everyone NOT in its audience, exactly like a private row.
    shared_audiences = {
        spec.label.lower(): spec.audience
        for spec in ctx.config.shared_rows()
        if spec.label and spec.audience is not None
    }
    reports = {r.slug: r for r in report.users}
    for user in audience:
        user_report = reports.get(user.slug)
        try:
            own_slug = own_slugs.get(user.plex_account_id)
            written = sync_user_restrictions(
                ctx.plextv,
                user,
                roster.get(user.plex_account_id),  # .get: a user Rowarr knows may be off the share
                stored_labels,
                ctx.snapshots,
                own_label=stored_labels.get(own_slug) if own_slug else None,
                label_prefix=ctx.config.label_prefix,
                shared_audiences=shared_audiences,
                dry_run=ctx.config.dry_run,
            )
            if written:
                # Every share we touch, audited by account id — most of these accounts have no
                # UserRunReport to record it on (rule 10).
                report.filter_writes[user.plex_account_id] = {"username": user.username, "fields": written}
            if user_report is not None:
                user_report.privacy_synced = bool(written)
        except Exception as e:
            # One user's filter not being written means the rows are not private. Nothing gets
            # promoted this run — including for users whose own sync succeeded.
            sync_failed = True
            message = f"privacy sync for {user.username}: {type(e).__name__}: {e}"
            if user_report is not None:
                user_report.status = "error"
                user_report.error = f"{user_report.error} | {message}" if user_report.error else message
            else:
                report.error = f"{report.error} | {message}" if report.error else message
            logger.exception("{}: privacy sync failed", user.username)

    filters_ok = not sync_failed
    for user in to_promote:
        user_report = next(r for r in report.users if r.slug == user.slug)
        if ctx.config.dry_run:
            logger.info("[dry-run] {}: would promote row to shared Home", user.username)
            continue
        if not filters_ok:
            logger.warning("{}: promotion skipped — a privacy sync failed this run", user.username)
            continue
        try:
            # Every row the user has, in every library — they can have several rows (all sharing
            # their label), and promoting only one would leave the others invisible to the one
            # person meant to see them.
            for section in targets.values():
                for collection in ctx.plex.find_owned_collections(section, user.label):
                    ctx.plex.promote(collection, shared=True)
        except Exception as e:
            user_report.status = "error"
            user_report.error = (user_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("{}: promote failed", user.username)

    # Promote the shared rows too — public, so everyone with library access sees them.
    for spec, agg in shared_to_promote if not ctx.config.dry_run and filters_ok else []:
        shared_report = next((r for r in report.users if r.slug == agg.slug), None)
        try:
            for section in targets.values():
                for collection in ctx.plex.find_owned_collections(section, spec.label):
                    ctx.plex.promote(collection, shared=True)
        except Exception as e:
            if shared_report is not None:
                shared_report.status = "error"
                shared_report.error = (shared_report.error or "") + f" | promote: {type(e).__name__}: {e}"
            logger.exception("shared row '{}': promote failed", spec.slug)

    report.finished_at = datetime.now(UTC)
    ok = sum(1 for u in report.users if u.status in ("ok", "cold_start"))
    logger.info("run complete: {}/{} users ok (dry_run={})", ok, len(report.users), ctx.config.dry_run)
    return report


def _server_audience(processed: list[UserProfile], roster: dict, known_slugs: dict[int, str]) -> list[UserProfile]:
    """Everyone who can see this server — the audience the rows must be hidden from.

    Rowarr's own user list is not the answer: it holds the people we build rows FOR, while the
    people rows must be hidden FROM is every account the server is shared with. A user Rowarr has
    never heard of still sees every row whose label their filter doesn't exclude.

    Accounts are matched by plex ACCOUNT ID, never by name. `known_slugs` is the adapter's durable
    account -> slug map (the server's users table; the CLI's slugs.json), so a user who renames
    themselves keeps the slug their row's label was built from. Rebuilding the profile from their
    current username instead would hand them a different slug, and `desired_excludes` would then
    decide their own row belonged to someone else and hide it from them.

    The owner is included here and skipped by `sync_user_restrictions` (Plex cannot restrict the
    owner — rule 5).
    """
    known = {u.plex_account_id: u for u in processed}
    audience = list(processed)
    for account_id, remote in roster.items():
        if account_id in known:
            continue
        audience.append(
            UserProfile(
                username=remote.username,
                plex_account_id=account_id,
                user_type=remote.user_type,
                # The slug Rowarr already gave this account — NOT one derived from their current
                # name. Empty for an account Rowarr has never seen, in which case UserProfile
                # derives one; that is safe precisely because such an account owns no row for the
                # derived slug to be wrong about.
                slug=known_slugs.get(account_id, ""),
            )
        )
    return audience


def _media_filter(items: list, media: str) -> list:
    """Keep only items of the row's media type ('both' keeps everything)."""
    if media == "both":
        return list(items)
    kind = MediaType(media)
    return [item for item in items if item.media_type is kind]


def _run_user(
    ctx: EngineContext,
    user: UserProfile,
    seed_index: dict[MediaType, dict[int, int]],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    user_report: UserRunReport,
) -> bool:
    """Deliver every per-person row this user is in the audience of. Candidates are computed once
    and reused across rows; each row curates and delivers with its own size/media/recipe. Returns
    True when at least one row was delivered (a candidate for promotion)."""
    cfg = ctx.config
    specs = [spec for spec in cfg.per_person_rows() if spec.audience is None or user.plex_account_id in spec.audience]
    if not specs:
        return False  # this user isn't in any per-person row's audience
    # The adapter puts the Phase-A global+per-user recipe on the profile; a row with its own recipe
    # overrides it for that row only.
    base_prompt = user.prompt

    _emit(ctx, user.slug, "history", {})
    user.history = ctx.history_source.fetch(user, min_completion=cfg.min_completion)
    user_report.counts.history = len(user.history)

    cold = len(user.history) < cfg.min_history
    ranked: list[Candidate] = []
    held_back: list[Candidate] = []
    base_cold: list[Pick] = []
    if cold:
        base_cold = _cold_start_picks(ctx, user, cfg)
        user_report.status = "cold_start"
    else:
        # Watches resolve against EVERY library (seed_index) — what a user watched in a second
        # movie library is still what they watched.
        by_rating_key = {key: tmdb_id for idx in seed_index.values() for tmdb_id, key in idx.items()}

        def resolve(item):
            return by_rating_key.get(item.rating_key) if item.rating_key else None

        seeds = derive_seeds(user.history, resolve, max_seeds=cfg.max_seeds)
        user_report.counts.seeds = len(seeds)
        _emit(ctx, user.slug, "candidates", {"history": len(user.history), "seeds": len(seeds)})
        pool = candidates_mod.gather_candidates(ctx.tmdb, seeds)
        user_report.counts.candidates = len(pool)

        watched_ids = {(s.tmdb_id, s.media_type) for s in seeds}
        recent = ctx.recent_picks.get(user.slug, set())
        in_library = candidates_mod.filter_candidates(
            pool,
            library_index,
            watched_tmdb_ids=watched_ids,
            excluded_genres=user.excluded_genres,
            recent_pick_ids=recent,
        )
        user_report.counts.in_library = len(in_library)
        ranked = ranking.pre_rank(in_library, cfg.candidates_pre_rank)
        user_report.counts.pre_ranked = len(ranked)

        # Titles the staleness guard held back. They are still valid recommendations — they were
        # simply on the row recently — so they backfill a row that fresh candidates can't fill.
        # Without this a thin candidate pool SHRINKS the row rather than repeating a title.
        # (It tops the row up overall; it does not promise every library a share of it.)
        # TMDB ids are only unique WITHIN a namespace — movie 1399 and TV 1399 are different
        # titles — so identity here is (id, type), never the id alone.
        fresh_ids = {(c.tmdb_id, c.media_type) for c in in_library}
        held_back = ranking.pre_rank(
            [
                c
                for c in candidates_mod.filter_candidates(
                    pool,
                    library_index,
                    watched_tmdb_ids=watched_ids,
                    excluded_genres=user.excluded_genres,
                    recent_pick_ids=set(),
                )
                if (c.tmdb_id, c.media_type) not in fresh_ids
            ],
            cfg.candidates_pre_rank,
        )
        user_report.status = "ok"

    if not ctx.plex.sections_by_type():
        raise RuntimeError("no movie or show library found for delivery")

    # One diff and label map for the whole user, accumulated across their rows. Handed to delivery
    # rather than returned from it: a row can half-succeed across libraries, and a row that was
    # created and labelled must reach `stored_labels` even if a later write blows up — otherwise
    # nobody's share filter excludes it and it is visible to everyone (the leak we exist to fix).
    user_report.diff = CollectionDiff()
    all_picks: list[Pick] = []
    delivered_any = False
    for spec in specs:
        k = user.row_size or spec.size
        if cold:
            picks = [
                Pick(**{**pick.__dict__, "rank": i + 1})
                for i, pick in enumerate(_media_filter(base_cold, spec.media)[:k])
            ]
        else:
            user.prompt = spec.prompt if spec.prompt is not None else base_prompt
            pool = _media_filter(ranked, spec.media)
            _emit(ctx, user.slug, "curating", {"candidates": len(pool)})
            try:
                picks = ctx.curator.curate(user, pool, k)
                user_report.llm_tokens += getattr(ctx.curator, "last_tokens", 0)
            except CuratorError as e:
                logger.warning("{}: curator failed ({}); degrading to heuristic mode", user.username, e)
                picks = NullCurator().curate(user, pool, k)
            if len(picks) < k:
                picks = _pad_picks(picks, pool + _media_filter(held_back, spec.media), k)
        all_picks.extend(picks)
        _emit(ctx, user.slug, "delivering", {"picks": len(picks)})
        deliver_rows(
            ctx.plex,
            user,
            picks,
            cfg,
            spec,
            sole_row=len(specs) == 1,
            dry_run=cfg.dry_run,
            stored_labels=stored_labels,
            diff=user_report.diff,
        )
        delivered_any = delivered_any or bool(picks)

    user_report.picks = all_picks
    user_report.counts.picks = len(all_picks)
    if not all_picks:
        logger.warning("{}: no picks produced — existing rows are left as they are", user.username)
    return delivered_any  # nothing delivered -> nothing to promote


def _run_shared(
    ctx: EngineContext,
    spec: RowSpec,
    users: list[UserProfile],
    seed_index: dict[MediaType, dict[int, int]],
    library_index: dict[MediaType, dict[int, int]],
    stored_labels: dict[str, str],
    report: RunReport,
) -> UserProfile | None:
    """Deliver one shared 'popular on this server' row from AGGREGATE history.

    A title only qualifies once at least ``spec.min_watchers`` distinct people in the audience have
    watched it, so no single person's viewing can reach a public row. Reasons are aggregate-framed —
    never "because you watched X", since there is no single "you". Returns the synthetic profile when
    a row was delivered (a promotion candidate), else None.
    """
    cfg = ctx.config
    audience = [u for u in users if spec.audience is None or u.plex_account_id in spec.audience]
    slug = f"{SHARED_SLUG_PREFIX}_{spec.slug}"
    user_report = UserRunReport(username=f"Shared · {spec.slug}", slug=slug)
    report.users.append(user_report)
    if not audience:
        user_report.status = "skipped"
        return None

    by_rating_key = {key: tmdb_id for idx in seed_index.values() for tmdb_id, key in idx.items()}

    def resolve(item) -> int | None:
        return item.tmdb_id or (by_rating_key.get(item.rating_key) if item.rating_key else None)

    # Count DISTINCT watchers per title across the audience; keep only titles enough people watched.
    watchers: dict[tuple[int, MediaType], set[int]] = {}
    example: dict[tuple[int, MediaType], WatchedItem] = {}
    for user in audience:
        for item in ctx.history_source.fetch(user, min_completion=cfg.min_completion):
            tmdb_id = resolve(item)
            if tmdb_id is None:
                continue
            key = (tmdb_id, item.media_type)
            watchers.setdefault(key, set()).add(user.plex_account_id)
            example.setdefault(key, item)
    agg_history = [example[key] for key, who in watchers.items() if len(who) >= spec.min_watchers]
    user_report.counts.history = len(agg_history)

    agg = UserProfile(
        username="Everyone",
        plex_account_id=0,
        user_type=UserType.SHARED,
        slug=slug,
        history=agg_history,
        prompt=spec.prompt,
    )
    if not agg_history:
        user_report.status = "skipped"
        logger.info("shared row '{}': no title watched by >= {} people yet", spec.slug, spec.min_watchers)
        return None

    seeds = derive_seeds(agg_history, resolve, max_seeds=cfg.max_seeds)
    pool = candidates_mod.gather_candidates(ctx.tmdb, seeds)
    watched_ids = {(s.tmdb_id, s.media_type) for s in seeds}
    in_library = candidates_mod.filter_candidates(
        pool,
        library_index,
        watched_tmdb_ids=watched_ids,
        excluded_genres=set(),
        recent_pick_ids=ctx.recent_picks.get(slug, set()),
    )
    ranked = _media_filter(ranking.pre_rank(in_library, cfg.candidates_pre_rank), spec.media)
    k = spec.size
    try:
        picks = ctx.curator.curate(agg, ranked, k)
    except CuratorError:
        picks = NullCurator().curate(agg, ranked, k)
    if len(picks) < k:
        picks = _pad_picks(picks, ranked, k)
    # Force aggregate framing regardless of curator: a shared row is nobody's "because you watched".
    picks = [Pick(**{**pick.__dict__, "reason": "Popular on this server"}) for pick in picks]

    user_report.picks = picks
    user_report.counts.picks = len(picks)
    user_report.status = "ok"
    user_report.diff = CollectionDiff()
    _emit(ctx, slug, "delivering", {"picks": len(picks)})
    deliver_rows(
        ctx.plex,
        agg,
        picks,
        cfg,
        spec,
        sole_row=True,  # one shared row per label
        dry_run=cfg.dry_run,
        stored_labels=stored_labels,
        diff=user_report.diff,
    )
    return agg if picks else None


def _pad_picks(picks: list[Pick], ranked: list[Candidate], k: int) -> list[Pick]:
    """Top up short curator output from the heuristic order (never invents titles)."""
    have = {(p.tmdb_id, p.media_type) for p in picks}  # movie 1399 and TV 1399 are different titles
    fillers = NullCurator().curate(
        UserProfile(username="", plex_account_id=0, user_type=UserType.SHARED),
        [c for c in ranked if (c.tmdb_id, c.media_type) not in have],
        k - len(picks),
    )
    out = list(picks)
    for f in fillers:
        out.append(Pick(**{**f.__dict__, "rank": len(out) + 1}))
    return out


def _cold_start_picks(ctx: EngineContext, user: UserProfile, cfg: EngineConfig) -> list[Pick]:
    """ "Popular on <server>" fallback for a user with thin history: top-rated titles.

    Every library gets a share, not just movies — a movies-only cold start would hand delivery a
    pick list with no shows in it, and a thin-history night (a Tautulli outage is enough) would
    then leave a TV watcher with a row of films they never asked for.
    """
    sections = ctx.plex.sections_by_type()
    if not sections:
        return []
    k = user.row_size or cfg.row_size
    share = max(1, k // len(sections))

    picks: list[Pick] = []
    for index, (kind, section) in enumerate(sections.items()):
        # The last library takes the remainder, so `row_size` titles are delivered, not k - k % n.
        wanted = k - len(picks) if index == len(sections) - 1 else min(share, k - len(picks))
        if wanted <= 0:
            break
        for item in section.search(sort="audienceRating:desc", limit=wanted * 2):
            tmdb_id = next(
                (int(g.id.removeprefix("tmdb://")) for g in getattr(item, "guids", []) if g.id.startswith("tmdb://")),
                None,
            )
            if tmdb_id is None:
                continue
            picks.append(
                Pick(
                    tmdb_id=tmdb_id,
                    rating_key=item.ratingKey,
                    title=item.title,
                    rank=len(picks) + 1,
                    reason="Popular on this server",
                    media_type=kind,
                )
            )
            if len([p for p in picks if p.media_type is kind]) == wanted:
                break
    return picks
