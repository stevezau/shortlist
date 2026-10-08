"""SQLAlchemy persistence for assistant grants and opaque credentials."""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from shortlist.server.db.models import Event

from .credentials import CredentialHasher, IssuedSecret, as_utc, credential_is_active
from .destinations import DestinationSelection, configured_destinations, validate_selected_destinations
from .models import (
    AssistantConsentFlow,
    AssistantGrant,
    AssistantLocalCredential,
    AssistantOAuthClient,
    AssistantOAuthCode,
    AssistantOAuthRevokedFamily,
    AssistantOAuthToken,
)
from .policy import AuthorizationDenied, basic_role_capabilities, capabilities_for_preset, owner_managed_capabilities
from .types import (
    ASSISTANT_CAPABILITIES,
    Capability,
    GrantConstraints,
    GrantContext,
    GrantPreset,
    GrantSummary,
    StoredGrantIdentity,
    VerifiedOAuthToken,
)


class OAuthTokenIssuanceDenied(ValueError):
    """A grant or token family was revoked while issuance was in flight."""


class GrantUpdateNotFound(LookupError):
    """The requested grant does not belong to the browser owner."""


class GrantUpdateConflict(RuntimeError):
    """The requested grant is inactive or changed since it was displayed."""


class GrantRemovalConflict(ValueError):
    """A connection must be revoked before its record can be removed."""


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(UTC)


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _grant_context(row: AssistantGrant) -> GrantContext:
    constraints = GrantConstraints.from_dict(row.constraints)
    capabilities = frozenset(Capability(value) for value in row.capabilities)
    if constraints.basic_access_v1 is not None:
        capabilities &= frozenset(basic_role_capabilities(constraints.basic_access_v1))
    return GrantContext(
        grant_id=row.id,
        owner_account_id=row.owner_account_id,
        client_id=row.client_id,
        name=row.name,
        preset=GrantPreset(row.preset),
        capabilities=capabilities,
        constraints=constraints,
        revision=row.revision,
        expires_at=as_utc(row.expires_at).astimezone(UTC) if row.expires_at is not None else None,
    )


def _effective_grant_context(session: Session, row: AssistantGrant) -> GrantContext:
    """Resolve the reviewed profile against this transaction's configured services."""
    context = _grant_context(row)
    if not context.constraints.owner_managed:
        return context
    from shortlist.server.catalogs.settings import get_settings_catalog

    constraints = replace(
        context.constraints,
        row_ids=frozenset(),
        library_keys=frozenset(),
        setting_groups=frozenset(setting.group.value for setting in get_settings_catalog()),
        destination_ids=frozenset(choice.destination_id for choice in configured_destinations(session)),
        include_future_rows=True,
        include_future_people=True,
        include_future_libraries=True,
        max_batch_size=None,
        max_work_per_operation=None,
    )
    return replace(context, constraints=constraints)


def require_current_grant_in_session(
    session: Session,
    principal: GrantContext | StoredGrantIdentity,
    *,
    now: datetime | None = None,
    check_revision: bool = True,
    current_owner_account_id: int | None = None,
) -> GrantContext:
    """Reload a principal within the caller's transaction and fail if stale.

    This is the apply-time identity check. Supplemental approval can add authority
    for an exact plan, but cannot bypass identity, owner, client, revocation,
    expiry, or grant-revision checks.
    """
    row = session.get(AssistantGrant, principal.grant_id)
    current = _now(now)
    if row is None or row.revoked_at is not None:
        raise AuthorizationDenied("assistant grant is revoked or missing")
    if row.expires_at is not None and as_utc(row.expires_at) <= current:
        raise AuthorizationDenied("assistant grant has expired")
    if row.owner_account_id != principal.owner_account_id or row.client_id != principal.client_id:
        raise AuthorizationDenied("assistant grant identity changed")
    if current_owner_account_id is not None and row.owner_account_id != current_owner_account_id:
        raise AuthorizationDenied("assistant grant belongs to a previous installation owner")
    if check_revision and row.revision != principal.revision:
        raise AuthorizationDenied("assistant grant changed; re-authorize the operation")
    current_context = _effective_grant_context(session, row)
    if isinstance(principal, GrantContext):
        return replace(current_context, capabilities=current_context.capabilities & principal.capabilities)
    if isinstance(principal, StoredGrantIdentity):
        return current_context
    raise TypeError("principal must be a GrantContext or StoredGrantIdentity")


