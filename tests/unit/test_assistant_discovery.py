"""Assistant discovery must enforce scope before returning configuration or identities."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.discovery import DiscoveryService
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantContext, GrantPreset
from shortlist.server.db.models import Base, Collection, CollectionAudience, Setting, Theme, ThemeHistory, User


@pytest.fixture
def service():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
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
    engine.dispose()


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
