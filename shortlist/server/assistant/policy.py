"""Authorization contracts for transaction-owned assistant changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.server.assistant_auth import Capability, GrantContext, ResourceSelection, require_authorized
from shortlist.server.db.models import Server


class ChangeError(ValueError):
    """A stable, safe error suitable for the assistant tool boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Principal(Protocol):
    grant_id: str
    owner_account_id: int
    client_id: str
    revision: int


@dataclass(frozen=True)
class AccessRequirements:
    """The union of capabilities and resolved resources a domain operation needs."""

    capabilities: tuple[str, ...] = ()
    row_ids: tuple[int, ...] = ()
    person_ids: tuple[int, ...] = ()
    library_keys: tuple[str, ...] = ()
    setting_groups: tuple[str, ...] = ()
    destination_ids: tuple[str, ...] = ()
    dynamic_rows: bool = False
    dynamic_audience: bool = False
    dynamic_libraries: bool = False
    batch_size: int | None = None
    work_units: int | None = None
    provider_calls: int | None = None
    requires_approval: bool = False

    def selection(self) -> ResourceSelection:
        """Translate the exact domain targets into the shared auth policy's selection."""
        return ResourceSelection(
            row_ids=frozenset(self.row_ids),
            person_ids=frozenset(self.person_ids),
            library_keys=frozenset(self.library_keys),
            setting_groups=frozenset(self.setting_groups),
            destination_ids=frozenset(self.destination_ids),
            dynamic_rows=self.dynamic_rows,
            dynamic_audience=self.dynamic_audience,
            dynamic_libraries=self.dynamic_libraries,
            batch_size=self.batch_size,
            work_units=self.work_units,
            provider_calls=self.provider_calls,
        )


class AuthorizationPolicy(Protocol):
    """Identity checks run in the mutation transaction; effect checks cannot widen identity."""

    def current_grant(self, session: Session, principal: Principal, now: datetime) -> GrantContext: ...

    def authorize(self, grant: GrantContext, requirements: AccessRequirements) -> None: ...


class AuthGrantPolicy:
    """Use the same current-grant and resource policy as assistant authentication."""

    def current_grant(self, session: Session, principal: Principal, now: datetime) -> GrantContext:
        from shortlist.server.assistant_auth.repository import require_current_grant_in_session

        try:
            owner_id = session.scalar(select(Server.owner_account_id).limit(1))
            if owner_id is None:
                raise ChangeError("missing_permission", "The installation must have a verified owner.")
            return require_current_grant_in_session(session, principal, now=now, current_owner_account_id=owner_id)
        except PermissionError as exc:
            raise ChangeError("missing_permission", "This assistant connection is no longer authorized.") from exc

    def authorize(self, grant: GrantContext, requirements: AccessRequirements) -> None:
        try:
            require_authorized(
                grant, (Capability(value) for value in requirements.capabilities), requirements.selection()
            )
        except (PermissionError, ValueError) as exc:
            raise ChangeError("missing_permission", "The connection does not authorize these effects.") from exc