def grant_created_rows_in_session(
    session: Session,
    principal: GrantContext,
    row_ids: Iterable[int],
    *,
    now: datetime | None = None,
    approved_creation: bool = False,
) -> GrantContext:
    """Attach server-created rows to a bounded grant in the apply transaction.

    The operation service supplies IDs returned by its trusted row adapter. A
    client cannot claim an existing row by placing its ID in an input payload.
    """
    created = frozenset(row_ids)
    if not created:
        return require_current_grant_in_session(session, principal, now=now)
    if any(not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0 for row_id in created):
        raise ValueError("created row IDs must be positive integers")
    current = require_current_grant_in_session(session, principal, now=now)
    if Capability.ROWS_CREATE not in current.capabilities and not approved_creation:
        raise AuthorizationDenied("missing permission: rows.create")
    if current.constraints.owner_managed:
        # Future rows are already covered. Avoid persisting a temporary catalog
        # snapshot back into the durable profile marker while attaching them.
        return current
    row = session.get(AssistantGrant, principal.grant_id)
    constraints = current.constraints
    updated_constraints = GrantConstraints(
        row_ids=constraints.row_ids | created,
        person_ids=constraints.person_ids,
        library_keys=constraints.library_keys,
        setting_groups=constraints.setting_groups,
        destination_ids=constraints.destination_ids,
        include_future_rows=constraints.include_future_rows,
        include_future_people=constraints.include_future_people,
        include_future_libraries=constraints.include_future_libraries,
        max_batch_size=constraints.max_batch_size,
        max_work_per_operation=constraints.max_work_per_operation,
        max_provider_calls=constraints.max_provider_calls,
        owner_managed=constraints.owner_managed,
    )
    row.constraints = updated_constraints.as_dict()
    row.revision += 1
    row.updated_at = _now(now)
    session.flush()
    updated = _effective_grant_context(session, row)
    # Resource ownership can grow after creation without widening an OAuth token's scopes.
    return replace(updated, capabilities=updated.capabilities & current.capabilities)


