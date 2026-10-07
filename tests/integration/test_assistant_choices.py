"""Permission-scoped configured-service choices for the assistant."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from shortlist.engine.delivery import row_marker
from shortlist.server.assistant.tools import ChoicesInput
from shortlist.server.assistant_auth import (
    AuthorizationDenied,
    Capability,
    GrantConstraints,
    GrantContext,
    GrantPreset,
)
from shortlist.server.services.assistant_choices import permitted_choices
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


def _principal(
    *,
    libraries: frozenset[str] = frozenset({"1"}),
    destinations: frozenset[str] = frozenset(),
    capabilities: frozenset[Capability] = frozenset({Capability.CONFIG_READ, Capability.CONNECTIONS_MANAGE}),
) -> GrantContext:
    return GrantContext(
        grant_id="choices-grant",
        owner_account_id=1,
        client_id="choices-client",
        name="Choices test",
        preset=GrantPreset.OWNER_AUTOMATION,
        capabilities=capabilities,
        constraints=GrantConstraints(
            library_keys=libraries,
            destination_ids=destinations,
            include_future_people=True,
        ),
        revision=1,
    )


def _collection_hub(title: str, *, on_shelf: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        title=title,
        identifier="custom.collection.1.42",
        promotedToSharedHome=on_shelf,
        promotedToOwnHome=False,
        promotedToRecommended=False,
    )


def test_choices_input_requires_the_matching_selection_shape():
    assert ChoicesInput.model_validate({"kind": "plex_anchors", "library_key": "1"}).library_key == "1"
    for value in (
        {"kind": "plex_anchors"},
        {"kind": "radarr", "library_key": "1"},
        {"kind": "unknown"},
    ):
        with pytest.raises(ValidationError):
            ChoicesInput.model_validate(value)


def test_plex_anchor_choices_page_foreign_collections_after_library_authorization(client: TestClient, monkeypatch):
    """Plex's managed-hub list never exposes marker-titled Shortlist rows as anchors."""

    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("plex.url", "http://pms:32400")
        store.set("plex.token", "token")
        session.commit()

    section = SimpleNamespace(
        key=1,
        managedHubs=lambda: [
            _collection_hub("Popular"),
            _collection_hub("Shared row" + row_marker(9)),
            _collection_hub("Recently Added", on_shelf=False),
        ],
    )
    constructed = []

    class FakePlex:
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))

        def sections(self):
            return [section]

    monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

    result = asyncio.run(
        permitted_choices(client.app.state, _principal(), kind="plex_anchors", library_key="1", limit=1, offset=1)
    )

    assert result.data == {
        "kind": "plex_anchors",
        "items": [{"kind": "plex_anchor", "title": "Recently Added", "on_shelf": False}],
        "next_offset": None,
        "total": 2,
    }
    assert constructed == [(("http://pms:32400", "token"), {"timeout": 8})]
    assert "untrusted" in result.warnings[0].lower()


