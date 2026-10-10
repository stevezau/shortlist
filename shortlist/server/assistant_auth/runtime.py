"""Explicit runtime wiring contract for the main FastAPI application."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy.orm import sessionmaker

from .credentials import CredentialHasher
from .oauth import OAuthService
from .repository import AssistantAuthRepository
from .verifier import AssistantTokenVerifier


@dataclass(frozen=True, slots=True)
class AssistantAuthSettings:
    """Opt-in canonical URLs; request headers never alter these values."""

    enabled: bool
    issuer: str
    resource: str

    def validate(self) -> None:
        """Reject ambiguous or unsafe canonical OAuth URLs."""
        if not self.enabled:
            return
        for label, value in (("issuer", self.issuer), ("resource", self.resource)):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError(f"assistant OAuth {label} must be an absolute HTTP URL without query or fragment")
            if parsed.username or parsed.password:
                raise ValueError(f"assistant OAuth {label} must not contain user information")
            if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
                raise ValueError(f"assistant OAuth {label} requires HTTPS outside loopback")
        if self.issuer.endswith("/"):
            raise ValueError("assistant OAuth issuer must not end with a slash")


@dataclass(frozen=True, slots=True)
class AssistantAuthRuntime:
    """Objects the main application mounts or passes to MCP."""

    repository: AssistantAuthRepository
    oauth: OAuthService
    token_verifier: AssistantTokenVerifier


def build_assistant_auth_runtime(
    *,
    sessions: sessionmaker,
    credential_hash_key: bytes,
    settings: AssistantAuthSettings,
) -> AssistantAuthRuntime | None:
    """Build assistant auth only when the owner enabled the feature."""
    settings.validate()
    if not settings.enabled:
        return None
    repository = AssistantAuthRepository(sessions, CredentialHasher(credential_hash_key))
    oauth = OAuthService(repository, issuer=settings.issuer, resource=settings.resource)
    return AssistantAuthRuntime(
        repository=repository,
        oauth=oauth,
        token_verifier=AssistantTokenVerifier(oauth),
    )


def _is_loopback(hostname: str | None) -> bool:
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