class AssistantAuthRepository:
    """Persist assistant identity without storing bearer credential bodies."""

    def __init__(self, sessions: sessionmaker, hasher: CredentialHasher) -> None:
        self.sessions = sessions
        self.hasher = hasher

    def create_grant(
        self,
        *,
        owner_account_id: int,
        client_id: str,
        name: str,
        preset: GrantPreset,
        constraints: GrantConstraints,
        capabilities: Iterable[Capability] | None = None,
        selected_destinations: list[DestinationSelection] | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        session: Session | None = None,
    ) -> GrantContext:
        """Create a named grant; callers must already have browser owner consent."""
        values = set(capabilities_for_preset(preset) if capabilities is None else capabilities)
        if constraints.basic_access_v1 is not None:
            profile = basic_role_capabilities(constraints.basic_access_v1)
            values = profile if capabilities is None else profile & values
            constraints = replace(constraints, owner_managed=True, max_provider_calls=0)
        if constraints.owner_managed:
            if constraints.basic_access_v1 is None:
                profile = owner_managed_capabilities(paid=constraints.max_provider_calls > 0)
                values = profile if capabilities is None else profile & values
            constraints = replace(
                constraints,
                row_ids=frozenset(),
                person_ids=frozenset(),
                library_keys=frozenset(),
                setting_groups=frozenset(),
                destination_ids=frozenset(),
                include_future_rows=True,
                include_future_people=True,
                include_future_libraries=True,
                max_batch_size=None,
                max_work_per_operation=None,
            )
        if not ASSISTANT_CAPABILITIES.issuperset(values):
            raise AuthorizationDenied("owner secrets and grant administration cannot be delegated")
        timestamp = _now(now)
        row = AssistantGrant(
            id=_opaque_id("grt"),
            owner_account_id=owner_account_id,
            client_id=client_id,
            name=name,
            preset=preset.value,
            capabilities=sorted(capability.value for capability in values),
            constraints=constraints.as_dict(),
            revision=1,
            created_at=timestamp,
            updated_at=timestamp,
            expires_at=as_utc(expires_at).astimezone(UTC) if expires_at is not None else None,
        )
        if session is not None:
            validate_selected_destinations(session, selected_destinations or [], set(constraints.destination_ids))
            session.add(row)
            session.flush()
            return _effective_grant_context(session, row)
        with self.sessions() as owned:
            validate_selected_destinations(owned, selected_destinations or [], set(constraints.destination_ids))
            owned.add(row)
            owned.commit()
            return _effective_grant_context(owned, row)

    def get_grant_context(
        self,
        grant_id: str,
        *,
        now: datetime | None = None,
        session: Session | None = None,
    ) -> GrantContext | None:
        """Load an active grant."""
        if session is not None:
            return self._active_context(session, session.get(AssistantGrant, grant_id), _now(now))
        with self.sessions() as owned:
            return self._active_context(owned, owned.get(AssistantGrant, grant_id), _now(now))

    def require_current_grant_in_session(
        self,
        session: Session,
        principal: GrantContext | StoredGrantIdentity,
        *,
        now: datetime | None = None,
        check_revision: bool = True,
        current_owner_account_id: int | None = None,
    ) -> GrantContext:
        """Repository-bound form of :func:`require_current_grant_in_session`."""
        return require_current_grant_in_session(
            session,
            principal,
            now=now,
            check_revision=check_revision,
            current_owner_account_id=current_owner_account_id,
        )

    def grant_created_rows_in_session(
        self,
        session: Session,
        principal: GrantContext,
        row_ids: Iterable[int],
        *,
        now: datetime | None = None,
        approved_creation: bool = False,
    ) -> GrantContext:
        """Repository-bound form of :func:`grant_created_rows_in_session`."""
        return grant_created_rows_in_session(
            session,
            principal,
            row_ids,
            now=now,
            approved_creation=approved_creation,
        )

    def find_grant_for_client(
        self,
        client_id: str,
        *,
        owner_account_id: int | None = None,
        now: datetime | None = None,
    ) -> GrantContext | None:
        """Find the newest active grant for an OAuth client."""
        with self.sessions() as session:
            statement = select(AssistantGrant).where(AssistantGrant.client_id == client_id)
            if owner_account_id is not None:
                statement = statement.where(AssistantGrant.owner_account_id == owner_account_id)
            rows = session.scalars(statement.order_by(AssistantGrant.created_at.desc())).all()
            for row in rows:
                if context := self._active_context(session, row, _now(now)):
                    return context
        return None

    def list_grants(self, owner_account_id: int, *, include_revoked: bool = False) -> list[GrantContext]:
        """List an owner's named integrations without credential material."""
        with self.sessions() as session:
            statement = select(AssistantGrant).where(AssistantGrant.owner_account_id == owner_account_id)
            if not include_revoked:
                statement = statement.where(AssistantGrant.revoked_at.is_(None))
            return [_grant_context(row) for row in session.scalars(statement.order_by(AssistantGrant.created_at))]

    def list_grant_summaries(self, owner_account_id: int) -> list[GrantSummary]:
        """List owner-visible status without returning credential bodies or digests."""
        from shortlist.server.assistant.budgets import AssistantBudget

        with self.sessions() as session:
            counts = (
                select(AssistantLocalCredential.grant_id, func.count(AssistantLocalCredential.id).label("count"))
                .where(AssistantLocalCredential.revoked_at.is_(None))
                .group_by(AssistantLocalCredential.grant_id)
                .subquery()
            )
            statement = (
                select(
                    AssistantGrant,
                    func.coalesce(counts.c.count, 0),
                    func.coalesce(AssistantBudget.provider_calls_reserved, 0),
                )
                .outerjoin(counts, counts.c.grant_id == AssistantGrant.id)
                .outerjoin(AssistantBudget, AssistantBudget.grant_id == AssistantGrant.id)
                .where(AssistantGrant.owner_account_id == owner_account_id)
                .order_by(AssistantGrant.created_at.desc())
            )
            return [
                GrantSummary(
                    context=_grant_context(row),
                    created_at=as_utc(row.created_at),
                    updated_at=as_utc(row.updated_at),
                    revoked_at=as_utc(row.revoked_at) if row.revoked_at else None,
                    last_used_at=as_utc(row.last_used_at) if row.last_used_at else None,
                    local_credential_count=count,
                    provider_calls_reserved=reserved,
                )
                for row, count, reserved in session.execute(statement)
            ]

    def replace_grant_authority(
        self,
        grant_id: str,
        *,
        capabilities: Iterable[Capability],
        constraints: GrantConstraints,
        expected_revision: int,
        now: datetime | None = None,
    ) -> GrantContext:
        """Replace authority using a revision compare-and-swap."""
        timestamp = _now(now)
        values = sorted({capability.value for capability in capabilities})
        if not ASSISTANT_CAPABILITIES.issuperset(Capability(value) for value in values):
            raise AuthorizationDenied("owner secrets and grant administration cannot be delegated")
        if constraints.basic_access_v1 is not None and not basic_role_capabilities(
            constraints.basic_access_v1
        ).issuperset(Capability(value) for value in values):
            raise AuthorizationDenied("capabilities exceed the selected access role")
        with self.sessions() as session:
            current = session.get(AssistantGrant, grant_id)
            if current is not None and GrantConstraints.from_dict(current.constraints).requires_access_approval:
                raise AuthorizationDenied("owner approval is required before replacing this legacy grant")
            changed = session.execute(
                update(AssistantGrant)
                .where(
                    AssistantGrant.id == grant_id,
                    AssistantGrant.revision == expected_revision,
                    AssistantGrant.revoked_at.is_(None),
                )
                .values(
                    capabilities=values,
                    constraints=constraints.as_dict(),
                    revision=AssistantGrant.revision + 1,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise AuthorizationDenied("assistant grant changed or was revoked")
            session.commit()
            return _grant_context(session.get(AssistantGrant, grant_id))

    def patch_grant_constraints(
        self,
        grant_id: str,
        *,
        owner_account_id: int,
        expected_revision: int,
        constraints_patch: dict[str, object],
        capabilities: Iterable[Capability] | None = None,
        selected_destinations: list[DestinationSelection] | None = None,
        now: datetime | None = None,
    ) -> GrantContext:
        """Merge explicit constraint fields through an owner-scoped revision CAS.

        The audit event is committed with the grant update, so a failed audit cannot
        leave a changed authority without its durable record.
        """
        if expected_revision <= 0:
            raise ValueError("expected revision must be positive")
        known = {
            "row_ids",
            "library_keys",
            "setting_groups",
            "destination_ids",
            "include_future_rows",
            "include_future_libraries",
            "max_batch_size",
            "max_work_per_operation",
            "max_provider_calls",
        }
        unknown = set(constraints_patch) - known
        if unknown:
            raise ValueError(f"unknown assistant grant constraints: {', '.join(sorted(unknown))}")
        capability_values = None if capabilities is None else set(capabilities)
        if capability_values is not None and not ASSISTANT_CAPABILITIES.issuperset(capability_values):
            raise ValueError("owner secrets and grant administration cannot be delegated")

        timestamp = _now(now)
        with self.sessions() as session:
            row = session.get(AssistantGrant, grant_id)
            if row is None or row.owner_account_id != owner_account_id:
                raise GrantUpdateNotFound("assistant grant not found")
            if self._active_context(session, row, timestamp) is None or row.revision != expected_revision:
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")

            current_constraints = GrantConstraints.from_dict(row.constraints)
            if current_constraints.basic_access_v1 is not None:
                raise GrantUpdateConflict("change this connection's access role instead")
            if current_constraints.requires_access_approval:
                raise GrantUpdateConflict("owner approval is required before changing this legacy grant")
            merged_constraints = current_constraints.as_dict()
            merged_constraints.update(constraints_patch)
            updated_constraints = GrantConstraints.from_dict(merged_constraints)
            validate_selected_destinations(
                session,
                selected_destinations or [],
                set(updated_constraints.destination_ids) - set(current_constraints.destination_ids),
            )
            changed_fields = sorted(
                field
                for field in constraints_patch
                if getattr(current_constraints, field) != getattr(updated_constraints, field)
            )
            if capability_values is not None and capability_values != set(_grant_context(row).capabilities):
                changed_fields.append("capabilities")
            changed = session.execute(
                update(AssistantGrant)
                .execution_options(synchronize_session=False)
                .where(
                    AssistantGrant.id == grant_id,
                    AssistantGrant.owner_account_id == owner_account_id,
                    AssistantGrant.revision == expected_revision,
                    AssistantGrant.revoked_at.is_(None),
                    or_(AssistantGrant.expires_at.is_(None), AssistantGrant.expires_at > timestamp),
                )
                .values(
                    capabilities=(
                        sorted(capability.value for capability in capability_values)
                        if capability_values is not None
                        else row.capabilities
                    ),
                    constraints=updated_constraints.as_dict(),
                    revision=AssistantGrant.revision + 1,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            session.add(
                Event(
                    scope="assistant.grant.update",
                    level="info",
                    message={
                        "grant_id": grant_id,
                        "actor": {"via": "browser", "account_id": owner_account_id},
                        "changed_fields": changed_fields,
                        "revision": expected_revision + 1,
                    },
                )
            )
            session.commit()
            session.refresh(row)
            return _effective_grant_context(session, row)

    def approve_updated_access(
        self,
        grant_id: str,
        *,
        owner_account_id: int,
        expected_revision: int,
        now: datetime | None = None,
    ) -> GrantContext:
        """Atomically convert one legacy grant to the all-people access model."""
        timestamp = _now(now)
        with self.sessions() as session:
            row = session.get(AssistantGrant, grant_id)
            if row is None or row.owner_account_id != owner_account_id:
                raise GrantUpdateNotFound("assistant grant not found")
            if self._active_context(session, row, timestamp) is None or row.revision != expected_revision:
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            current = GrantConstraints.from_dict(row.constraints)
            if not current.requires_access_approval:
                raise GrantUpdateConflict("assistant grant already has updated access")
            updated = GrantConstraints(
                row_ids=frozenset(),
                library_keys=frozenset(),
                setting_groups=current.setting_groups,
                destination_ids=current.destination_ids,
                include_future_rows=True,
                include_future_libraries=True,
                max_batch_size=current.max_batch_size,
                max_work_per_operation=None,
                max_provider_calls=current.max_provider_calls,
                include_future_people=True,
            )
            changed = session.execute(
                update(AssistantGrant)
                .execution_options(synchronize_session=False)
                .where(
                    AssistantGrant.id == grant_id,
                    AssistantGrant.owner_account_id == owner_account_id,
                    AssistantGrant.revision == expected_revision,
                    AssistantGrant.revoked_at.is_(None),
                    or_(AssistantGrant.expires_at.is_(None), AssistantGrant.expires_at > timestamp),
                )
                .values(
                    constraints=updated.as_dict(),
                    revision=AssistantGrant.revision + 1,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            session.add(
                Event(
                    scope="assistant.grant.approved_updated_access",
                    level="info",
                    message={
                        "grant_id": grant_id,
                        "actor": {"via": "browser", "account_id": owner_account_id},
                        "revision": expected_revision + 1,
                    },
                )
            )
            session.commit()
            session.refresh(row)
            return _effective_grant_context(session, row)

    def update_owner_managed(
        self,
        grant_id: str,
        *,
        owner_account_id: int,
        expected_revision: int,
        upgrade: bool = False,
        paid_enabled: bool | None = None,
        max_provider_calls: int | None = None,
        now: datetime | None = None,
    ) -> GrantContext:
        """Explicit owner profile upgrade or paid edit, atomically guarded by revision."""
        from shortlist.server.assistant.budgets import AssistantBudget

        timestamp = _now(now)
        if expected_revision <= 0 or (paid_enabled is None and max_provider_calls is not None):
            raise ValueError("invalid owner-managed update")
        if paid_enabled is True and (max_provider_calls is None or not 1 <= max_provider_calls <= 100):
            raise ValueError("paid calls require a finite allowance from 1 to 100")
        if paid_enabled is False and max_provider_calls not in (None, 0):
            raise ValueError("turning paid calls off requires a zero allowance")
        with self.sessions() as session:
            row = session.get(AssistantGrant, grant_id)
            if row is None or row.owner_account_id != owner_account_id:
                raise GrantUpdateNotFound("assistant grant not found")
            if self._active_context(session, row, timestamp) is None or row.revision != expected_revision:
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            current = GrantConstraints.from_dict(row.constraints)
            if current.basic_access_v1 is not None:
                raise GrantUpdateConflict("change this connection's access role instead")
            if not upgrade and not current.owner_managed:
                raise GrantUpdateConflict("upgrade this connection before changing its paid access")
            if (
                upgrade
                and current.owner_managed
                and owner_managed_capabilities().issubset(Capability(value) for value in row.capabilities)
            ):
                raise GrantUpdateConflict("connection already has full Shortlist access")
            reserved_row = session.get(AssistantBudget, grant_id)
            reserved = reserved_row.provider_calls_reserved if reserved_row else 0
            if paid_enabled and max_provider_calls is not None and max_provider_calls <= reserved:
                raise ValueError("paid allowance must exceed used or uncertain calls")

            values = set(Capability(value) for value in row.capabilities)
            if upgrade:
                paid_before = Capability.AI_GENERATE in values
                values = owner_managed_capabilities(paid=paid_before)
                current = replace(
                    current,
                    row_ids=frozenset(),
                    person_ids=frozenset(),
                    library_keys=frozenset(),
                    include_future_rows=True,
                    include_future_people=True,
                    include_future_libraries=True,
                    max_batch_size=None,
                    max_work_per_operation=None,
                    owner_managed=True,
                )
            if paid_enabled is not None:
                if paid_enabled:
                    values.add(Capability.AI_GENERATE)
                    current = replace(current, max_provider_calls=max_provider_calls)
                else:
                    values.discard(Capability.AI_GENERATE)
                    current = replace(current, max_provider_calls=0)
            changed = session.execute(
                update(AssistantGrant)
                .execution_options(synchronize_session=False)
                .where(
                    AssistantGrant.id == grant_id,
                    AssistantGrant.owner_account_id == owner_account_id,
                    AssistantGrant.revision == expected_revision,
                    AssistantGrant.revoked_at.is_(None),
                    or_(AssistantGrant.expires_at.is_(None), AssistantGrant.expires_at > timestamp),
                )
                .values(
                    capabilities=sorted(capability.value for capability in values),
                    constraints=current.as_dict(),
                    revision=AssistantGrant.revision + 1,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            session.add(
                Event(
                    scope="assistant.grant.owner_managed",
                    level="info",
                    message={
                        "grant_id": grant_id,
                        "actor": {"via": "browser", "account_id": owner_account_id},
                        "changed_fields": ["owner_managed"] if upgrade else ["paid_services"],
                        "revision": expected_revision + 1,
                    },
                )
            )
            session.commit()
            session.refresh(row)
            return _effective_grant_context(session, row)

    def set_basic_access(
        self,
        grant_id: str,
        *,
        owner_account_id: int,
        expected_revision: int,
        access_role: str,
        now: datetime | None = None,
    ) -> GrantContext:
        """Apply one explicit owner role decision without changing historical paid usage."""
        values = basic_role_capabilities(access_role)
        if expected_revision <= 0:
            raise ValueError("expected revision must be positive")
        timestamp = _now(now)
        with self.sessions() as session:
            row = session.get(AssistantGrant, grant_id)
            if row is None or row.owner_account_id != owner_account_id:
                raise GrantUpdateNotFound("assistant grant not found")
            if self._active_context(session, row, timestamp) is None or row.revision != expected_revision:
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            current = GrantConstraints.from_dict(row.constraints)
            updated = replace(
                current,
                row_ids=frozenset(),
                person_ids=frozenset(),
                library_keys=frozenset(),
                setting_groups=frozenset(),
                destination_ids=frozenset(),
                include_future_rows=True,
                include_future_people=True,
                include_future_libraries=True,
                max_batch_size=None,
                max_work_per_operation=None,
                owner_managed=True,
                basic_access_v1=access_role,
            )
            changed = session.execute(
                update(AssistantGrant)
                .execution_options(synchronize_session=False)
                .where(
                    AssistantGrant.id == grant_id,
                    AssistantGrant.owner_account_id == owner_account_id,
                    AssistantGrant.revision == expected_revision,
                    AssistantGrant.revoked_at.is_(None),
                    or_(AssistantGrant.expires_at.is_(None), AssistantGrant.expires_at > timestamp),
                )
                .values(
                    capabilities=sorted(capability.value for capability in values),
                    constraints=updated.as_dict(),
                    revision=AssistantGrant.revision + 1,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise GrantUpdateConflict("assistant grant changed, expired, or was revoked")
            session.add(
                Event(
                    scope="assistant.grant.basic_access",
                    level="info",
                    message={
                        "grant_id": grant_id,
                        "actor": {"via": "browser", "account_id": owner_account_id},
                        "access_role": access_role,
                        "revision": expected_revision + 1,
                    },
                )
            )
            session.commit()
            session.refresh(row)
            return _effective_grant_context(session, row)

    def revoke_grant(self, grant_id: str, *, now: datetime | None = None) -> bool:
        """Immediately revoke a grant and all authentication through it."""
        timestamp = _now(now)
        with self.sessions() as session:
            changed = session.execute(
                update(AssistantGrant)
                .where(AssistantGrant.id == grant_id, AssistantGrant.revoked_at.is_(None))
                .values(revoked_at=timestamp, revision=AssistantGrant.revision + 1, updated_at=timestamp)
            ).rowcount
            session.execute(
                update(AssistantLocalCredential)
                .where(
                    AssistantLocalCredential.grant_id == grant_id,
                    AssistantLocalCredential.revoked_at.is_(None),
                )
                .values(revoked_at=timestamp)
            )
            session.execute(
                update(AssistantOAuthToken)
                .where(AssistantOAuthToken.grant_id == grant_id)
                .values(access_revoked_at=timestamp, refresh_revoked_at=timestamp)
            )
            session.commit()
            return changed == 1

    def remove_revoked_grant(
        self,
        grant_id: str,
        *,
        owner_account_id: int,
        now: datetime | None = None,
    ) -> bool:
        """Remove an already-revoked owner connection while retaining its audit history."""
        timestamp = _now(now)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(AssistantGrant, grant_id)
            if row is None or row.owner_account_id != owner_account_id:
                session.rollback()
                return False
            if row.revoked_at is None:
                session.rollback()
                raise GrantRemovalConflict("revoke the connection before removing it")
            session.add(
                Event(
                    ts=timestamp,
                    scope="assistant.grant.removed",
                    level="info",
                    message={
                        "grant_id": grant_id,
                        "actor": {"via": "browser", "account_id": owner_account_id},
                    },
                )
            )
            session.delete(row)
            session.commit()
            return True

    def issue_local_credential(
        self,
        grant_id: str,
        *,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> IssuedSecret:
        """Issue one local bearer credential and persist only its digest."""
        timestamp = _now(now)
        raw = self.hasher.issue("shla").take()
        with self.sessions() as session:
            if self._active_context(session, session.get(AssistantGrant, grant_id), timestamp) is None:
                raise AuthorizationDenied("cannot issue a credential for an inactive grant")
            session.add(
                AssistantLocalCredential(
                    id=_opaque_id("cred"),
                    grant_id=grant_id,
                    token_digest=self.hasher.digest(raw),
                    token_prefix=raw[:12],
                    created_at=timestamp,
                    expires_at=expires_at,
                )
            )
            session.commit()
        return IssuedSecret(raw)

    def authenticate_local_credential(
        self,
        credential: str,
        *,
        now: datetime | None = None,
    ) -> GrantContext | None:
        """Authenticate a local credential without granting legacy owner access."""
        timestamp = _now(now)
        digest = self.hasher.digest(credential)
        with self.sessions() as session:
            row = session.scalar(
                select(AssistantLocalCredential).where(AssistantLocalCredential.token_digest == digest)
            )
            if row is None or not credential_is_active(row.expires_at, row.revoked_at, now=timestamp):
                return None
            context = self._active_context(session, session.get(AssistantGrant, row.grant_id), timestamp)
            if context is None:
                return None
            row.last_used_at = timestamp
            grant = session.get(AssistantGrant, row.grant_id)
            grant.last_used_at = timestamp
            session.commit()
            return context

    def list_local_credentials(
        self,
        grant_id: str,
        *,
        session: Session | None = None,
    ) -> list[AssistantLocalCredential]:
        """List metadata for a grant's local credentials."""
        statement = select(AssistantLocalCredential).where(AssistantLocalCredential.grant_id == grant_id)
        if session is not None:
            return list(session.scalars(statement))
        with self.sessions() as owned:
            return list(owned.scalars(statement))

    def register_oauth_client(
        self,
        *,
        client_id: str,
        client_name: str,
        redirect_uris: list[str],
        token_endpoint_auth_method: str = "none",
        now: datetime | None = None,
    ) -> AssistantOAuthClient:
        """Register exact redirect URIs for an OAuth client."""
        if not redirect_uris or len(set(redirect_uris)) != len(redirect_uris):
            raise ValueError("OAuth clients need unique redirect URIs")
        if token_endpoint_auth_method != "none":
            raise ValueError("only public PKCE clients are supported by this foundation")
        timestamp = _now(now)
        row = AssistantOAuthClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=list(redirect_uris),
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method=token_endpoint_auth_method,
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.sessions() as session:
            session.add(row)
            session.commit()
        return row

    def oauth_client_count(self) -> int:
        """Return the persisted public-client count for registration capacity control."""
        with self.sessions() as session:
            return session.scalar(select(func.count()).select_from(AssistantOAuthClient)) or 0

    def create_consent_flow(
        self,
        *,
        owner_account_id: int,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        client_state: str,
        code_challenge: str,
        code_challenge_method: str,
        expires_at: datetime,
        now: datetime,
    ) -> tuple[str, IssuedSecret]:
        """Persist browser consent and return its independent CSRF token once."""
        csrf = self.hasher.issue("csrf").take()
        flow_id = _opaque_id("consent")
        with self.sessions() as session:
            session.add(
                AssistantConsentFlow(
                    id=flow_id,
                    owner_account_id=owner_account_id,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    resource=resource,
                    scope=scope,
                    client_state=client_state,
                    code_challenge=code_challenge,
                    code_challenge_method=code_challenge_method,
                    csrf_digest=self.hasher.digest(csrf),
                    status="pending",
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            session.commit()
        return flow_id, IssuedSecret(csrf)

    def consume_consent_flow(
        self,
        flow_id: str,
        *,
        owner_account_id: int,
        csrf_token: str,
        approved: bool,
        grant_id: str | None,
        now: datetime,
    ) -> AssistantConsentFlow | None:
        """Validate browser CSRF and atomically consume a consent decision."""
        with self.sessions() as session:
            row = session.get(AssistantConsentFlow, flow_id)
            if (
                row is None
                or row.owner_account_id != owner_account_id
                or row.status != "pending"
                or row.consumed_at is not None
                or as_utc(row.expires_at) <= now
                or not self.hasher.matches(csrf_token, row.csrf_digest)
            ):
                return None
            changed = session.execute(
                update(AssistantConsentFlow)
                .where(
                    AssistantConsentFlow.id == flow_id,
                    AssistantConsentFlow.status == "pending",
                    AssistantConsentFlow.consumed_at.is_(None),
                )
                .values(
                    status="approved" if approved else "denied",
                    grant_id=grant_id if approved else None,
                    consumed_at=now,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                return None
            session.commit()
            consumed = session.get(AssistantConsentFlow, flow_id)
            session.expunge(consumed)
            return consumed

    def get_oauth_client(self, client_id: str) -> AssistantOAuthClient | None:
        """Load an enabled OAuth client."""
        with self.sessions() as session:
            row = session.get(AssistantOAuthClient, client_id)
            if row is None or row.disabled_at is not None:
                return None
            session.expunge(row)
            return row

    def save_authorization_code(
        self,
        *,
        raw_code: str,
        grant_id: str,
        owner_account_id: int,
        client_id: str,
        redirect_uri: str,
        resource: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        with self.sessions() as session:
            session.add(
                AssistantOAuthCode(
                    code_digest=self.hasher.digest(raw_code),
                    grant_id=grant_id,
                    owner_account_id=owner_account_id,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    resource=resource,
                    scope=scope,
                    code_challenge=code_challenge,
                    code_challenge_method=code_challenge_method,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            session.commit()

    def get_authorization_code(self, raw_code: str) -> AssistantOAuthCode | None:
        """Load an unconsumed code for validation without consuming it."""
        with self.sessions() as session:
            row = session.scalar(
                select(AssistantOAuthCode).where(
                    AssistantOAuthCode.code_digest == self.hasher.digest(raw_code),
                    AssistantOAuthCode.consumed_at.is_(None),
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def consume_authorization_code(self, code_id: int, *, now: datetime) -> bool:
        """Atomically consume a code once, including under concurrent redemption."""
        with self.sessions() as session:
            changed = session.execute(
                update(AssistantOAuthCode)
                .where(AssistantOAuthCode.id == code_id, AssistantOAuthCode.consumed_at.is_(None))
                .values(consumed_at=now)
            ).rowcount
            session.commit()
            return changed == 1

    def save_oauth_token_pair(
        self,
        *,
        grant_id: str,
        client_id: str,
        issuer: str,
        resource: str,
        scope: str,
        raw_access: str,
        access_expires_at: datetime,
        raw_refresh: str,
        refresh_expires_at: datetime,
        refresh_family_id: str,
        previous_token_id: int | None,
        now: datetime,
    ) -> int:
        with self.sessions() as session:
            # Rotation validation and Authlib's save hook are separate calls. Serialize issuance
            # with the revocation tombstone so a replay cannot resurrect a revoked family.
            session.execute(text("BEGIN IMMEDIATE"))
            grant = self._active_context(session, session.get(AssistantGrant, grant_id), now)
            if (
                session.get(AssistantOAuthRevokedFamily, refresh_family_id) is not None
                or grant is None
                or grant.client_id != client_id
                or not {cap.value for cap in grant.capabilities}.issuperset(scope.split())
            ):
                raise OAuthTokenIssuanceDenied("The grant or refresh family no longer permits token issuance.")
            row = AssistantOAuthToken(
                grant_id=grant_id,
                client_id=client_id,
                issuer=issuer,
                resource=resource,
                scope=scope,
                access_digest=self.hasher.digest(raw_access),
                access_prefix=raw_access[:12],
                access_expires_at=access_expires_at,
                refresh_digest=self.hasher.digest(raw_refresh),
                refresh_prefix=raw_refresh[:12],
                refresh_expires_at=refresh_expires_at,
                refresh_family_id=refresh_family_id,
                previous_token_id=previous_token_id,
                issued_at=now,
            )
            session.add(row)
            session.commit()
            return row.id

    def verify_access_token(self, raw_token: str, *, now: datetime) -> VerifiedOAuthToken | None:
        """Resolve an opaque access token and its still-current grant."""
        with self.sessions() as session:
            token = session.scalar(
                select(AssistantOAuthToken).where(AssistantOAuthToken.access_digest == self.hasher.digest(raw_token))
            )
            if token is None or not credential_is_active(token.access_expires_at, token.access_revoked_at, now=now):
                return None
            grant = self._active_context(session, session.get(AssistantGrant, token.grant_id), now)
            if grant is None or token.client_id != grant.client_id:
                return None
            token.last_used_at = now
            session.get(AssistantGrant, token.grant_id).last_used_at = now
            session.commit()
            return VerifiedOAuthToken(
                grant=grant,
                token_id=token.id,
                issuer=token.issuer,
                client_id=token.client_id,
                scopes=frozenset(token.scope.split()),
                resource=token.resource,
                expires_at=as_utc(token.access_expires_at),
                issued_at=as_utc(token.issued_at),
                claims={"grant_id": grant.grant_id, "grant_revision": grant.revision},
            )

    def get_refresh_token(self, raw_token: str) -> AssistantOAuthToken | None:
        """Load a refresh credential, including revoked rows for replay detection."""
        with self.sessions() as session:
            row = session.scalar(
                select(AssistantOAuthToken).where(AssistantOAuthToken.refresh_digest == self.hasher.digest(raw_token))
            )
            if row is not None:
                session.expunge(row)
            return row

    def find_oauth_token(self, raw_token: str, *, client_id: str | None = None) -> AssistantOAuthToken | None:
        """Load either token kind for Authlib's revocation endpoint."""
        digest = self.hasher.digest(raw_token)
        with self.sessions() as session:
            statement = select(AssistantOAuthToken).where(
                or_(AssistantOAuthToken.access_digest == digest, AssistantOAuthToken.refresh_digest == digest)
            )
            if client_id is not None:
                statement = statement.where(AssistantOAuthToken.client_id == client_id)
            row = session.scalar(statement)
            if row is not None:
                session.expunge(row)
            return row

    def rotate_refresh_token(self, token_id: int, *, now: datetime) -> bool:
        """Atomically claim a refresh token for rotation."""
        with self.sessions() as session:
            changed = session.execute(
                update(AssistantOAuthToken)
                .where(
                    AssistantOAuthToken.id == token_id,
                    AssistantOAuthToken.refresh_revoked_at.is_(None),
                    AssistantOAuthToken.refresh_expires_at > now,
                )
                .values(refresh_revoked_at=now, last_used_at=now)
            ).rowcount
            session.commit()
            return changed == 1

    def revoke_refresh_family(self, family_id: str, *, now: datetime) -> None:
        """Revoke every access and refresh credential in a rotation family."""
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._revoke_refresh_family_in_session(session, family_id, now=now)
            session.commit()

    @staticmethod
    def _revoke_refresh_family_in_session(session: Session, family_id: str, *, now: datetime) -> None:
        if session.get(AssistantOAuthRevokedFamily, family_id) is None:
            session.add(AssistantOAuthRevokedFamily(family_id=family_id, revoked_at=now))
        session.execute(
            update(AssistantOAuthToken)
            .where(AssistantOAuthToken.refresh_family_id == family_id)
            .values(access_revoked_at=now, refresh_revoked_at=now)
        )

    def revoke_oauth_token(self, raw_token: str, *, token_type_hint: str | None, now: datetime) -> bool:
        """Revoke an access token or a whole refresh family without deleting history."""
        digest = self.hasher.digest(raw_token)
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            token = session.scalar(
                select(AssistantOAuthToken).where(
                    or_(AssistantOAuthToken.access_digest == digest, AssistantOAuthToken.refresh_digest == digest)
                )
            )
            if token is None:
                return False
            is_refresh = token.refresh_digest == digest or token_type_hint == "refresh_token"
            if is_refresh:
                self._revoke_refresh_family_in_session(session, token.refresh_family_id, now=now)
            else:
                token.access_revoked_at = now
            session.commit()
            return True

    @staticmethod
    def _active_context(session: Session, row: AssistantGrant | None, now: datetime) -> GrantContext | None:
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at is not None and as_utc(row.expires_at) <= now:
            return None
        return _effective_grant_context(session, row)
