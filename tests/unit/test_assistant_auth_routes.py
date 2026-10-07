"""Browser-facing assistant auth route behavior."""

from collections.abc import Iterator
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.models import AssistantGrant, AssistantOAuthToken
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.assistant_auth.routes import ConstraintsIn, ConstraintsPatchIn, create_oauth_router
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
from shortlist.server.db.models import Base
from tests.db_helpers import disposing_engine


@pytest.fixture
def oauth_registration_client() -> Iterator[tuple[TestClient, AssistantAuthRepository, sessionmaker]]:
    """Build the real registration router against a disposable repository."""
    with disposing_engine(
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"r" * 32))
        app = FastAPI()
        app.include_router(create_oauth_router(repository=repository))
        with TestClient(app) as client:
            yield client, repository, sessions


def _registration_payload(**metadata: object) -> dict[str, object]:
    return {
        "client_name": "Codex",
        "redirect_uris": ["http://127.0.0.1:43219/callback/test"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **metadata,
    }


@pytest.mark.parametrize("model", [ConstraintsIn, ConstraintsPatchIn])
@pytest.mark.parametrize("field", ["person_ids", "include_future_people", "access_version"])
def test_public_grant_constraints_reject_private_people_compatibility_fields(model, field: str) -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        model.model_validate({field: [] if field == "person_ids" else True})


def test_owner_create_constraint_defaults_distinguish_empty_and_selected_resources() -> None:
    assert ConstraintsIn().to_domain().include_future_rows is True
    assert ConstraintsIn().to_domain().include_future_libraries is True
    assert ConstraintsIn(row_ids={7}).to_domain().include_future_rows is False
    assert ConstraintsIn(library_keys={"7"}).to_domain().include_future_libraries is False
    assert ConstraintsIn(row_ids={7}, include_future_rows=True).to_domain().include_future_rows is True
    assert ConstraintsIn(library_keys={"7"}, include_future_libraries=True).to_domain().include_future_libraries is True


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {
            "scope": " ".join(sorted(capability.value for capability in ASSISTANT_CAPABILITIES)),
            "application_type": "native",
        },
        {"future_extension": {"capabilities": ["secrets.read"], "approved": True}},
    ],
)
def test_registration_ignores_rfc7591_extension_metadata_without_granting_access(
    oauth_registration_client, metadata
) -> None:
    client, repository, sessions = oauth_registration_client

    response = client.post("/assistant/oauth/register", json=_registration_payload(**metadata))

    assert response.status_code == 201, response.json()
    assert set(response.json()).isdisjoint(metadata)
    assert repository.oauth_client_count() == 1
    with sessions() as session:
        assert session.query(AssistantGrant).count() == 0
        assert session.query(AssistantOAuthToken).count() == 0


@pytest.mark.parametrize(
    "metadata,status_code",
    [
        ({"client_name": ""}, 422),
        ({"client_name": []}, 422),
        ({"redirect_uris": []}, 422),
        ({"redirect_uris": "http://127.0.0.1:43219/callback"}, 422),
        ({"redirect_uris": ["http://example.invalid/callback"]}, 400),
        ({"redirect_uris": ["https://user@example.invalid/callback"]}, 400),
        ({"redirect_uris": ["https://example.invalid/callback#fragment"]}, 400),
        ({"redirect_uris": ["com.example:/callback"]}, 400),
        ({"token_endpoint_auth_method": "client_secret_post"}, 400),
        ({"grant_types": ["authorization_code"]}, 400),
        ({"response_types": ["token"]}, 400),
    ],
)
def test_registration_rejects_invalid_known_metadata_without_creating_a_client(
    oauth_registration_client, metadata, status_code
) -> None:
    client, repository, _sessions = oauth_registration_client

    response = client.post("/assistant/oauth/register", json=_registration_payload(**metadata))

    assert response.status_code == status_code, response.json()
    assert repository.oauth_client_count() == 0


@pytest.mark.parametrize("base_path", ["", "/shortlist"])
def test_authorize_redirect_resolves_to_spa_consent_under_the_same_base_path(base_path: str) -> None:
    oauth_app = FastAPI()
    oauth_app.include_router(create_oauth_router())
    app = oauth_app
    if base_path:
        app = FastAPI()
        app.mount(base_path, oauth_app)

    query = "client_id=public-client&state=opaque-state"
    with TestClient(app, base_url="https://media.example") as client:
        response = client.get(
            f"{base_path}/assistant/oauth/authorize?{query}",
            follow_redirects=False,
        )

    assert response.status_code == 303
    resolved = urljoin(str(response.request.url), response.headers["location"])
    assert resolved == f"https://media.example{base_path}/assistant/consent?{query}"
