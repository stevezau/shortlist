"""Shared fixtures for the `tests/integration/test_api_*.py` files split from the old
`test_api.py` (see `.claude/docs/review-backlog.md` §6.5) — full app via TestClient, real
lifespan, tmp SQLite, forged owner session.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, User
from shortlist.server.main import create_app

OWNER_ID = 555000001

# plex.tv `GET /api/v2/user` — the same payload the PIN login (auth.py) and the wizard's capability
# probe already read from a live server; `thumb` is the one key the user sync adds on top.
OWNER_JSON = {
    "id": OWNER_ID,
    "uuid": "abc123",
    "username": "steve",
    "title": "Steve",
    "email": "steve@example.com",
    "thumb": "https://plex.tv/users/abc/avatar",
    "subscription": {"active": True},
}


@pytest.fixture
def client(tmp_path: Path):
    """The one `client` fixture shared by every `test_api_*.py` file: a real app + TestClient,
    a linked server (so owner checks are active), and two pre-seeded users (sarah, mike)."""
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        # Link a server so owner checks are active, and add users.
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="http://pms:32400",
                    token_enc="x",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            session.add(User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True))
            session.add(User(plex_account_id=555000200, username="mike", slug="mike"))
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client
