"""REST output schemas derived from strict catalog definitions, retaining future fields."""

from shortlist.server.schema_base import PassthroughModel

from .models import (
    EffectReference,
    NumericRange,
    RowTemplateDefinition,
    SettingDefinition,
    SettingOption,
    SettingPrerequisite,
)


class NumericRangeOut(NumericRange, PassthroughModel):
    """Public numeric bounds."""


class SettingOptionOut(SettingOption, PassthroughModel):
    """Public named option."""


class SettingPrerequisiteOut(SettingPrerequisite, PassthroughModel):
    """Public applicability condition."""


class EffectReferenceOut(EffectReference, PassthroughModel):
    """Public consequence description."""


class SettingDefinitionOut(SettingDefinition, PassthroughModel):
    """Document every current setting field while preserving nested additions."""

    options: tuple[SettingOptionOut, ...] = ()
    range: NumericRangeOut | None = None
    prerequisites: tuple[SettingPrerequisiteOut, ...] = ()
    effects: tuple[EffectReferenceOut, ...]


class RowTemplateDefinitionOut(RowTemplateDefinition, PassthroughModel):
    """Public row template metadata and creation defaults."""
