"""Resolve PMS's local owner alias without guessing from display names or row settings."""

from sqlalchemy.orm import Session

from shortlist.server.db.models import Server


def verified_owner_account_id(session: Session, plex) -> int | None:
    """Canonical owner only for the actual authenticated PMS linked during verified setup."""
    machine_id = plex.machine_id
    if not isinstance(machine_id, str) or not machine_id:
        return None
    server = session.query(Server).filter(Server.machine_id == machine_id).one_or_none()
    if server is None:
        return None
    # Setup verified ownership through plex.tv. The client's machine ID comes from the
    # authenticated PMS response, independently of this stored ownership record.
    owner = server.owner_account_id
    return owner if type(owner) is int and owner > 0 else None
