"""Constrained first-install bootstrap authority."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from .credentials import as_utc
from .models import AssistantBootstrapFlow
from .repository import AssistantAuthRepository
from .types import GrantConstraints, GrantContext, GrantPreset


class BootstrapDenied(PermissionError):
    """Deployment control and Plex ownership did not both validate."""


class BootstrapService:
    """Exchange short-lived deployment proof plus Plex ownership for a grant."""

    flow_ttl = timedelta(minutes=15)
    setup_grant_ttl = timedelta(hours=8)

    def __init__(self, repository: AssistantAuthRepository) -> None:
        self.repository = repository

    def begin(
        self,
        *,
        deployment_proof: str,
        client_id: str,
        client_name: str,
        current_owner_account_id: int | None,
        expected_machine_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Start bootstrap through a deployment-controlled out-of-band proof.

        The proof belongs in a helper or protected deployment channel. It must
        never be placed in an MCP result, URL, log, or chat instruction.
        """
        if current_owner_account_id is not None:
            raise BootstrapDenied("an existing owner must create assistant access in the browser")
        if len(deployment_proof) < 32:
            raise BootstrapDenied("deployment proof is too short")
        timestamp = now or datetime.now(UTC)
        flow_id = f"boot_{secrets.token_urlsafe(18)}"
        row = AssistantBootstrapFlow(
            id=flow_id,
            client_id=client_id,
            client_name=client_name,
            deployment_proof_digest=self.repository.hasher.digest(deployment_proof),
            expected_machine_id=expected_machine_id,
            status="pending",
            created_at=timestamp,
            expires_at=timestamp + self.flow_ttl,
        )
        with self.repository.sessions() as session:
            session.add(row)
            session.commit()
        return flow_id

    def complete(
        self,
        *,
        flow_id: str,
        deployment_proof: str,
        plex_owner_account_id: int,
        owned_machine_ids: set[str],
        selected_machine_id: str,
        current_owner_account_id: int | None,
        seeded_token_account_id: int | None = None,
        now: datetime | None = None,
    ) -> GrantContext:
        """Verify exact ownership and atomically replace bootstrap with a grant."""
        timestamp = now or datetime.now(UTC)
        with self.repository.sessions() as session:
            row = session.scalar(select(AssistantBootstrapFlow).where(AssistantBootstrapFlow.id == flow_id))
            if (
                row is None
                or row.status != "pending"
                or row.consumed_at is not None
                or as_utc(row.expires_at) <= timestamp
                or not self.repository.hasher.matches(deployment_proof, row.deployment_proof_digest)
            ):
                raise BootstrapDenied("bootstrap flow is invalid, expired, or already used")
            if selected_machine_id not in owned_machine_ids:
                raise BootstrapDenied("the signed-in Plex account does not own the selected server")
            if row.expected_machine_id is not None and row.expected_machine_id != selected_machine_id:
                raise BootstrapDenied("the selected Plex server does not match the deployment proof")
            if seeded_token_account_id is not None and seeded_token_account_id != plex_owner_account_id:
                raise BootstrapDenied("the seeded Plex credential belongs to another account")
            if current_owner_account_id is not None and current_owner_account_id != plex_owner_account_id:
                raise BootstrapDenied("the linked Shortlist owner does not match the verified Plex owner")

            changed = session.execute(
                update(AssistantBootstrapFlow)
                .where(
                    AssistantBootstrapFlow.id == flow_id,
                    AssistantBootstrapFlow.status == "pending",
                    AssistantBootstrapFlow.consumed_at.is_(None),
                )
                .values(
                    status="verified",
                    owner_account_id=plex_owner_account_id,
                    verified_machine_id=selected_machine_id,
                    consumed_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                raise BootstrapDenied("bootstrap flow was already consumed")
            grant = self.repository.create_grant(
                owner_account_id=plex_owner_account_id,
                client_id=row.client_id,
                name=row.client_name,
                preset=GrantPreset.OWNER_AUTOMATION,
                constraints=GrantConstraints(),
                expires_at=timestamp + self.setup_grant_ttl,
                now=timestamp,
                session=session,
            )
            row = session.get(AssistantBootstrapFlow, flow_id)
            row.grant_id = grant.grant_id
            session.commit()
            return grant
