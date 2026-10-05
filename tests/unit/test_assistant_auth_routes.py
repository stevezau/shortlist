"""Browser-facing assistant auth route behavior."""

from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shortlist.server.assistant_auth.routes import create_oauth_router


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
