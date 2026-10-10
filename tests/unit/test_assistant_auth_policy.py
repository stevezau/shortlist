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
from shortlist.server.assistant_auth.policy import basic_role_capabilities


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
    def test_versioned_roles_have_closed_read_and_manage_authority(self) -> None:
        view = basic_role_capabilities("view")
        manage = basic_role_capabilities("manage")

        assert {Capability.CONFIG_READ, Capability.HISTORY_EXPORT, Capability.REQUESTS_READ} <= view
        assert {Capability.CHANGES_PREPARE, Capability.RUNS_PREVIEW, Capability.RUNS_EXECUTE}.isdisjoint(view)
        assert {Capability.CONFIG_WRITE, Capability.RUNS_EXECUTE, Capability.MAINTENANCE_EXECUTE} <= manage
        assert {Capability.AI_GENERATE, Capability.SECRETS_READ, Capability.GRANTS_MANAGE}.isdisjoint(manage)

    def test_versioned_view_ceiling_rejects_injected_mutation_capability(self) -> None:
        grant = _grant(
            GrantPreset.OWNER_AUTOMATION,
            constraints=GrantConstraints(basic_access_v1="view"),
            extra={Capability.MAINTENANCE_EXECUTE},
        )

        require_authorized(grant, {Capability.HISTORY_USE})
        with pytest.raises(AuthorizationDenied, match="access role"):
            require_authorized(grant, {Capability.CHANGES_PREPARE})
        with pytest.raises(AuthorizationDenied, match="access role"):
            require_authorized(grant, {Capability.MAINTENANCE_EXECUTE})

    def test_missing_role_marker_is_legacy_and_invalid_role_is_rejected(self) -> None:
        assert GrantConstraints.from_dict({}).basic_access_v1 is None
        assert "basic_access_v1" not in GrantConstraints().as_dict()
        with pytest.raises(ValueError, match="basic access role"):
            GrantConstraints.from_dict({"basic_access_v1": "admin"})

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

    def test_legacy_people_constraint_requires_owner_approval(self) -> None:
        grant = _grant(
            GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(person_ids=frozenset({1, 2}), include_future_people=False),
        )

        with pytest.raises(AuthorizationDenied, match="owner approval"):
            require_authorized(
                grant,
                {Capability.AUDIENCES_WRITE},
                ResourceSelection(person_ids=frozenset({1, 2}), dynamic_audience=False),
            )

    def test_legacy_connection_allows_only_explicit_metadata(self) -> None:
        grant = _grant(
            GrantPreset.INSPECT,
            constraints=GrantConstraints(include_future_people=False),
        )

        require_authorized(grant, {Capability.INSTANCE_READ}, allow_legacy_metadata=True)
        with pytest.raises(AuthorizationDenied, match="owner approval"):
            require_authorized(grant, {Capability.INSTANCE_READ})

    def test_all_people_compatibility_marker_does_not_filter_operation_targets(self) -> None:
        grant = _grant(
            GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(person_ids=frozenset({1}), include_future_people=True),
        )

        require_authorized(
            grant,
            {Capability.AUDIENCES_WRITE},
            ResourceSelection(person_ids=frozenset({2}), dynamic_audience=True),
        )

    def test_without_the_all_people_marker_the_same_target_is_denied(self) -> None:
        grant = _grant(
            GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(person_ids=frozenset({1}), include_future_people=False),
        )

        with pytest.raises(AuthorizationDenied):
            require_authorized(
                grant,
                {Capability.AUDIENCES_WRITE},
                ResourceSelection(person_ids=frozenset({2}), dynamic_audience=True),
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
