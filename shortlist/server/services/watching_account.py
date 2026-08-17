"""Move the owner's watching onto a separate Plex account.

**The problem this exists for.** Plex hides a per-person row from everyone else through the SHARE
that person has with the server. The owner has no share with themselves, so nothing can hide
anything from them: the Recommended shelf inside every library shows them all N rows. Plex offers no
setting for this, and it is by a wide margin the most-asked question about Shortlist.

The only real fix is to stop watching as the admin account. This module makes that cheap: find (or
create) a Home user, copy the owner's watched set onto it so its picks are right from the first run,
and optionally mark those titles played on the PMS so the new account looks lived-in.

**The date problem, and why `source_viewed_at` exists.** Plex has no way to backdate a watch —
`/:/scrobble` is stamped `now`. Copying 2,000 titles onto a new account therefore tells the PMS they
were all watched today, and the next watch sync writes that into `watched_titles.viewed_at`. Since
seeds are taken from the most RECENT watches, a set sharing one timestamp orders arbitrarily and the
new account's recommendations become noise. So the copy records the true date in `source_viewed_at`,
which the sync neither overwrites nor sweeps away (a non-scrobbled transfer leaves rows Plex has
never heard of, and a blind full-read replace would delete every one of them), and which every
"how recently?" read prefers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy.orm import Session

from shortlist.engine.models import UserType
from shortlist.server.db.models import User, WatchedTitle, utcnow


@dataclass
class TransferReport:
    """What a transfer did, or would do under `dry_run`. Every field is auditable (rule 10)."""

    copied: int = 0
    already_present: int = 0
    scrobbled: int = 0
    scrobble_skipped: int = 0
    dry_run: bool = False
    #: Shortlist has never cached a single watched title for the SOURCE, so there was nothing this
    #: transfer could ever have copied. Reported apart from `copied == 0` because the two are
    #: opposites — "they already had everything" is success, this is the copy being impossible —
    #: and collapsing them into one bare 0 is what made the setup wizard silently useless (#88).
    source_empty: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "copied": self.copied,
            "already_present": self.already_present,
            "scrobbled": self.scrobbled,
            "scrobble_skipped": self.scrobble_skipped,
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


def transfer_watch_history(
    session: Session,
    *,
    from_user_id: int,
    to_user_id: int,
    plex=None,
    target_token: str = "",
    scrobble: bool = False,
    dry_run: bool = False,
) -> TransferReport:
    """Copy one person's watched set onto another, preserving the TRUE watch dates.

    The copy into Shortlist is the part that makes recommendations correct, and it writes nothing to
    Plex. `scrobble=True` additionally marks each title played on the PMS as the target account, so
    Plex shows checkmarks — an all-or-nothing extra that costs one PMS write per title and cannot
    carry the original date (see the module docstring).

    Args:
        session: An open session; the caller owns the transaction.
        from_user_id: Whose history to copy (normally the owner).
        to_user_id: The watching account receiving it.
        plex: A `PlexClient`, required only when `scrobble` is set.
        target_token: The target account's server token, required only when `scrobble` is set.
        scrobble: Also mark the titles played in Plex for the target account.
        dry_run: Count everything, write nothing.

    Returns:
        A `TransferReport`.

    Raises:
        LookupError: Either user is unknown.
        ValueError: `scrobble` was asked for without a client and token to do it with.
    """
    source = session.get(User, from_user_id)
    target = session.get(User, to_user_id)
    if source is None or target is None:
        raise LookupError("both the source and the target must be known users")
    if from_user_id == to_user_id:
        raise ValueError("cannot transfer a watch history onto the same account")
    # A WATCHING account is one of the owner's own Home profiles. Copying their history onto a
    # SHARED user would build that person's Picked-for-You from the owner's taste and exclude titles
    # they have never seen — one person's data silently wearing another's name. The UI only ever
    # offers Home users; this is the same rule at the layer that actually writes.
    if UserType(target.user_type) is not UserType.MANAGED:
        raise ValueError(
            "a watching account must be one of your own Plex Home users — "
            f"{target.username!r} is a {target.user_type} account"
        )
    if scrobble and (plex is None or not target_token):
        raise ValueError("marking titles played in Plex needs a Plex client and the target's token")

    report = TransferReport(dry_run=dry_run)
    existing = {
        (row.section_key, row.rating_key)
        for row in session.query(WatchedTitle.section_key, WatchedTitle.rating_key).filter(
            WatchedTitle.user_id == to_user_id
        )
    }
    rows = session.query(WatchedTitle).filter(WatchedTitle.user_id == from_user_id).all()
    report.source_empty = not rows

    for row in rows:
        already = (row.section_key, row.rating_key) in existing
        if already:
            # Already theirs — from their own watching, or from a re-run of this transfer. Leave the
            # ROW alone (their own viewing is more truthful than a copy of somebody else's), but do
            # NOT skip the scrobble below: a transfer interrupted halfway would otherwise leave those
            # titles copied-but-unmarked for ever, with a re-run counting them as done (rule 6).
            report.already_present += 1
        elif not dry_run:
            session.add(
                WatchedTitle(
                    user_id=to_user_id,
                    section_key=row.section_key,
                    rating_key=row.rating_key,
                    tmdb_id=row.tmdb_id,
                    media_type=row.media_type,
                    title=row.title,
                    year=row.year,
                    watch_count=row.watch_count,
                    viewed_leaf_count=row.viewed_leaf_count,
                    leaf_count=row.leaf_count,
                    # `viewed_at` is what Plex will report after a scrobble (today). The real date
                    # goes in `source_viewed_at`, which the sync never overwrites and every recency
                    # read prefers — without this the transfer would flatten their taste signal.
                    viewed_at=utcnow() if scrobble else (row.source_viewed_at or row.viewed_at),
                    source_viewed_at=row.source_viewed_at or row.viewed_at,
                )
            )
        if not already:
            report.copied += 1

        if scrobble and row.rating_key > 0:
            try:
                if plex.scrobble_as(row.rating_key, target_token, dry_run=dry_run):
                    report.scrobbled += 1
                else:
                    report.scrobble_skipped += 1
            except Exception as e:
                # One unreachable title must not abandon the other two thousand. The copy into
                # Shortlist has already happened, so recommendations are correct either way — a
                # missing checkmark is cosmetic.
                report.scrobble_skipped += 1
                if len(report.errors) < 10:
                    report.errors.append(f"{row.title}: {type(e).__name__}")

    logger.info(
        "watch history transfer {} -> {}: {} copied, {} already there, {} scrobbled, {} skipped (dry_run={})",
        source.username,
        target.username,
        report.copied,
        report.already_present,
        report.scrobbled,
        report.scrobble_skipped,
        dry_run,
    )
    return report
