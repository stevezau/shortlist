from __future__ import annotations

import pytest

from shortlist.server.api.settings import KNOWN_KEYS
from shortlist.server.catalogs import (
    Effect,
    ResetBehavior,
    get_setting_definition,
    get_settings_catalog,
    get_template_catalog,
    require_known_effects,
)
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS


def test_settings_catalog_covers_every_public_writable_setting() -> None:
    catalog = get_settings_catalog()
    by_key = {definition.key: definition for definition in catalog}

    assert set(by_key) == KNOWN_KEYS
    assert not (set(by_key) & PRIVATE_KEYS)
    assert {key for key, definition in by_key.items() if definition.secret} == SECRET_KEYS - PRIVATE_KEYS

    for key, definition in by_key.items():
        assert definition.label.strip()
        assert definition.description.strip()
        assert definition.effects
        assert definition.required_capabilities
        if key in DEFAULTS:
            assert definition.has_default is True
            assert definition.default == DEFAULTS[key]
        else:
            assert definition.has_default is False


def test_catalog_distinguishes_null_value_from_reset() -> None:
    assert get_setting_definition("requests.min_rating_other").reset_behavior is ResetBehavior.LITERAL_NULL
    assert get_setting_definition("sync.watch_cron").reset_behavior is ResetBehavior.RESTORE_DEFAULT


def test_unknown_settings_and_effects_fail_closed() -> None:
    with pytest.raises(KeyError, match="unknown setting"):
        get_setting_definition("new.unclassified.setting")

    with pytest.raises(ValueError, match="unknown effect"):
        require_known_effects(["local_config", "unclassified_admin_shortcut"])


def test_row_templates_have_complete_effective_configuration() -> None:
    templates = get_template_catalog()

    assert templates
    assert len({template.id for template in templates}) == len(templates)
    assert {template.id for template in templates if template.kind == "ai"} == {"describe-a-row"}
    expected_fields = set(templates[0].effective_values)
    assert expected_fields

    for template in templates:
        assert set(template.effective_values) == expected_fields
        assert set(template.values) <= expected_fields
        assert tuple(template.values) == template.changed_fields
        assert template.effects == (Effect.LOCAL_CONFIG,)
