"""Scoped authentication and authorization for named assistant connections."""

from .policy import AuthorizationDenied, capabilities_for_preset, require_authorized
from .repository import grant_created_rows_in_session, require_current_grant_in_session
from .types import (
    Capability,
    GrantConstraints,
    GrantContext,
    GrantPreset,
    GrantSummary,
    ResourceSelection,
    StoredGrantIdentity,
)

__all__ = [
    "AuthorizationDenied",
    "Capability",
    "GrantConstraints",
    "GrantContext",
    "GrantPreset",
    "GrantSummary",
    "ResourceSelection",
    "StoredGrantIdentity",
    "capabilities_for_preset",
    "grant_created_rows_in_session",
    "require_authorized",
    "require_current_grant_in_session",
]
