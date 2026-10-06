"""Shared descriptive catalogs for REST and assistant integrations."""

from .models import (
    EFFECT_CAPABILITIES,
    Capability,
    Effect,
    EffectReference,
    EffectTiming,
    NumericRange,
    ResetBehavior,
    RowTemplateDefinition,
    SeasonPresetReference,
    SettingDefinition,
    SettingGroup,
    SettingOption,
    SettingPrerequisite,
    ValueType,
    require_known_effects,
)
from .settings import get_setting_definition, get_settings_catalog
from .templates import (
    ROW_INPUT_DEFAULTS,
    get_season_preset_catalog,
    get_template_catalog,
    get_template_definition,
)

__all__ = [
    "EFFECT_CAPABILITIES",
    "ROW_INPUT_DEFAULTS",
    "Capability",
    "Effect",
    "EffectReference",
    "EffectTiming",
    "NumericRange",
    "ResetBehavior",
    "RowTemplateDefinition",
    "SeasonPresetReference",
    "SettingDefinition",
    "SettingGroup",
    "SettingOption",
    "SettingPrerequisite",
    "ValueType",
    "get_season_preset_catalog",
    "get_setting_definition",
    "get_settings_catalog",
    "get_template_catalog",
    "get_template_definition",
    "require_known_effects",
]
