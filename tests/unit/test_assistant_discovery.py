"""Assistant discovery must enforce scope before returning configuration or identities."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.discovery import DiscoveryService
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantContext, GrantPreset
from shortlist.server.catalogs.templates import ROW_INPUT_DEFAULTS
from shortlist.server.db.models import Collection, CollectionAudience, Setting, Theme, ThemeHistory, User
from tests.db_helpers import create_schema, disposing_engine


@pytest.fixture
def service():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine)
        with sessions() as session:
            session.add_all(
                [
                    User(id=1, plex_account_id=11, username="Allowed", slug="allowed"),
                    User(id=2, plex_account_id=22, username="Private", slug="private"),
                    Collection(id=1, slug="allowed", name="Allowed row", audience="subset", library_keys=["1"]),
                    Collection(id=2, slug="private", name="Private row", audience="subset", library_keys=["2"]),
                    Setting(key="tmdb.apikey", value={"v": "never-export-this-secret"}),
                    Setting(key="api.token", value={"v": "never-export-owner-token"}),
                ]
            )
            session.flush()
            session.add_all(
                [CollectionAudience(collection_id=1, user_id=1), CollectionAudience(collection_id=2, user_id=2)]
            )
            session.commit()
        yield DiscoveryService(SimpleNamespace(sessions=sessions, secrets=None))


@pytest.fixture
def principal():
    return GrantContext(
        grant_id="grant",
        owner_account_id=11,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.MANAGE_SELECTED_ROWS,
        capabilities=frozenset(
            {Capability.INSTANCE_READ, Capability.CONFIG_READ, Capability.CATALOG_READ, Capability.PEOPLE_READ}
        ),
        constraints=GrantConstraints(
            row_ids=frozenset({1}),
            person_ids=frozenset({1}),
            library_keys=frozenset({"1"}),
            setting_groups=frozenset({"metadata"}),
        ),
        revision=1,
    )


def test_directory_returns_all_people_without_grant_person_filtering(service, principal):
    result = service.people(principal, limit=10, offset=0)
    assert [item["id"] for item in result.data["items"]] == [1, 2]
    assert "prefs" not in result.model_dump_json()


def test_rows_cannot_be_read_by_guessing_an_id(service, principal):
    from shortlist.server.assistant_auth import AuthorizationDenied

    with pytest.raises(AuthorizationDenied):
        service.row(principal, 2)
    result = service.rows(principal, limit=10, offset=0)
    assert [item["id"] for item in result.data["items"]] == [1]


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("hub_anchor", {"1": {"row": "private", "before": False, "top": False, "enabled": True}}),
        ("avoid_rows", ["private"]),
        ("ai_instructions", {"mode": "add", "text": "Prefer quiet comedies."}),
        ("ai_paused", True),
    ],
)
def test_full_row_scope_reads_persisted_editable_values(service, principal, field, expected):
    """An assistant can inspect saved values before proposing a preserving edit."""
    with service.state.sessions() as session:
        row = session.get(Collection, 1)
        row.hub_anchor = {"1": {"row": "private", "before": False, "top": False, "enabled": True}}
        row.avoid_rows = ["private"]
        row.prompt = {"mode": "add", "text": "Prefer quiet comedies."}
        row.ai_paused = True
        session.commit()
    broad = replace(
        principal,
        constraints=replace(
            principal.constraints,
            include_future_rows=True,
            include_future_people=True,
            include_future_libraries=True,
        ),
    )

    result = service.row(broad, 1)

    assert result.data["fields"][field] == expected
    assert field not in result.data.get("redacted_fields", {})


@pytest.mark.parametrize("missing_scope", ["row", "library"])
def test_inaccessible_row_references_are_explicitly_redacted_as_whole_fields(service, principal, missing_scope):
    """A partial configuration must not masquerade as a safe replacement value."""
    with service.state.sessions() as session:
        row = session.get(Collection, 1)
        row.hub_anchor = {
            "1": {"row": "allowed", "before": False, "top": False, "enabled": True},
            "2": {"row": "private", "before": False, "top": False, "enabled": True},
        }
        row.avoid_rows = ["allowed", "private"]
        session.commit()
    scoped = replace(
        principal,
        constraints=replace(
            principal.constraints,
            row_ids=frozenset({1} if missing_scope == "row" else {1, 2}),
            library_keys=frozenset({"1", "2"} if missing_scope == "row" else {"1"}),
        ),
    )

    result = service.row(scoped, 1)

    for field in ("hub_anchor", "avoid_rows"):
        assert field not in result.data["fields"]
        assert result.data["redacted_fields"][field]
    assert result.warnings
    assert "private" not in result.model_dump_json()


@settings(max_examples=16, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    row_selected=st.booleans(),
    library_selected=st.booleans(),
    future_rows=st.booleans(),
    future_libraries=st.booleans(),
)
def test_reference_disclosure_never_exceeds_combined_row_and_library_scope(
    service, principal, row_selected, library_selected, future_rows, future_libraries
):
    # Each example writes the same complete saved values, so no prior example's
    # state can widen or narrow this example's effective grant.
    expected = {
        "hub_anchor": {"1": {"row": "private", "before": False, "top": False, "enabled": True}},
        "avoid_rows": ["private"],
    }
    with service.state.sessions() as session:
        row = session.get(Collection, 1)
        row.hub_anchor, row.avoid_rows = expected["hub_anchor"], expected["avoid_rows"]
        session.commit()
    scoped = replace(
        principal,
        constraints=replace(
            principal.constraints,
            row_ids=frozenset({1, 2} if row_selected else {1}),
            library_keys=frozenset({"1", "2"} if library_selected else {"1"}),
            include_future_rows=future_rows,
            include_future_libraries=future_libraries,
        ),
    )

    result = service.row(scoped, 1)

    if (row_selected or future_rows) and (library_selected or future_libraries):
        assert {key: result.data["fields"][key] for key in expected} == expected
    else:
        assert not expected.keys() & result.data["fields"].keys()
        assert expected.keys() <= result.data["redacted_fields"].keys()
        assert "private" not in result.model_dump_json()


def test_configuration_only_returns_granted_group_and_secret_presence(service, principal):
    result = service.configuration(principal, "metadata")
    text = result.model_dump_json()
    assert "never-export" not in text
    assert "api.token" not in text
    assert "tmdb.apikey" in text
    assert result.data["values"]["tmdb.apikey"]["configured"] is True


def test_configuration_group_cannot_bypass_scope(service, principal):
    from shortlist.server.assistant_auth import AuthorizationDenied

    with pytest.raises(AuthorizationDenied):
        service.configuration(principal, "requests")


def test_template_discovery_has_precise_row_input_metadata(service, principal):
    """Keep LLM discovery precise enough to construct a valid row plan.

    This reads the public discovery result and anchors fields whose null defaults
    would otherwise conceal their actual input type, along with the nested
    objects used by the MCP row workflow.
    """
    result = service.templates(principal)
    definitions = result.data["field_definitions"]

    assert set(definitions) == set(ROW_INPUT_DEFAULTS) | {"ai_paused"}
    assert "defer_rename" not in definitions
    assert definitions["theme_id"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert definitions["theme_id"]["default"] is None
    assert "ordinary row" in definitions["theme_id"]["description"]
    assert definitions["watched_pct"]["anyOf"] == [
        {"maximum": 1.0, "minimum": 0.0, "type": "number"},
        {"type": "null"},
    ]
    assert definitions["req_auto_send"]["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
    assert definitions["req_preferred_languages"]["anyOf"] == [
        {"items": {"type": "string"}, "maxItems": 50, "type": "array"},
        {"type": "null"},
    ]
    assert definitions["ai_paused"]["type"] == "boolean"
    assert definitions["ai_paused"]["default"] is False
    assert definitions["ai_paused"]["effects"]["true"] == "prevents recurring provider top-ups"

    poster = definitions["poster"]
    assert poster["properties"]["mode"]["enum"] == ["", "ai", "generate", "text", "upload"]
    assert poster["properties"]["title"]["maxLength"] == 120
    assert "no AI" in poster["description"]
    anchor = definitions["hub_anchor"]["additionalProperties"]
    assert anchor["properties"]["anchor"] == {"default": "", "maxLength": 255, "title": "Anchor", "type": "string"}
    assert anchor["properties"]["enabled"]["default"] is True
    instructions = definitions["ai_instructions"]["properties"]
    assert instructions["mode"]["enum"] == ["add", "default", "own"]
    assert instructions["text"]["maxLength"] == 2000
    assert result.data["creation_defaults"] == {
        "enabled": False,
        "schedule_active": False,
        "stored_schedule": "30 3 * * *",
        "supplied_values_override_template_defaults": True,
        "description": "A planned row is inactive until explicitly enabled; supplied values replace template defaults.",
    }


def test_broad_row_access_does_not_export_personal_theme_history(service, principal):
    from shortlist.server.db.models import utcnow

    with service.state.sessions() as session:
        session.add_all(
            [
                Theme(id=1, slug="personal", name="Private history theme"),
                Theme(id=2, slug="unused", name="Unused editorial theme"),
            ]
        )
        session.flush()
        session.add(ThemeHistory(collection_id=1, user_id=1, theme_id=1, state="current", started_at=utcnow()))
        session.commit()
    broad = replace(
        principal, constraints=replace(principal.constraints, include_future_rows=True, include_future_people=True)
    )
    result = service.themes(broad)
    assert [item["id"] for item in result.data["items"]] == [2]
    with pytest.raises(PermissionError):
        service.theme(broad, 1)
    allowed = replace(broad, capabilities=broad.capabilities | {Capability.HISTORY_EXPORT})
    assert service.theme(allowed, 1).data["name"] == "Private history theme"


def test_instance_projects_only_own_lifetime_provider_reservations(service, principal):
    from shortlist.server.assistant.budgets import AssistantBudget

    with service.state.sessions() as session:
        session.add_all(
            [
                AssistantBudget(grant_id=principal.grant_id, provider_calls_reserved=2),
                AssistantBudget(grant_id="different-grant", provider_calls_reserved=99),
            ]
        )
        session.commit()
    bounded = replace(principal, constraints=replace(principal.constraints, max_provider_calls=3))
    quota = service.instance(bounded).data["provider_call_quota"]
    assert (quota["lifetime_limit"], quota["reserved"], quota["remaining"]) == (3, 2, 1)
    reduced = replace(principal, constraints=replace(principal.constraints, max_provider_calls=1))
    assert service.instance(reduced).data["provider_call_quota"]["remaining"] == 0


def test_instance_workflow_reflects_effective_oauth_scope_within_manage_role(service, principal):
    limited_token = replace(
        principal,
        capabilities=frozenset({Capability.INSTANCE_READ}),
        constraints=replace(principal.constraints, basic_access_v1="manage"),
    )
    data = service.instance(limited_token).data
    assert data["access_role"] == "manage"
    assert data["workflow"] == ["discover", "read"]
    assert "provider_call_quota" not in data
