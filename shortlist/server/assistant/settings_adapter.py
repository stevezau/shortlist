"""Catalog-backed settings plans with closed field and effect coverage."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, JsonValue
from sqlalchemy import select

from shortlist.server.assistant_auth import Capability
from shortlist.server.catalogs.models import ResetBehavior, ValueType
from shortlist.server.catalogs.settings import get_setting_definition
from shortlist.server.db.models import Collection, Setting, User
from shortlist.server.services.settings_mutations import apply_settings_in_session, prepare_settings_in_session

from .changes import AccessRequirements, DomainPlan, DomainResult, EffectIntent, fingerprint
from .contracts import StrictModel


class SettingsIntent(StrictModel):
    values: dict[str, JsonValue] = Field(
        default_factory=dict,
        max_length=64,
        description=(
            "Non-secret setting values, keyed by exact names from describe_settings. Unspecified fields are preserved."
        ),
    )
    resets: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="Fields to remove from saved configuration so the current built-in default is inherited.",
    )


def validate_assistant_values(intent: SettingsIntent) -> None:
    """Deny unknown fields, secret inputs and JSON type coercion before any mutation."""
    if not intent.values and not intent.resets:
        raise ValueError("Choose at least one setting to change or reset.")
    for key in set(intent.values) | set(intent.resets):
        try:
            definition = get_setting_definition(key)
        except KeyError:
            raise ValueError(f"Unknown setting: {key}") from None
        if definition.secret or not definition.assistant_writable:
            raise ValueError(f"{key} must be managed directly in the owner's browser.")
        if key in intent.resets and definition.reset_behavior != ResetBehavior.RESTORE_DEFAULT:
            raise ValueError(f"{key} does not support restoring an inherited default.")
        if key not in intent.values:
            continue
        value = intent.values[key]
        if value is None:
            if not definition.nullable:
                raise ValueError(f"{key} does not accept null; use resets to restore its default.")
            continue
        valid = {
            ValueType.BOOLEAN: lambda value=value: type(value) is bool,
            ValueType.INTEGER: lambda value=value: type(value) is int,
            ValueType.NUMBER: lambda value=value: type(value) in (int, float),
            ValueType.STRING: lambda value=value: isinstance(value, str) and len(value) <= 16_000,
            ValueType.STRING_LIST: lambda value=value: (
                isinstance(value, list) and len(value) <= 200 and all(isinstance(v, str) for v in value)
            ),
            ValueType.INTEGER_LIST: lambda value=value: (
                isinstance(value, list) and len(value) <= 200 and all(type(v) is int for v in value)
            ),
            ValueType.OBJECT: lambda value=value: isinstance(value, dict),
        }[definition.value_type]()
        if not valid:
            raise ValueError(f"{key} requires {definition.value_type.value}.")
        if definition.options:
            options = [option.value for option in definition.options]
            selected = value if definition.value_type in {ValueType.STRING_LIST, ValueType.INTEGER_LIST} else [value]
            if any(item not in options for item in selected):
                raise ValueError(f"{key} is not one of the catalog's supported options.")
        if definition.range is not None and not definition.range.minimum <= value <= definition.range.maximum:
            raise ValueError(f"{key} is outside its supported range.")


class SettingsAdapter:
    kind = "configuration"

    def __init__(self, secrets) -> None:
        self.secrets = secrets

    def prepare(self, session, intent: dict) -> DomainPlan:
        body = SettingsIntent.model_validate(intent)
        validate_assistant_values(body)
        mutation = prepare_settings_in_session(session, self.secrets, body.values, resets=tuple(body.resets))
        keys = set(body.values) | set(body.resets)
        definitions = [get_setting_definition(key) for key in sorted(keys)]
        capabilities = {Capability.CONFIG_WRITE.value}
        for definition in definitions:
            capabilities.update(cap.value for cap in definition.required_capabilities)
        groups = {definition.group.value for definition in definitions}
        global_effects = bool(groups - {"system"})
        rows = list(session.scalars(select(Collection))) if global_effects else []
        people = tuple(session.scalars(select(User.id).where(User.removed_at.is_(None)))) if global_effects else ()
        # Source snapshots stay privileged; only their hashes leave this method.
        dependencies = {
            "settings": fingerprint({row.key: row.value for row in session.scalars(select(Setting))}),
            "rows": fingerprint(
                [{c.name: str(getattr(row, c.name)) for c in Collection.__table__.columns} for row in rows]
            ),
            "people": fingerprint(list(people)),
        }
        from shortlist.server.services.settings_validation import FETCHED_URL_KEYS

        destinations = []
        for key in sorted(keys & set(FETCHED_URL_KEYS)):
            value = body.values.get(key) if key in body.values else get_setting_definition(key).default
            if not value or key not in mutation.changed:
                continue
            parsed = urlsplit(str(value))
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "Assistant-configured service URLs must not contain credentials, queries or fragments."
                )
            destinations.append(str(value).rstrip("/"))
        recurring_external = bool(capabilities & {Capability.AI_GENERATE.value, Capability.REQUESTS_SEND.value})
        effects = (
            (EffectIntent("assistant.converge", {"domain": "settings", "steps": list(mutation.steps)}, "settings"),)
            if mutation.steps
            else ()
        )
        return DomainPlan(
            normalized_intent=body.model_dump(mode="json"),
            dependencies=dependencies,
            requirements=AccessRequirements(
                capabilities=tuple(sorted(capabilities)),
                row_ids=tuple(row.id for row in rows),
                person_ids=people,
                setting_groups=tuple(sorted(groups)),
                destination_ids=tuple(destinations),
                library_keys=tuple(sorted({str(key) for row in rows for key in row.library_keys or []})),
                dynamic_rows=global_effects,
                dynamic_audience=global_effects,
                dynamic_libraries=any(not row.library_keys for row in rows),
                batch_size=len(keys),
                requires_approval=bool(destinations) or recurring_external,
            ),
            effects=effects,
            summary={
                "description": "Update selected settings and persist their required follow-up work.",
                "configuration_diff": mutation.changed,
                "declared_effects": [
                    effect.model_dump(mode="json") for definition in definitions for effect in definition.effects
                ],
                "recurring_policy": "Saved settings and schedules persist after this connection expires.",
                "recurring_external_effects": recurring_external,
                "quota_policy": (
                    "Owner-approved recurring provider or acquisition settings can authorize ongoing work. "
                    "The assistant theme-generation call quota does not cap ordinary scheduled runs."
                )
                if recurring_external
                else None,
            },
        )

    def apply(self, session, intent: dict) -> DomainResult:
        body = SettingsIntent.model_validate(intent)
        mutation = prepare_settings_in_session(session, self.secrets, body.values, resets=tuple(body.resets))
        apply_settings_in_session(session, self.secrets, mutation)
        return DomainResult(result={"changed_keys": sorted(mutation.changed)}, audit_diff=mutation.changed)
