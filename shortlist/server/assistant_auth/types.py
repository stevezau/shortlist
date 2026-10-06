"""Value types shared by assistant authentication and authorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Capability(StrEnum):
    """One independently enforceable assistant permission."""

    INSTANCE_READ = "instance.read"
    CONFIG_READ = "config.read"
    CATALOG_READ = "catalog.read"
    PEOPLE_READ = "people.read"
    ACTIVITY_READ = "activity.read"
    CHANGES_PREPARE = "changes.prepare"
    HISTORY_USE = "history.use"
    HISTORY_PROVIDERS = "history.providers"
    HISTORY_EXPORT = "history.export"
    ROWS_CREATE = "rows.create"
    ROWS_UPDATE = "rows.update"
    ROWS_ACTIVATE = "rows.activate"
    ROWS_DELETE = "rows.delete"
    AUDIENCES_WRITE = "audiences.write"
    THEMES_WRITE = "themes.write"
    SEASONS_WRITE = "seasons.write"
    CONFIG_WRITE = "config.write"
    PEOPLE_WRITE = "people.write"
    SCHEDULES_WRITE = "schedules.write"
    CONNECTIONS_MANAGE = "connections.manage"
    RUNS_PREVIEW = "runs.preview"
    RUNS_EXECUTE = "runs.execute"
    JOBS_CANCEL = "jobs.cancel"
    AI_GENERATE = "ai.generate"
    REQUESTS_READ = "requests.read"
    REQUESTS_MANAGE = "requests.manage"
    REQUESTS_SEND = "requests.send"
    MAINTENANCE_EXECUTE = "maintenance.execute"
    SECRETS_READ = "secrets.read"
    GRANTS_MANAGE = "grants.manage"


RESERVED_OWNER_CAPABILITIES = frozenset({Capability.SECRETS_READ, Capability.GRANTS_MANAGE})
ASSISTANT_CAPABILITIES = frozenset(Capability) - RESERVED_OWNER_CAPABILITIES


class GrantPreset(StrEnum):
    """Owner-facing starting points for a named assistant connection."""

    INSPECT = "inspect"
    MANAGE_SELECTED_ROWS = "manage_selected_rows"
    OWNER_AUTOMATION = "owner_automation"


@dataclass(frozen=True, slots=True)
class GrantConstraints:
    """Server-enforced bounds applied after capability checks."""

    row_ids: frozenset[int] = frozenset()
    # Storage-only compatibility aliases. Public request/response models never
    # expose these and authorization no longer treats them as a person ACL.
    person_ids: frozenset[int] = frozenset()
    library_keys: frozenset[str] = frozenset()
    setting_groups: frozenset[str] = frozenset()
    destination_ids: frozenset[str] = frozenset()
    include_future_rows: bool = False
    include_future_people: bool = True
    include_future_libraries: bool = False
    max_batch_size: int | None = None
    max_work_per_operation: int | None = None
    # Lifetime allowance for the named grant. Durable reservation/accounting is
    # performed by the operation service; zero means provider dispatch is off.
    max_provider_calls: int = 0

    @property
    def requires_access_approval(self) -> bool:
        """Whether this grant is not a known all-people grant."""
        return self.include_future_people is not True

    def as_dict(self) -> dict:
        """Return the JSON-safe persisted representation."""
        result = {
            "row_ids": sorted(self.row_ids),
            "library_keys": sorted(self.library_keys),
            "setting_groups": sorted(self.setting_groups),
            "destination_ids": sorted(self.destination_ids),
            "include_future_rows": self.include_future_rows,
            "include_future_libraries": self.include_future_libraries,
            "max_batch_size": self.max_batch_size,
            "max_work_per_operation": self.max_work_per_operation,
            "max_provider_calls": self.max_provider_calls,
        }
        # Kept only for binary rollback compatibility. New authorization never
        # reads these as a per-person ACL.
        result["person_ids"] = sorted(self.person_ids)
        result["include_future_people"] = self.include_future_people is True
        return result

    def public_dict(self) -> dict:
        """Return browser-safe constraints without compatibility internals."""
        return {
            "row_ids": sorted(self.row_ids),
            "library_keys": sorted(self.library_keys),
            "setting_groups": sorted(self.setting_groups),
            "destination_ids": sorted(self.destination_ids),
            "include_future_rows": self.include_future_rows,
            "include_future_libraries": self.include_future_libraries,
            "max_batch_size": self.max_batch_size,
            "max_work_per_operation": self.max_work_per_operation,
            "max_provider_calls": self.max_provider_calls,
        }

    @classmethod
    def from_dict(cls, value: dict | None) -> GrantConstraints:
        """Build constraints from a persisted value, rejecting unknown fields."""
        value = dict(value or {})
        known = {
            "row_ids",
            "person_ids",
            "library_keys",
            "setting_groups",
            "destination_ids",
            "include_future_rows",
            "include_future_people",
            "include_future_libraries",
            "max_batch_size",
            "max_work_per_operation",
            "max_provider_calls",
        }
        unknown = value.keys() - known
        if unknown:
            raise ValueError(f"unknown assistant grant constraints: {', '.join(sorted(unknown))}")
        legacy_people = frozenset(value.pop("person_ids", ()))
        legacy_future_people = value.pop("include_future_people", False) is True
        for name in ("row_ids", "library_keys", "setting_groups", "destination_ids"):
            value[name] = frozenset(value.get(name, ()))
        return cls(
            **value,
            person_ids=legacy_people,
            include_future_people=legacy_future_people,
        )


@dataclass(frozen=True, slots=True)
class GrantContext:
    """Authenticated assistant principal passed to application services."""

    grant_id: str
    owner_account_id: int
    client_id: str
    name: str
    preset: GrantPreset
    capabilities: frozenset[Capability]
    constraints: GrantConstraints
    revision: int
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GrantSummary:
    """Owner-facing grant facts without bearer credential material."""

    context: GrantContext
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    local_credential_count: int = 0


@dataclass(frozen=True, slots=True)
class StoredGrantIdentity:
    """Backend-only persisted identity used for trusted approval and run rechecks."""

    grant_id: str
    owner_account_id: int
    client_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class ResourceSelection:
    """Resources and bounded work an operation resolved server-side."""

    row_ids: frozenset[int] = frozenset()
    person_ids: frozenset[int] = frozenset()
    library_keys: frozenset[str] = frozenset()
    setting_groups: frozenset[str] = frozenset()
    destination_ids: frozenset[str] = frozenset()
    dynamic_rows: bool = False
    dynamic_audience: bool = False
    dynamic_libraries: bool = False
    batch_size: int | None = None
    work_units: int | None = None
    provider_calls: int | None = None


@dataclass(frozen=True, slots=True)
class VerifiedOAuthToken:
    """Opaque access-token facts established from server-side state."""

    grant: GrantContext
    token_id: int
    issuer: str
    client_id: str
    scopes: frozenset[str]
    resource: str
    expires_at: datetime
    issued_at: datetime
    claims: dict = field(default_factory=dict)