def test_plex_anchor_choices_reject_an_unapproved_library_before_constructing_a_client(client: TestClient, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Plex was read before the library constraint was authorized")

    monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", forbidden)

    with pytest.raises(AuthorizationDenied):
        asyncio.run(
            permitted_choices(
                client.app.state,
                _principal(libraries=frozenset({"2"})),
                kind="plex_anchors",
                library_key="1",
                limit=25,
                offset=0,
            )
        )


def test_plex_anchor_choices_reject_missing_capability_before_constructing_a_client(client: TestClient, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Plex was read before config.read was authorized")

    monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", forbidden)

    with pytest.raises(AuthorizationDenied):
        asyncio.run(
            permitted_choices(
                client.app.state,
                _principal(capabilities=frozenset({Capability.CONNECTIONS_MANAGE})),
                kind="plex_anchors",
                library_key="1",
                limit=25,
                offset=0,
            )
        )


@pytest.mark.parametrize("service", ["radarr", "sonarr"])
def test_arr_choices_page_configured_profiles_and_folders_after_destination_authorization(
    client: TestClient, monkeypatch, service: str
):
    url = f"https://{service}.example.test/api/"
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set(f"requests.{service}.url", url)
        store.set(f"requests.{service}.apikey", "secret")
        session.commit()

    fake = SimpleNamespace(
        quality_profiles=lambda: [{"id": 1, "name": "HD"}, {"id": 2, "name": "UHD"}],
        root_folders=lambda: [{"id": 3, "path": "/movies"}],
    )
    calls = []

    def make_client(actual_service, target):
        calls.append((actual_service, target))
        return fake

    monkeypatch.setattr("shortlist.engine.clients.arr.make_arr_client", make_client)

    result = asyncio.run(
        permitted_choices(
            client.app.state,
            _principal(destinations=frozenset({url.removesuffix("/")})),
            kind=service,
            library_key=None,
            limit=2,
            offset=1,
        )
    )

    assert result.data == {
        "kind": service,
        "items": [
            {"kind": "quality_profile", "id": 2, "name": "UHD"},
            {"kind": "root_folder", "id": 3, "path": "/movies"},
        ],
        "next_offset": None,
        "total": 3,
    }
    assert [(actual_service, target.url, target.api_key) for actual_service, target in calls] == [
        (service, url.removesuffix("/"), "secret")
    ]


@pytest.mark.parametrize("service", ["radarr", "sonarr"])
def test_arr_choices_reject_an_unapproved_destination_before_constructing_a_client(
    client: TestClient, monkeypatch, service: str
):
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set(f"requests.{service}.url", f"https://{service}.example.test/")
        store.set(f"requests.{service}.apikey", "secret")
        session.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Arr was read before the saved destination was authorized")

    monkeypatch.setattr("shortlist.engine.clients.arr.make_arr_client", forbidden)

    with pytest.raises(AuthorizationDenied):
        asyncio.run(
            permitted_choices(
                client.app.state,
                _principal(destinations=frozenset({"https://other.example.test"})),
                kind=service,
                library_key=None,
                limit=25,
                offset=0,
            )
        )


def test_arr_choices_hide_vendor_failure_details(client: TestClient, monkeypatch):
    secret = "private-vendor-detail"
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("requests.radarr.url", "https://radarr.example.test")
        store.set("requests.radarr.apikey", "key")
        session.commit()

    def broken(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr("shortlist.engine.clients.arr.make_arr_client", broken)

    with pytest.raises(ValueError, match="configured Radarr service") as error:
        asyncio.run(
            permitted_choices(
                client.app.state,
                _principal(destinations=frozenset({"https://radarr.example.test"})),
                kind="radarr",
                library_key=None,
                limit=25,
                offset=0,
            )
        )

    assert secret not in str(error.value)


def _saved_curator(client: TestClient, provider: str = "openai_compatible") -> None:
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("curator.provider", provider)
        store.set("curator.openai_base_url", "https://models.example.test/")
        store.set("curator.api_key", "synthetic-model-key")
        store.set("curator.model", "saved-model")
        session.commit()


@pytest.mark.parametrize(
    ("provider", "destination"),
    [
        ("openai_compatible", "https://models.example.test/v1"),
        ("ollama", "https://models.example.test/v1"),
        ("openai", "https://api.openai.com/v1"),
        ("anthropic", "https://api.anthropic.com"),
        ("google", "https://generativelanguage.googleapis.com"),
    ],
)
def test_curator_model_choices_use_saved_authorized_provider_and_paginate(client, monkeypatch, provider, destination):
    _saved_curator(client, provider)
    calls = []

    def factory(actual_provider, **kwargs):
        calls.append((actual_provider, kwargs))
        return SimpleNamespace(list_models=lambda: ["small", "medium", "large"])

    monkeypatch.setattr("shortlist.engine.curator.make_curator", factory)
    result = asyncio.run(
        permitted_choices(
            client.app.state,
            _principal(destinations=frozenset({destination})),
            kind="curator_models",
            library_key=None,
            limit=1,
            offset=1,
        )
    )
    assert result.data == {
        "kind": "curator_models",
        "provider": provider,
        "items": [{"kind": "curator_model", "id": "medium"}],
        "next_offset": 2,
        "total": 3,
    }
    assert len(calls) == 1 and calls[0][0] == provider
    assert calls[0][1]["api_key"] == "synthetic-model-key"
    assert calls[0][1]["base_url"] == destination
    assert calls[0][1]["model"] == "saved-model"
    assert calls[0][1]["follow_redirects"] is False
    assert "untrusted" in str(result.warnings).lower()
    assert "synthetic-model-key" not in str(result)


@pytest.mark.parametrize("denial", ["destination", "capability"])
def test_curator_model_choices_authorize_before_constructing_provider(client, monkeypatch, denial):
    _saved_curator(client)

    def forbidden(*args, **kwargs):
        pytest.fail("model provider was constructed before authorization")

    monkeypatch.setattr("shortlist.engine.curator.make_curator", forbidden)
    principal = _principal(
        destinations=frozenset(
            {"https://elsewhere.example.test" if denial == "destination" else "https://models.example.test/v1"}
        ),
        capabilities=frozenset({Capability.CONFIG_READ})
        if denial == "capability"
        else frozenset({Capability.CONNECTIONS_MANAGE}),
    )
    with pytest.raises(AuthorizationDenied):
        asyncio.run(
            permitted_choices(client.app.state, principal, kind="curator_models", library_key=None, limit=25, offset=0)
        )


def test_curator_model_choices_hide_vendor_failure_and_never_generate(client, monkeypatch):
    _saved_curator(client)

    def broken():
        raise RuntimeError("synthetic-model-key PRIVATE_REMOTE_BODY")

    monkeypatch.setattr(
        "shortlist.engine.curator.make_curator", lambda *args, **kwargs: SimpleNamespace(list_models=broken)
    )
    result = asyncio.run(
        permitted_choices(
            client.app.state,
            _principal(destinations=frozenset({"https://models.example.test/v1"})),
            kind="curator_models",
            library_key=None,
            limit=25,
            offset=0,
        )
    )
    assert result.data["items"] == [] and result.data["next_offset"] is None
    assert result.data["total"] == 0
    assert "PRIVATE_REMOTE_BODY" not in str(result) and "synthetic-model-key" not in str(result)


def test_curator_model_choices_schema_accepts_only_saved_provider_selection():
    assert ChoicesInput.model_validate({"kind": "curator_models"}).kind == "curator_models"
    for extra in (
        {"library_key": "1"},
        {"url": "https://unapproved.test"},
        {"api_key": "secret"},
        {"provider": "openai"},
        {"limit": 101},
    ):
        with pytest.raises(ValidationError):
            ChoicesInput.model_validate({"kind": "curator_models", **extra})
