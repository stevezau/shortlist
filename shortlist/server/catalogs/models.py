"""Typed contracts shared by assistant catalogs, REST, and MCP tools."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue

from shortlist.server.assistant_auth import Capability


class CatalogModel(BaseModel):
    """Immutable, strict base model for catalog records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Effect(StrEnum):
    """Observable consequence classes used by planning and authorization."""

    LOCAL_CONFIG = "local_config"
    LOCAL_STATE = "local_state"
    SCHEDULER_CHANGE = "scheduler_change"
    EXTERNAL_READ = "external_read"
    PROVIDER_SPEND = "provider_spend"
    PLEX_READ = "plex_read"
    PLEX_WRITE = "plex_write"
    PLEX_PRIVACY_WRITE = "plex_privacy_write"
    ACQUISITION_WRITE = "acquisition_write"
    NOTIFICATION_SEND = "notification_send"
    CREDENTIAL_CHANGE = "credential_change"
    PERSONAL_DATA_DISCLOSURE = "personal_data_disclosure"


class EffectTiming(StrEnum):
    """When an effect can occur after a catalog-backed change."""

    IMMEDIATE = "immediate"
    QUEUED = "queued"
    FUTURE_RUN = "future_run"
    RECURRING = "recurring"


class ValueType(StrEnum):
    """JSON value shapes accepted for settings."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    STRING_LIST = "string_list"
    INTEGER_LIST = "integer_list"
    OBJECT = "object"


class ResetBehavior(StrEnum):
    """How a caller expresses the absence of a stored setting value."""

    RESTORE_DEFAULT = "restore_default"
    LITERAL_NULL = "literal_null"
    UNAVAILABLE = "unavailable"


class SettingGroup(StrEnum):
    """Stable groups used by settings discovery and grant constraints."""

    PLEX = "plex"
    METADATA = "metadata"
    RECOMMENDATIONS = "recommendations"
    ROW_DEFAULTS = "row_defaults"
    REQUESTS = "requests"
    NOTIFICATIONS = "notifications"
    SCHEDULES = "schedules"
    SYSTEM = "system"
    SETUP = "setup"


class NumericRange(CatalogModel):
    """Inclusive numeric bounds and optional display unit."""

    minimum: float | int
    maximum: float | int
    unit: str | None = None


class SettingOption(CatalogModel):
    """One named value accepted by an enumerated setting."""

    value: JsonValue
    label: str
    description: str | None = None


class SettingPrerequisite(CatalogModel):
    """A machine-readable condition that makes a setting relevant."""

    key: str
    values: tuple[JsonValue, ...]
    description: str


class EffectReference(CatalogModel):
    """One effect and when it can happen."""

    kind: Effect
    timing: EffectTiming
    description: str


class SettingDefinition(CatalogModel):
    """Complete descriptive and policy metadata for one settings key."""

    key: str
    label: str
    description: str
    value_type: ValueType
    group: SettingGroup
    has_default: bool
    default: JsonValue = None
    options: tuple[SettingOption, ...] = ()
    range: NumericRange | None = None
    nullable: bool = False
    reset_behavior: ResetBehavior = ResetBehavior.RESTORE_DEFAULT
    prerequisites: tuple[SettingPrerequisite, ...] = ()
    secret: bool = False
    assistant_writable: bool = True
    effects: tuple[EffectReference, ...]
    required_capabilities: tuple[Capability, ...]


class RowTemplateDefinition(CatalogModel):
    """A row starting point and its fully resolved creation defaults."""

    id: str
    kind: str
    emoji: str
    title: str
    summary: str
    description: str
    highlights: tuple[str, ...]
    values: dict[str, JsonValue]
    effective_values: dict[str, JsonValue]
    changed_fields: tuple[str, ...]
    editable_fields: tuple[str, ...]
    required_services: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()
    audience_behavior: str
    effects: tuple[Effect, ...] = (Effect.LOCAL_CONFIG,)
    required_capabilities: tuple[Capability, ...] = (Capability.ROWS_CREATE,)


class SeasonPresetReference(CatalogModel):
    """Reference to the existing backend season preset catalog."""

    key: str
    label: str
    description: str
    category: str
    note: str


# Minimum effect-specific capabilities used when a catalog describes downstream consequences. An
# operation must also authorize its action and resources; this mapping never grants an effect by
# itself and is deliberately stricter than generic configuration access.
EFFECT_CAPABILITIES: dict[Effect, tuple[Capability, ...]] = {
    Effect.LOCAL_CONFIG: (Capability.CONFIG_WRITE,),
    Effect.LOCAL_STATE: (Capability.CONFIG_WRITE,),
    Effect.SCHEDULER_CHANGE: (Capability.SCHEDULES_WRITE,),
    Effect.EXTERNAL_READ: (Capability.CONNECTIONS_MANAGE,),
    Effect.PROVIDER_SPEND: (Capability.AI_GENERATE,),
    Effect.PLEX_READ: (Capability.RUNS_PREVIEW,),
    Effect.PLEX_WRITE: (Capability.RUNS_EXECUTE,),
    Effect.PLEX_PRIVACY_WRITE: (Capability.AUDIENCES_WRITE, Capability.RUNS_EXECUTE),
    Effect.ACQUISITION_WRITE: (Capability.REQUESTS_SEND,),
    Effect.NOTIFICATION_SEND: (Capability.CONNECTIONS_MANAGE,),
    Effect.CREDENTIAL_CHANGE: (Capability.CONNECTIONS_MANAGE,),
    Effect.PERSONAL_DATA_DISCLOSURE: (Capability.HISTORY_PROVIDERS,),
}


def require_known_effects(effects: list[str] | tuple[str, ...]) -> tuple[Effect, ...]:
    """Return classified effects, rejecting an unknown value.

    Args:
        effects: Effect names supplied by a domain registry or planner.

    Returns:
        The corresponding effect enum values.

    Raises:
        ValueError: If any effect has no policy classification.
    """
    known: list[Effect] = []
    for value in effects:
        try:
            known.append(Effect(value))
        except ValueError as exc:
            raise ValueError(f"unknown effect {value!r}; deny the operation until it is classified") from exc
    return tuple(known)
