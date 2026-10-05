from __future__ import annotations

import pytest

from shortlist.server.assistant_auth import (
    AuthorizationDenied,
    Capability,
    GrantConstraints,
    GrantContext,
    GrantPreset,
    ResourceSelection,
    capabilities_for_preset,
    require_authorized,
)


def _grant(
    preset: GrantPreset,
    *,
    constraints: GrantConstraints | None = None,
    extra: set[Capability] | None = None,
) -> GrantContext:
    return GrantContext(
        grant_id="grt_test",
        owner_account_id=42,
        client_id="client-test",
        name="Test assistant",
        preset=preset,
        capabilities=frozenset(capabilities_for_preset(preset) | (extra or set())),
        constraints=constraints or GrantConstraints(),
        revision=1,
    )


class TestGrantPresets:
    def test_inspect_can_prepare_but_cannot_apply_configuration(self) -> None:
        grant = _grant(GrantPreset.INSPECT)

        require_authorized(grant, {Capability.INSTANCE_READ, Capability.CHANGES_PREPARE})
        with pytest.raises(AuthorizationDenied, match=r"config\.write"):
            require_authorized(grant, {Capability.CONFIG_WRITE})

    def test_owner_automation_still_excludes_secret_and_grant_administration(self) -> None:
        capabilities = capabilities_for_preset(GrantPreset.OWNER_AUTOMATION)

        assert Capability.CONFIG_WRITE in capabilities
        assert Capability.RUNS_EXECUTE in capabilities
        assert Capability.SECRETS_READ not in capabilities
        assert Capability.GRANTS_MANAGE not in capabilities
        assert Capability.MAINTENANCE_EXECUTE not in capabilities

    def test_spend_disclosure_and_acquisition_are_always_explicit_additions(self) -> None:
        for preset in GrantPreset:
            capabilities = capabilities_for_preset(preset)
            assert Capability.HISTORY_EXPORT not in capabilities
            assert Capability.HISTORY_PROVIDERS not in capabilities
            assert Capability.AI_GENERATE not in capabilities
            assert Capability.REQUESTS_SEND not in capabilities


class TestGrantConstraints:
    def test_selected_row_grant_cannot_use_an_unlisted_row_or_library(self) -> None:
        grant = _grant(
            GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(row_ids=frozenset({10}), library_keys=frozenset({"1"})),
        )

        require_authorized(
            grant,
            {Capability.ROWS_UPDATE},
            ResourceSelection(row_ids=frozenset({10}), library_keys=frozenset({"1"})),
        )
        with pytest.raises(AuthorizationDenied, match="row"):
            require_authorized(
                grant,
                {Capability.ROWS_UPDATE},
                ResourceSelection(row_ids=frozenset({11}), library_keys=frozenset({"1"})),
            )

    def test_everyone_is_a_fixed_approved_set_without_future_people_permission(self) -> None:
        grant = _grant(
            GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(person_ids=frozenset({1, 2}), include_future_people=False),
        )

        require_authorized(
            grant,
            {Capability.AUDIENCES_WRITE},
            ResourceSelection(person_ids=frozenset({1, 2}), dynamic_audience=False),
        )
        with pytest.raises(AuthorizationDenied, match="future"):
            require_authorized(
                grant,
                {Capability.AUDIENCES_WRITE},
                ResourceSelection(person_ids=frozenset({1, 2}), dynamic_audience=True),
            )

    def test_batch_and_work_limits_can_only_narrow_instance_limits(self) -> None:
        grant = _grant(
            GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(max_batch_size=5, max_work_per_operation=20),
        )

        require_authorized(grant, {Capability.RUNS_EXECUTE}, ResourceSelection(batch_size=5, work_units=20))
        with pytest.raises(AuthorizationDenied, match="batch"):
            require_authorized(grant, {Capability.RUNS_EXECUTE}, ResourceSelection(batch_size=6))
        with pytest.raises(AuthorizationDenied, match="work"):
            require_authorized(grant, {Capability.RUNS_EXECUTE}, ResourceSelection(work_units=21))

    def test_provider_dispatch_requires_an_explicit_nonzero_allowance(self) -> None:
        unbudgeted = _grant(
            GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(),
            extra={Capability.AI_GENERATE},
        )
        budgeted = _grant(
            GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(max_provider_calls=2),
            extra={Capability.AI_GENERATE},
        )

        with pytest.raises(AuthorizationDenied, match="provider"):
            require_authorized(unbudgeted, {Capability.AI_GENERATE}, ResourceSelection(provider_calls=1))
        require_authorized(budgeted, {Capability.AI_GENERATE}, ResourceSelection(provider_calls=2))
        with pytest.raises(AuthorizationDenied, match="provider"):
            require_authorized(budgeted, {Capability.AI_GENERATE}, ResourceSelection(provider_calls=3))
