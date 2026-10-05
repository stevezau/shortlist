"""Capability presets and fail-closed resource authorization."""

from __future__ import annotations

from collections.abc import Iterable

from .types import Capability, GrantContext, GrantPreset, ResourceSelection


class AuthorizationDenied(PermissionError):
    """The authenticated assistant grant does not authorize an operation."""


_INSPECT = {
    Capability.INSTANCE_READ,
    Capability.CONFIG_READ,
    Capability.CATALOG_READ,
    Capability.ACTIVITY_READ,
    Capability.CHANGES_PREPARE,
}

_SELECTED_ROWS = _INSPECT | {
    Capability.PEOPLE_READ,
    Capability.ROWS_CREATE,
    Capability.ROWS_UPDATE,
    Capability.AUDIENCES_WRITE,
    Capability.THEMES_WRITE,
    Capability.RUNS_PREVIEW,
}

_OWNER_AUTOMATION = _SELECTED_ROWS | {
    Capability.HISTORY_USE,
    Capability.ROWS_ACTIVATE,
    Capability.ROWS_DELETE,
    Capability.SEASONS_WRITE,
    Capability.CONFIG_WRITE,
    Capability.PEOPLE_WRITE,
    Capability.SCHEDULES_WRITE,
    Capability.CONNECTIONS_MANAGE,
    Capability.RUNS_EXECUTE,
    Capability.JOBS_CANCEL,
    Capability.REQUESTS_READ,
    Capability.REQUESTS_MANAGE,
}

_PRESETS = {
    GrantPreset.INSPECT: frozenset(_INSPECT),
    GrantPreset.MANAGE_SELECTED_ROWS: frozenset(_SELECTED_ROWS),
    GrantPreset.OWNER_AUTOMATION: frozenset(_OWNER_AUTOMATION),
}


def capabilities_for_preset(preset: GrantPreset) -> set[Capability]:
    """Return a mutable copy of a preset's base capabilities."""
    return set(_PRESETS[preset])


def require_authorized(
    grant: GrantContext,
    required: Iterable[Capability],
    resources: ResourceSelection | None = None,
) -> None:
    """Raise unless a grant covers every capability and resolved resource.

    Args:
        grant: Authenticated named assistant connection.
        required: Union of capabilities derived from the operation's real effects.
        resources: Objects and work limits resolved by server application code.

    Raises:
        AuthorizationDenied: The grant lacks a capability or exceeds a constraint.
    """
    missing = set(required) - grant.capabilities
    if missing:
        names = ", ".join(sorted(capability.value for capability in missing))
        raise AuthorizationDenied(f"missing permission: {names}")
    if resources is None:
        return

    constraints = grant.constraints
    _require_subset("row", resources.row_ids, constraints.row_ids, constraints.include_future_rows)
    _require_subset("person", resources.person_ids, constraints.person_ids, constraints.include_future_people)
    _require_subset("library", resources.library_keys, constraints.library_keys, constraints.include_future_libraries)
    _require_subset("setting group", resources.setting_groups, constraints.setting_groups, False)
    _require_subset("destination", resources.destination_ids, constraints.destination_ids, False)

    if resources.dynamic_rows and not constraints.include_future_rows:
        raise AuthorizationDenied("future rows are outside this grant")
    if resources.dynamic_audience and not constraints.include_future_people:
        raise AuthorizationDenied("future people are outside this grant")
    if resources.dynamic_libraries and not constraints.include_future_libraries:
        raise AuthorizationDenied("future libraries are outside this grant")
    if (
        resources.batch_size is not None
        and constraints.max_batch_size is not None
        and resources.batch_size > constraints.max_batch_size
    ):
        raise AuthorizationDenied("batch exceeds this grant's limit")
    if (
        resources.work_units is not None
        and constraints.max_work_per_operation is not None
        and resources.work_units > constraints.max_work_per_operation
    ):
        raise AuthorizationDenied("work exceeds this grant's per-operation limit")
    if resources.provider_calls is not None and resources.provider_calls > constraints.max_provider_calls:
        raise AuthorizationDenied("provider calls exceed this grant's allowance")


def _require_subset(label: str, requested: frozenset, allowed: frozenset, dynamic: bool) -> None:
    if requested and not dynamic and not allowed.issuperset(requested):
        raise AuthorizationDenied(f"{label} is outside this grant")
