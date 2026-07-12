"""Boot the real app and drive the auth gates over HTTP, in each of the three claim states.

Unit-level auth tests fake `app.state`; these boot `create_app` so the wiring — which state each
router is in, whether a seeded secret counts, whether the token ever leaves — is exercised for
real. It is the layer that would have caught the seeded-Tautulli hole.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from rowarr.server.auth import CSRF_HEADER
from rowarr.server.main import create_app

pytestmark = pytest.mark.integration


def _client(tmp_path: Path, **env) -> TestClient:
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as client:
        client._env = env
        yield_client = client
    return yield_client


def _app(tmp_path: Path, **env):
    """A booted app with `env` seeded on first boot (the ENV_SEEDS path)."""
    import os

    app = create_app(config_dir=tmp_path)
    # Seeding happens in the lifespan from os.environ; set them for the startup, then clear.
    for key, value in env.items():
        os.environ[key] = value
    try:
        client = TestClient(app)
        client.__enter__()
    finally:
        for key in env:
            os.environ.pop(key, None)
    return client


class TestASeededTautulliKeyClosesTheGate:
    """HIGH from review: `holds_secrets` used to count only the Plex token, so an instance seeded
    with just a Tautulli key booted 'empty' — and its owner-only routes opened to anonymous
    callers, one of whom could POST /settings/test/tautulli and have the key mailed to their host.
    """

    def test_an_instance_seeded_with_only_a_tautulli_key_demands_a_login(self, tmp_path: Path):
        client = _app(tmp_path, TAUTULLI_URL="http://taut", TAUTULLI_APIKEY="secret-key")
        try:
            session = client.get("/api/auth/session").json()
            assert session["login_required"] is True, "a seeded secret must require a sign-in"
        finally:
            client.__exit__(None, None, None)

    def test_the_settings_routes_are_owner_only_even_before_a_server_is_linked(self, tmp_path: Path):
        client = _app(tmp_path, TAUTULLI_URL="http://taut", TAUTULLI_APIKEY="secret-key")
        try:
            # No session: the connection-test endpoint that would exfiltrate the key is refused
            # outright — settings is never open, claimed or not.
            r = client.post("/api/settings/test/tautulli", headers={CSRF_HEADER: "1"})
            assert r.status_code in (401, 403), r.text
        finally:
            client.__exit__(None, None, None)


class TestATrulyEmptyInstanceOpensTheWizard:
    def test_no_secret_no_server_means_no_login_required(self, tmp_path: Path):
        client = _app(tmp_path)
        try:
            assert client.get("/api/auth/session").json()["login_required"] is False
            # ...the wizard RENDERS without a session: GET /state is open.
            assert client.get("/api/setup/state").status_code == 200
            # ...but WRITING progress needs one (nothing worth saving happens before connect), and
            # settings is never open.
            assert client.put("/api/setup/state", json={"step": 3}, headers={CSRF_HEADER: "1"}).status_code == 401
            assert client.get("/api/settings").status_code == 401
        finally:
            client.__exit__(None, None, None)
