"""Retention for OAuth working state, so the assistant tables do not grow for ever.

Every refresh writes a token row and every consent writes a flow and a code, so without a sweep these
tables gain a row per ~15 minutes of an active connection. Rows are only removed once they can no
longer matter to a security decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from .models import (
    AssistantConsentFlow,
    AssistantGrant,
    AssistantOAuthClient,
    AssistantOAuthCode,
    AssistantOAuthRevokedFamily,
    AssistantOAuthToken,
)

#: A dynamically registered client that has asked for nothing in this long is spam or abandoned.
UNUSED_CLIENT_TTL = timedelta(hours=24)

#: A revoked family's tombstone only has to outlive the longest refresh token it could still hold:
#: `OAuthService.refresh_token_ttl` (30 days) plus a day. A unit test pins the margin.
REVOKED_FAMILY_TTL = timedelta(days=31)


def prune_unused_oauth_clients(session: Session, *, now: datetime) -> int:
    """Delete clients with no code, token, grant or consent flow after `UNUSED_CLIENT_TTL`.

    `/register` is public and caps the client count, so without this a flood of registrations that
    never authorize fills the cap for good and locks real clients out.
    """
    used = (
        exists().where(AssistantOAuthCode.client_id == AssistantOAuthClient.client_id)
        | exists().where(AssistantOAuthToken.client_id == AssistantOAuthClient.client_id)
        | exists().where(AssistantGrant.client_id == AssistantOAuthClient.client_id)
        | exists().where(AssistantConsentFlow.client_id == AssistantOAuthClient.client_id)
    )
    stale = select(AssistantOAuthClient.client_id).where(
        AssistantOAuthClient.created_at < now - UNUSED_CLIENT_TTL, ~used
    )
    return session.execute(delete(AssistantOAuthClient).where(AssistantOAuthClient.client_id.in_(stale))).rowcount


def prune_oauth_state(session: Session, *, now: datetime) -> dict[str, int]:
    """Delete expired codes, consent flows and tokens, old revocation tombstones and unused clients.

    A rotated or revoked token is kept until its REFRESH expiry, not its access expiry: replaying a
    rotated refresh token is what revokes the whole family, and that needs the row to still be there.
    Past refresh expiry a replay is refused as expired anyway.
    """
    codes = session.execute(delete(AssistantOAuthCode).where(AssistantOAuthCode.expires_at < now)).rowcount
    flows = session.execute(delete(AssistantConsentFlow).where(AssistantConsentFlow.expires_at < now)).rowcount
    tokens = session.execute(
        delete(AssistantOAuthToken).where(
            AssistantOAuthToken.refresh_expires_at < now, AssistantOAuthToken.access_expires_at < now
        )
    ).rowcount
    families = session.execute(
        delete(AssistantOAuthRevokedFamily).where(AssistantOAuthRevokedFamily.revoked_at < now - REVOKED_FAMILY_TTL)
    ).rowcount
    clients = prune_unused_oauth_clients(session, now=now)
    return {
        "oauth_codes": codes,
        "consent_flows": flows,
        "oauth_tokens": tokens,
        "revoked_families": families,
        "oauth_clients": clients,
    }
