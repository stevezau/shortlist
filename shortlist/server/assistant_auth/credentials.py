"""Generation and keyed hashing for high-entropy assistant credentials."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime


class SecretAlreadyTaken(RuntimeError):
    """A one-time credential body has already been delivered."""


class IssuedSecret:
    """A newly generated credential whose body can be retrieved once."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value: str | None = value

    def take(self) -> str:
        """Return and forget the credential body."""
        if self._value is None:
            raise SecretAlreadyTaken("credential has already been delivered")
        value = self._value
        self._value = None
        return value

    def __repr__(self) -> str:
        return "IssuedSecret(<redacted>)"


class CredentialHasher:
    """Create opaque credentials and deterministic keyed lookup digests."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("credential hashing key must contain at least 32 bytes")
        self._key = bytes(key)

    def issue(self, prefix: str) -> IssuedSecret:
        """Create a 256-bit random credential with a recognizable prefix."""
        if not prefix.isalnum():
            raise ValueError("credential prefix must be alphanumeric")
        return IssuedSecret(f"{prefix}_{secrets.token_urlsafe(32)}")

    def digest(self, credential: str) -> str:
        """Return the HMAC-SHA256 digest used for indexed lookup."""
        return hmac.new(self._key, credential.encode(), hashlib.sha256).hexdigest()

    def matches(self, credential: str, digest: str) -> bool:
        """Compare a credential with a stored digest in constant time."""
        return hmac.compare_digest(self.digest(credential), digest)


def as_utc(value: datetime) -> datetime:
    """Interpret SQLite's naive timezone-aware columns as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def credential_is_active(
    expires_at: datetime | None,
    revoked_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether stored credential lifetime fields permit authentication."""
    current = now or datetime.now(UTC)
    return revoked_at is None and (expires_at is None or as_utc(expires_at) > current)
