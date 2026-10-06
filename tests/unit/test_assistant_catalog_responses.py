"""Public catalog documentation preserves fields without weakening internal validation."""

import pytest
from pydantic import ValidationError

from shortlist.server.catalogs.models import NumericRange, SettingDefinition
from shortlist.server.catalogs.responses import NumericRangeOut, SettingDefinitionOut
from shortlist.server.catalogs.settings import get_settings_catalog


def test_public_catalog_preserves_future_fields_at_every_nested_boundary() -> None:
    source = next(item for item in get_settings_catalog() if item.range is not None)
    payload = source.model_dump(mode="json")
    payload["future_setting_metadata"] = "kept"
    payload["range"]["future_unit_metadata"] = "kept"
    payload["options"] = [{"value": 1, "label": "One", "description": None, "future_option_metadata": "kept"}]
    payload["prerequisites"] = [
        {"key": "enabled", "values": [True], "description": "Enabled", "future_prerequisite_metadata": "kept"}
    ]
    payload["effects"][0]["future_effect_metadata"] = "kept"

    assert SettingDefinitionOut.model_validate(payload).model_dump(mode="json") == payload
    with pytest.raises(ValidationError):
        SettingDefinition.model_validate(payload)


def test_internal_range_remains_strict_and_public_range_preserves_extensions() -> None:
    payload = {"minimum": 1, "maximum": 10, "future": "kept"}
    assert NumericRangeOut.model_validate(payload).model_dump()["future"] == "kept"
    with pytest.raises(ValidationError):
        NumericRange.model_validate(payload)
