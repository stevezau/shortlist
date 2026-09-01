"""Move the owner's watching onto a separate Plex account.

**The problem this exists for.** Plex hides a per-person row from everyone else through the SHARE
that person has with the server. The owner has no share with themselves, so nothing can hide
anything from them: the Recommended shelf inside every library shows them all N rows. Plex offers no
setting for this, and it is by a wide margin the most-asked question about Shortlist.

The only real fix is to stop watching as the admin account. This module makes that cheap: find a Home
user and replicate the owner's watch state onto it, so the new account's picks are right from the
first run and Plex shows the same checkmarks, the same half-finished shows and the same Continue
Watching shelf.

**What this used to get wrong.** It copied the `watched_titles` cache and scrobbled each row's rating
key. For a show that key is the SHOW's, and a show-key scrobble marks every episode — so someone 400
episodes into One Piece arrived with all 1,100 finished. On the maintainer's own account 342 of 535
watched shows are partial, so that was the common case, not an edge. The cache could not have done
better: it is built from `?unwatched=0`, which is show-level and completions-only, and knows how MANY
episodes were watched but never which. So the source of truth moved to a live per-EPISODE read of the
source account, and `shortlist.engine.watch_replica` turns two states into an ordered write plan.

**It mirrors.** State the source lacks is removed, which is what makes the result a replica and what
repairs an account the old version spoiled. That makes this the one path in Shortlist that can delete
watch history, so it snapshots first (rule 2) and `undo_transfer` restores from that snapshot.

**The date problem, and why `source_viewed_at` still exists.** Plex has no way to backdate a watch —
every write is stamped `now`, and no endpoint accepts a date. So the true dates are kept on our side:
`source_viewed_at` per cached title, and the source's own play-log rows copied into `watch_events`.
Writes are additionally ordered oldest-first, which cannot restore the dates but does make the
target's Continue Watching sort the way the source's does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, UserType
from shortlist.engine.watch_replica import (
    OpKind,
    WatchState,
    build_plan,
    removals_by_title,
    summarise,
)
from shortlist.server.db.models import User, WatchedTitle, WatchEvent, WatchStateSnapshot, utcnow

#: How many titles the dry run names before it stops listing. A removal list is for a human to sanity
#: check, and nobody reads eleven thousand lines — but a bare count is not something anyone can check
#: either, which is the whole reason the list exists.
REMOVAL_PREVIEW = 50

#: How many values go into one `IN (...)`. SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 32766, and
#: these lists carry one entry per title on the account.
_IN_CHUNK = 500


@dataclass
class TransferReport:
    """What a transfer did, or would do under `dry_run`. Every field is auditable (rule 10)."""

    planned: int = 0
    applied: int = 0
    #: The PMS answered 401/403/404 — the title is in a library this account cannot see. A normal
    #: outcome for a target shared fewer libraries than the source, not a failure.
    unreachable: int = 0
    #: A write that RAISED — a timeout, a 500, a broken connection. Counted apart from `unreachable`
    #: because they are opposite claims: one says "that title isn't there for them", the other says
    #: "we don't know what happened". Folding them together made three timeouts render as "3 were in
    #: libraries that account can't see" AND "that account now matches yours", both false.
    failed: int = 0
    marks: int = 0
    unmarks: int = 0
    offsets_set: int = 0
    offsets_cleared: int = 0
    #: Titles this run would un-mark or rewind, by name. The only destructive part of the feature, so
    #: it is reported as names rather than a number — see REMOVAL_PREVIEW.
    removals_preview: list[str] = field(default_factory=list)
    #: Set by the verify pass: leaves that still disagree after everything was applied.
    verify_mismatched: int = 0
    verify_checked: int = 0
    #: Show rows un-scrobbled because every episode of them was removed. Counted separately because
    #: they are not leaves and the audit row must still be able to explain them (rule 10).
    shows_cleared: int = 0
    #: Libraries the TARGET account cannot see. Not a failure — but it makes the snapshot partial, so
    #: `undo_transfer` refuses to restore from it.
    target_unreadable: list[str] = field(default_factory=list)
    events_copied: int = 0
    titles_cached: int = 0
    snapshot_id: int | None = None
    dry_run: bool = False
    #: The SOURCE has nothing to copy. Reported apart from `planned == 0`, because "they already
    #: match" is success and this is the copy being impossible — collapsing the two into one bare 0 is
    #: what made the setup wizard silently useless (#88).
    source_empty: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "planned": self.planned,
            "applied": self.applied,
            "unreachable": self.unreachable,
            "failed": self.failed,
            "marks": self.marks,
            "unmarks": self.unmarks,
            "offsets_set": self.offsets_set,
            "offsets_cleared": self.offsets_cleared,
            "removals_preview": self.removals_preview,
            "verify_mismatched": self.verify_mismatched,
            "verify_checked": self.verify_checked,
            "shows_cleared": self.shows_cleared,
            "target_unreadable": self.target_unreadable,
            "events_copied": self.events_copied,
            "titles_cached": self.titles_cached,
            "snapshot_id": self.snapshot_id,
            "dry_run": self.dry_run,
            "source_empty": self.source_empty,
            "errors": self.errors,
        }


def candidate_home_users(plextv, session: Session) -> list[dict]:
    """Home users on the owner's account that could become their watching account.

    Excludes the owner themselves and anyone already registered in Shortlist with a row — moving to
    an account that already has its own Picked-for-You would merge two people's taste into one.
    PIN-protected accounts are listed but flagged: `canary_server_token` cannot switch to them, so a
    transfer to one cannot mint the token it needs.
    """
    known = {u.plex_account_id for u in session.query(User).filter(User.enabled.is_(True))}
    out = []
    for home_user in plextv.home_users():
        account_id = int(home_user.get("id") or 0)
        if not account_id or home_user.get("admin"):
            continue
        out.append(
            {
                "plex_account_id": account_id,
                "title": home_user.get("title") or home_user.get("username") or f"account {account_id}",
                "protected": bool(home_user.get("protected")),
                "already_a_shortlist_user": account_id in known,
            }
        )
    return out


def _check_pair(session: Session, from_user_id: int, to_user_id: int) -> tuple[User, User]:
    """The two users, or a refusal. Same guards as before the rewrite — they are about identity."""
    source = session.get(User, from_user_id)
    target = session.get(User, to_user_id)
    if source is None or target is None:
        raise LookupError("both the source and the target must be known users")
    if from_user_id == to_user_id:
        raise ValueError("cannot transfer a watch history onto the same account")
    # A WATCHING account is one of the owner's own Home profiles. Copying their history onto a SHARED
    # user would build that person's Picked-for-You from the owner's taste and exclude titles they
    # have never seen — one person's data silently wearing another's name. Now it would also DELETE
    # that person's real watch history, since the mirror removes whatever the source lacks. The UI
    # only ever offers Home users; this is the same rule at the layer that actually writes.
    if UserType(target.user_type) is not UserType.MANAGED:
        raise ValueError(
            "a watching account must be one of your own Plex Home users — "
            f"{target.username!r} is a {target.user_type} account"
        )
    return source, target


def _section_pairs(plex) -> list[tuple[str, MediaType]]:
    """`(section_key, media_type)` for every library that can hold a watch."""
    return [
        (str(s.key), MediaType.MOVIE if s.type == "movie" else MediaType.SHOW) for s in plex.sections(("movie", "show"))
    ]


def take_snapshot(sessions, user_id: int, state: WatchState, job_id: int | None) -> int:
    """Persist the target's state BEFORE the first write, IN ITS OWN COMMITTED TRANSACTION (rule 2).

    Its own transaction is the whole point, and it was a flush before. The caller's transaction is
    committed only after every Plex write has landed, so anything raising in between — the verify
    read hitting a 500, the play-log copy, a stray `OperationalError` — rolled the snapshot back while
    the writes stayed on Plex. Worse, the job then retries, re-reads a half-mirrored target, finds the
    plan already converged, and takes no snapshot at all: the un-marked watches are gone with no
    record anywhere. That is precisely the failure rule 2 exists to make impossible.

    Returns the snapshot id. Reuses an existing un-restored snapshot for the same job, so a retry
    keeps pointing at the state the account was really in before the FIRST attempt, not the state the
    first attempt left behind.

    Counts and offsets, not just watched/unwatched: a restore that re-marked a rewatched film once, or
    a part-watched episode as finished, would produce a third state that existed on neither account.
    """
    with sessions() as session:
        if job_id is not None:
            existing = (
                session.query(WatchStateSnapshot)
                .filter(
                    WatchStateSnapshot.job_id == job_id,
                    WatchStateSnapshot.user_id == user_id,
                    WatchStateSnapshot.restored_at.is_(None),
                )
                .first()
            )
            if existing is not None:
                logger.info("watch replication: reusing snapshot {} from an earlier attempt", existing.id)
                return existing.id
        row = _snapshot_row(user_id, state, job_id)
        session.add(row)
        session.commit()
        return row.id


def _snapshot_row(user_id: int, state: WatchState, job_id: int | None) -> WatchStateSnapshot:
    row = WatchStateSnapshot(
        user_id=user_id,
        job_id=job_id,
        # Whether the read behind it saw every library. A snapshot taken from a partial read describes
        # less than the account holds, and restoring from it would un-mark everything it never
        # recorded — so `undo_transfer` refuses rather than trusting it.
        complete=state.is_complete,
        # Five elements, not four: the show key is what `_clear_emptied_shows` needs to tell a show
        # the restore has emptied from one it still holds episodes of. Reconstructing an `ItemState`
        # without it made every restored op carry `show_rating_key=None`, so nothing could be cleared
        # and a naive call would have un-scrobbled shows the snapshot still wanted.
        state=[
            [i.rating_key, i.view_count, i.view_offset_ms, i.media_type, i.show_rating_key]
            for i in state.items.values()
        ],
    )
    return row


def transfer_watch_history(
    session: Session,
    *,
    sessions,
    from_user_id: int,
    to_user_id: int,
    plex,
    source_token: str,
    target_token: str,
    dry_run: bool = False,
    job_id: int | None = None,
) -> TransferReport:
    """Make the target account's watch state match the source's, exactly.

    Args:
        session: An open session; the caller owns the transaction.
        sessions: The session FACTORY. Required separately from `session` because the snapshot must
            be committed in its own transaction before the first Plex write — the caller's
            transaction is not committed until every write has landed, so a flush would be rolled
            back by anything that raised in between, leaving writes on Plex and no undo.
        from_user_id: Whose watching to replicate. Usually the owner, but any account: someone who
            already moved once has their history on THAT account, not on the admin one.
        to_user_id: The watching account receiving it. Must be a Plex Home (managed) user.
        plex: A `PlexClient`.
        source_token: Server token to read the SOURCE as. The admin token for an OWNER source, and
            that account's OWN server token for any other — never the admin token for a non-owner, or
            the owner's history is copied while the audit row names somebody else.
        target_token: The target's own server token, from `canary_server_token`. Every write uses it.
        dry_run: Read and plan, write nothing — including no snapshot, since there is nothing to
            protect (rule 8).
        job_id: The job this runs under, so an undo can find this transfer's snapshot rather than the
            newest one.

    Returns:
        A `TransferReport`.

    Raises:
        LookupError: Either user is unknown.
        ValueError: The target is not a Home user, or is the source.
    """
    source_user, target_user = _check_pair(session, from_user_id, to_user_id)
    sections = _section_pairs(plex)

    source_state = plex.read_watch_state(sections, source_token)
    # REFUSED, not worked around. `build_plan` treats the source as authoritative and removes whatever
    # it does not contain, so mirroring from a read that could not see a library un-marks every title
    # that library holds on the target — measured shape: 10,995 episodes — while reporting a clean
    # run, because the target genuinely does match the truncated source.
    #
    # There is no safe partial behaviour here worth guessing at: a source that cannot see a library
    # has no opinion about it, which is not the same as "nothing watched there".
    if not source_state.is_complete:
        raise ValueError(
            "cannot copy from an account that cannot see every library — "
            f"library {', '.join(source_state.unreadable)} is not shared with it. "
            "Share it with that account, or copy from one that can see everything."
        )

    target_state = plex.read_watch_state(sections, target_token)

    report = TransferReport(dry_run=dry_run, source_empty=not source_state.items)
    # NOT refused: a target shared fewer libraries than the source is a legitimate setup, and nothing
    # can be written there anyway (those writes 404 and land in `unreachable`). But the SNAPSHOT is
    # then partial, and an undo restoring from a partial snapshot would remove watches it never
    # recorded — so it is flagged here and `undo_transfer` refuses to act on it.
    report.target_unreadable = list(target_state.unreadable)
    plan = build_plan(source_state, target_state)
    report.planned = len(plan)
    counts = summarise(plan)
    report.marks = counts[OpKind.MARK.value]
    report.unmarks = counts[OpKind.UNMARK.value]
    report.offsets_set = counts[OpKind.SET_OFFSET.value]
    report.offsets_cleared = counts[OpKind.CLEAR_OFFSET.value]
    report.removals_preview = removals_by_title(plan, limit=REMOVAL_PREVIEW)

    if not dry_run and plan:
        # Before the first write, never after — a crash between the two is exactly what the snapshot
        # is for, and one taken afterwards would record our own changes as the user's own state.
        report.snapshot_id = take_snapshot(sessions, to_user_id, target_state, job_id)

    # The KEYS we could not write, not a count of them. The verify pass has to exclude exactly those
    # from its mismatch tally, and subtracting one population's size from another's only happened to
    # give the right answer when the two coincided — eight unreachable and two real failures reported
    # a clean run.
    could_not_write: set[int] = set()
    for op in plan:
        try:
            if plex.apply_watch_op(op, target_token, dry_run=dry_run):
                report.applied += 1
            else:
                # Refused: 401/403/404. Genuinely not writable for this account, so the verify pass
                # must not count it as a mismatch.
                report.unreachable += 1
                could_not_write.add(op.rating_key)
        except Exception as e:
            # One failure must not abandon the other eleven thousand. Recorded, not raised — and
            # deliberately NOT added to `could_not_write`: we do not know the title is unwritable, so
            # it has to keep showing up as a mismatch rather than being excused into a clean report.
            report.failed += 1
            if len(report.errors) < 10:
                report.errors.append(f"{op.title or op.rating_key}: {type(e).__name__}")

    # AFTER the leaf writes, never before: un-scrobbling a show key clears every episode under it, so
    # doing it first would wipe the very episodes the plan had just been asked to mark. Dry-run aware
    # rather than skipped, because a preview that omits them understates what a real run does.
    _clear_emptied_shows(plex, plan, source_state, target_token, report, dry_run=dry_run)

    if not dry_run:
        report.events_copied = _copy_play_events(session, source_user, target_user, plex, source_state)
        # Flushed so `stamp_true_dates` can read the events this transfer just wrote — it queries the
        # table rather than the in-memory list, precisely so `WatchSync` can re-run it later.
        session.flush()
        report.titles_cached = stamp_true_dates(session, to_user_id)
        _verify(plex, sections, source_state, target_token, report, could_not_write)

    logger.info(
        "watch replication {} -> {}: {} planned ({} marks, {} un-marks, {} offsets), "
        "{} applied, {} unreachable, {} mismatched after verify (dry_run={})",
        source_user.username,
        target_user.username,
        report.planned,
        report.marks,
        report.unmarks,
        report.offsets_set + report.offsets_cleared,
        report.applied,
        report.unreachable,
        report.verify_mismatched,
        dry_run,
    )
    return report


def _clear_emptied_shows(
    plex, plan, source_state: WatchState, target_token: str, report: TransferReport, *, dry_run: bool = False
) -> None:
    """Un-scrobble the SHOW key of any show whose episodes we just emptied.

    Un-scrobbling an episode does not clear its show: the show row keeps its own `viewCount` and
    `lastViewedAt`, so it still comes back from `?type=2&unwatched=0` — the read `watched_titles` is
    built from — now reading 0/N. The target then looks to Shortlist like someone who has watched a
    show with none of it watched, and the engine stops offering it. It is the same residue that
    explains the 63 zero-episode shows found on a real account, and §2 of the design records the
    behaviour that causes it.

    Only shows we actually emptied, and only when the SOURCE has nothing left in them — never a show
    the target still has episodes of, and never one the source watches.
    """
    emptied: set[int] = set()
    for op in plan:
        if op.kind in (OpKind.UNMARK, OpKind.CLEAR_OFFSET) and op.show_rating_key:
            emptied.add(op.show_rating_key)
    if not emptied:
        return
    # A show the source still watches keeps its row — those episodes were re-marked, not removed.
    still_wanted = {i.show_rating_key for i in source_state.items.values() if i.show_rating_key}
    for show_key in sorted(emptied - still_wanted):
        try:
            # The return value matters, like it does in the leaf loop: a show in a library the target
            # cannot see answers False, and auditing that as "cleared" is a rule-10 inaccuracy.
            if plex.unscrobble_as(show_key, target_token, dry_run=dry_run):
                report.shows_cleared += 1
            else:
                report.unreachable += 1
        except Exception as e:
            # Counted, not just logged — an audit row that silently drops the eleventh failure cannot
            # answer what changed (rule 10).
            report.failed += 1
            if len(report.errors) < 10:
                report.errors.append(f"show {show_key}: {type(e).__name__}")


def _verify(
    plex,
    sections,
    source_state: WatchState,
    target_token: str,
    report: TransferReport,
    could_not_write: set[int],
) -> None:
    """Re-read the target and diff it against the source. Fills the verify fields on `report`.

    Deliberately re-derived rather than assumed from the write results: a write the PMS accepted is
    not a write that took effect, and the old transfer reported counts it had never checked. Comparing
    LEAVES only — a show row is state Plex derives and can disagree with its own episodes.

    Titles the PMS refused are excluded BY KEY, not by count. They are legitimately still different —
    a target shared fewer libraries cannot hold them — and counting them again here would make a
    correct run look broken. Subtracting the unreachable COUNT from the mismatch count was the first
    attempt and it silently hid real failures whenever the two populations differed.
    """
    after = plex.read_watch_state(sections, target_token)
    remaining = build_plan(source_state, after)
    report.verify_checked = len(source_state.items)
    report.verify_mismatched = sum(1 for op in remaining if op.rating_key not in could_not_write)


def _copy_play_events(session: Session, source: User, target: User, plex, source_state: WatchState) -> int:
    """Copy the source's play log onto the target's account id, keeping the TRUE timestamps.

    This is the only dated history the new account will ever have: probed live, a scrobble writes no
    row to `/status/sessions/history/all`, so the target's own log stays empty however much we write.

    `source='transfer'` is what keeps these out of pick attribution. They are real watches and must
    feed recency and seeds, but they are not this person pressing play on a Shortlist row, and a
    back-dated credit is a bug shape this codebase has shipped before.
    """
    rows = plex.play_history(since=None)
    mine = [e for e in rows if e.plex_account_id == source.plex_account_id]
    # NO early return on an empty log — the fallback below is the path that actually carries the
    # dates on a real server, and returning here skipped it entirely.
    #
    # Namespaced so the copy is idempotent on a re-run and can never collide with a genuine row from
    # Plex's own log, whose keys are bare paths.
    keys = {f"transfer:{target.plex_account_id}:{e.history_key or e.rating_key}" for e in mine}
    keys |= {f"transfer:{target.plex_account_id}:state:{k}" for k in source_state.items}
    # CHUNKED. `keys` is one entry per play-log row PLUS one per source leaf, and SQLite's default
    # SQLITE_MAX_VARIABLE_NUMBER is 32766 — so a heavy account (16k leaves plus a populated log) raised
    # `too many SQL variables` AFTER every Plex write had already landed. The live run survived only
    # because that account's play log happened to be empty.
    already: set[str] = set()
    ordered = list(keys)
    for start in range(0, len(ordered), _IN_CHUNK):
        batch = ordered[start : start + _IN_CHUNK]
        rows = session.query(WatchEvent.history_key).filter(WatchEvent.history_key.in_(batch)).all()
        already |= {k for (k,) in rows}
    added = 0
    for event in mine:
        key = f"transfer:{target.plex_account_id}:{event.history_key or event.rating_key}"
        if key in already:
            continue
        already.add(key)
        session.add(
            WatchEvent(
                plex_account_id=target.plex_account_id,
                rating_key=event.rating_key,
                show_rating_key=event.show_rating_key,
                media_type=event.media_type,
                viewed_at=event.viewed_at,
                source="transfer",
                history_key=key,
            )
        )
        added += 1

    # --- and the fallback, which on a real server turned out to be the MAIN path ---------------
    #
    # Measured on the maintainer's own account: 10,948 watched leaves and ZERO usable play-log rows,
    # because the log only reaches back so far and because a bulk "mark as watched" never writes one
    # at all. Copying the log alone therefore carried no dates whatsoever, and `source_viewed_at`
    # stayed NULL — which is precisely the failure the column exists to prevent.
    #
    # The leaf read does carry `lastViewedAt`, so anything the log could not date is dated from that.
    # It is a weaker fact — the LATEST view rather than each play — and it is recorded as such: one
    # synthesised row per title, not one per play.
    covered = {e.rating_key for e in mine}
    for item in source_state.items.values():
        if item.rating_key in covered or not item.last_viewed_at:
            continue
        key = f"transfer:{target.plex_account_id}:state:{item.rating_key}"
        if key in already:
            continue
        already.add(key)
        session.add(
            WatchEvent(
                plex_account_id=target.plex_account_id,
                rating_key=item.rating_key,
                show_rating_key=item.show_rating_key,
                media_type=item.media_type,
                viewed_at=datetime.fromtimestamp(item.last_viewed_at, tz=UTC),
                source="transfer",
                history_key=key,
            )
        )
        added += 1
    return added


def stamp_true_dates(session: Session, user_id: int) -> int:
    """Stamp `source_viewed_at` on a transferred account's cached titles, from the copied play log.

    Plex reports every replicated title as watched TODAY — it accepts no date — and the sync writes
    that into `watched_titles.viewed_at`. Seeds come from the most RECENT watches, so a set sharing
    one timestamp orders arbitrarily and the new account's recommendations become noise.

    Derived from `watch_events` rows carrying `source='transfer'`, NOT from the state read that
    produced them, because of an ordering problem: on a FIRST transfer the target has no
    `watched_titles` rows at all — they are created by the next watch sync, reading back what we just
    wrote to Plex. Stamping from the in-memory read would therefore find nothing to stamp and the true
    dates would never land. Sourcing it from a table means it can be re-run afterwards, which is what
    `WatchSync` does once the rows exist.

    A show is stamped with the newest of its EPISODES: `watched_titles` is keyed at show level, while
    the replica and the play log both work in leaves.

    Returns how many rows were stamped. Idempotent — a row that already has a date is left alone, so
    a person's own later watching is never overwritten by a copy of somebody else's.
    """
    # Runs for EVERY user on EVERY watch sync, and all but the one transferred account has nothing to
    # do — so the genuinely cheapest question goes first. Gating on un-stamped `watched_titles` was
    # not it: `source_viewed_at` is NULL on every row for the 99% who never had a transfer, so that
    # "gate" materialised each user's entire watched set every night to discover there was nothing
    # to do. One indexed existence check answers it instead.
    account_id = _account_for(session, user_id)
    has_transfer = (
        session.query(WatchEvent.id)
        .filter(WatchEvent.plex_account_id == account_id, WatchEvent.source == "transfer")
        .first()
    )
    if has_transfer is None:
        return 0

    rows_to_stamp = (
        session.query(WatchedTitle)
        .filter(WatchedTitle.user_id == user_id, WatchedTitle.source_viewed_at.is_(None))
        .all()
    )
    if not rows_to_stamp:
        return 0
    wanted = {row.rating_key for row in rows_to_stamp}

    newest: dict[int, datetime] = {}
    # Only the keys that can actually be stamped — a show is matched through `show_rating_key`, a
    # movie through its own, so both columns are asked for the same set.
    events = (
        session.query(WatchEvent.rating_key, WatchEvent.show_rating_key, WatchEvent.viewed_at)
        .filter(
            WatchEvent.plex_account_id == account_id,
            WatchEvent.source == "transfer",
        )
        .yield_per(2_000)
    )
    # Filtered in Python, not with a giant `IN`: `wanted` is one entry per un-stamped title, which on
    # a fresh transfer is every title the account has — far past SQLite's variable limit.
    for rating_key, show_rating_key, viewed_at in events:
        if rating_key not in wanted and show_rating_key not in wanted:
            continue
        key = show_rating_key or rating_key
        when = viewed_at if viewed_at.tzinfo else viewed_at.replace(tzinfo=UTC)
        if when > newest.get(key, datetime(1970, 1, 1, tzinfo=UTC)):
            newest[key] = when
    if not newest:
        return 0

    stamped = 0
    rows = rows_to_stamp
    for row in rows:
        when = newest.get(row.rating_key)
        if when is None:
            continue
        row.source_viewed_at = when
        stamped += 1
    return stamped


def undo_transfer(
    session: Session,
    *,
    sessions,
    snapshot_id: int,
    plex,
    target_token: str,
    dry_run: bool = False,
    job_id: int | None = None,
) -> TransferReport:
    """Put the target account back exactly as the snapshot found it.

    Restores from the snapshot rather than replaying the writes backwards. Those are different
    operations once the transfer can also REMOVE things: re-marking what we un-marked, without the
    counts and offsets that sat behind it, leaves a third state that existed on neither account.

    Idempotent by the `restored_at` stamp — a second press reports "already restored" instead of
    replaying against a state the account no longer has.

    **It is itself a mirror, so it is itself destructive.** Anything on the account that the snapshot
    does not contain — including everything the person watched on it AFTER the transfer — is
    un-marked. That got none of the protection the transfer's destructive half got, so it takes its
    own snapshot first (rule 2: an undo has to be undoable), reports its removals by name, and the UI
    previews it before running it for real.
    """
    snapshot = session.get(WatchStateSnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError("no snapshot with that id — nothing to restore")
    report = TransferReport(dry_run=dry_run)
    if snapshot.restored_at is not None:
        report.errors.append("that transfer has already been undone")
        return report
    # A snapshot taken from a partial read describes less than the account actually held, and this
    # restore is a MIRROR of it — so it would un-mark every watch the snapshot never recorded. The
    # transfer flags that at the time rather than discovering it here.
    # The SAME identity rule the transfer enforces, at the layer that actually writes. `_check_pair`
    # refuses a non-MANAGED target because mirroring onto a shared user deletes that person's real
    # watch history — and the undo is a mirror too, but it trusted `snapshot.user_id` unconditionally.
    #
    # It can drift: `user_sync` reassigns `user_type` on every roster sync, so a Home user removed
    # from Home and re-invited as a shared account flips MANAGED → SHARED while their snapshot stays
    # listed and undoable. Restoring it would then mirror onto a real person's account.
    target = session.get(User, snapshot.user_id)
    if target is None or UserType(target.user_type) is not UserType.MANAGED:
        report.errors.append(
            "that account is no longer one of your own Plex Home users, so its watch history will not be overwritten"
        )
        return report
    if not snapshot.complete:
        report.errors.append(
            "that snapshot is incomplete — a library was not readable when it was taken, so restoring "
            "from it could remove watches it never recorded"
        )
        return report

    from shortlist.engine.watch_replica import ItemState

    # Rows written before the show key was added carry four elements; unpacked leniently so an older
    # snapshot still restores. It must NOT try to clear show rows in that case — see `knows_shows`.
    # `all`, not `any`, and `all([])` is True — both halves are load-bearing.
    #
    #   * `any` is False for an EMPTY snapshot, which is what a transfer onto a brand-new watching
    #     account records — the normal setup. Undoing that un-marks every episode and then skipped
    #     the show clear, leaving exactly the scrobbled-show residue this rewrite exists to repair.
    #   * `any` is True for a MIXED snapshot, where one 5-element row makes the whole thing look
    #     trustworthy while the 4-element rows contribute nothing to `still_wanted` — so a show the
    #     snapshot asked to keep is un-scrobbled and every episode under it destroyed.
    knows_shows = all(len(row) > 4 for row in (snapshot.state or []))
    wanted = WatchState(
        items={
            int(row[0]): ItemState(
                rating_key=int(row[0]),
                media_type=str(row[3]),
                view_count=int(row[1]),
                view_offset_ms=int(row[2]),
                show_rating_key=int(row[4]) if len(row) > 4 and row[4] else None,
            )
            for row in (snapshot.state or [])
        }
    )
    sections = _section_pairs(plex)
    current = plex.read_watch_state(sections, target_token)

    plan = build_plan(wanted, current)
    report.planned = len(plan)
    counts = summarise(plan)
    report.marks = counts[OpKind.MARK.value]
    report.unmarks = counts[OpKind.UNMARK.value]
    report.offsets_set = counts[OpKind.SET_OFFSET.value]
    report.offsets_cleared = counts[OpKind.CLEAR_OFFSET.value]

    report.removals_preview = removals_by_title(plan, limit=REMOVAL_PREVIEW)

    if not dry_run and plan:
        # Its own snapshot, before its own first write. Restoring is a mirror in the other direction,
        # so it removes whatever the snapshot lacks — most importantly anything watched on the account
        # since the transfer. Without this, pressing Undo was the one destructive act in the feature
        # with nothing behind it.
        report.snapshot_id = take_snapshot(sessions, snapshot.user_id, current, job_id)

    for op in plan:
        try:
            if plex.apply_watch_op(op, target_token, dry_run=dry_run):
                report.applied += 1
            else:
                report.unreachable += 1
        except Exception as e:
            report.failed += 1
            if len(report.errors) < 10:
                report.errors.append(f"{op.rating_key}: {type(e).__name__}")

    # Same show-level residue the transfer has to clear: un-scrobbling an episode leaves its show
    # flagged, and the restore is the path that empties whole shows.
    #
    # ONLY when the snapshot knows its show keys. `_clear_emptied_shows` decides what to spare from
    # `still_wanted`, which it reads off the source — and a 4-element snapshot yields
    # `show_rating_key=None` for every item, so `still_wanted` comes out EMPTY while `emptied` (built
    # from the live read) carries real show keys. It would then un-scrobble a show the snapshot
    # explicitly asked to keep, and un-scrobbling a show key clears every episode under it: the
    # restore would destroy the very watches it was restoring, and report success.
    #
    # Backfilling the keys from the live read is not a fix either — a snapshot item that needs a MARK
    # is by definition absent from the live read, so its show would still be cleared right after the
    # MARK landed.
    if knows_shows:
        # Dry-run aware, not skipped: the transfer reports its show clears in a preview for the same
        # reason — a preview that omits them understates what the real run does.
        _clear_emptied_shows(plex, plan, wanted, target_token, report, dry_run=dry_run)

    # ONLY when the restore actually landed. Stamping unconditionally consumed the one recovery
    # record rule 2 exists to preserve: a PMS that 500s mid-undo — or a library un-shared since the
    # copy, which makes every write return False with no exception at all — left `applied=0` while
    # `restored_at` was set, the snapshot dropped out of `/snapshots`, and a retry answered "already
    # been undone". The handler re-plans from a fresh read, so leaving it pending is safe and a retry
    # writes only what is still missing.
    landed = not dry_run and report.unreachable == 0 and report.failed == 0 and not report.errors
    if not dry_run and not landed:
        # INSERTED first, not appended. The UI renders `errors[0]`, and the per-op entries added
        # during the write loop are of the form "12345: ReadTimeout" — so the most likely real
        # failure reached the screen as a rating key and an exception class, which the frontend rules
        # forbid ("errors say what went wrong and how to fix it — never raw error codes"). The
        # per-op detail stays behind it for the audit row.
        report.errors.insert(0, "the restore did not complete, so this transfer can still be undone")
    if landed:
        snapshot.restored_at = utcnow()
        # The copied play events describe watches that are no longer represented on the account, so
        # they go with the restore. Only ours — `source='transfer'` — never Plex's own rows.
        #
        # Their rating keys are read FIRST, because they are also the scope of the cache cleanup
        # below: they name exactly the titles this transfer put on the account.
        copied = session.query(WatchEvent).filter(
            WatchEvent.plex_account_id == _account_for(session, snapshot.user_id),
            WatchEvent.source == "transfer",
        )
        copied_keys = {key for (key,) in copied.with_entities(WatchEvent.rating_key).all() if key is not None}
        report.events_copied = -copied.delete(synchronize_session=False)
        # And the cached rows beside them. `watch_cache` EXEMPTS rows carrying a `source_viewed_at`
        # from every deletion path — right while the transfer stands, wrong the moment it is undone.
        # Left behind, they can never self-heal: Plex reports the account as no longer having watched
        # the title, the cache keeps it anyway, and the engine's already-watched filter suppresses it
        # for ever. On the one account this feature exists to set up, while the UI says "Put back
        # exactly as it was."
        #
        # DELETED here rather than merely un-stamped and left for the periodic sweep. This is the
        # undo's own mess and it should clear it up itself: the sweep only runs on the
        # `sync.watch_full_days` cadence, only when the read can prove it saw the whole library, and
        # NEVER on a PMS that does not report `totalSize` — so relying on it left the rows in place
        # indefinitely on exactly the servers least able to recover. Doing it here also frees
        # `sync_section` to refuse an empty answer, which is the shape that erases a whole section.
        #
        # SCOPED to the rating keys this transfer actually copied, never "every stamped row".
        # `stamp_true_dates` matches on rating key alone, so it also stamps a title the account had
        # watched ITSELF before the transfer — and an unscoped delete took those too. The re-read
        # that was supposed to heal them is not guaranteed: a library the account is no longer shared
        # raises `SectionNotShared` and is deliberately skipped with its rows kept, and a managed
        # account whose token cannot be minted is never refilled at all. On those two shapes an
        # over-delete is permanent, not a one-cycle blip.
        report.titles_cached = -(
            session.query(WatchedTitle)
            .filter(
                WatchedTitle.user_id == snapshot.user_id,
                WatchedTitle.source_viewed_at.isnot(None),
                WatchedTitle.rating_key.in_(copied_keys) if copied_keys else sa_false(),
            )
            .delete(synchronize_session=False)
        )

    logger.info(
        "watch replication undo from snapshot {}: {} planned, {} applied, {} unreachable (dry_run={})",
        snapshot_id,
        report.planned,
        report.applied,
        report.unreachable,
        dry_run,
    )
    return report


def _account_for(session: Session, user_id: int) -> int:
    user = session.get(User, user_id)
    return user.plex_account_id if user else 0
