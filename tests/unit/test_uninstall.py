"""Uninstall: dry-run preview vs real restore+delete, label gating, per-user audit events."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from rowarr.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from rowarr.server.db.models import Event, RestrictionSnapshotRow, Server, User
from rowarr.server.main import create_app

OWNER_ID = 555000001


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="u",
                    token_enc="x",
                    version="1.43.3.10793",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            user = User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True)
            session.add(user)
            session.commit()
            session.add(
                RestrictionSnapshotRow(
                    user_id=user.id,
                    reason="initial",
                    filters_before={"filterMovies": "contentRating!=R", "filterTelevision": ""},
                    filters_after={},
                )
            )
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


def fake_context(monkeypatch, client: TestClient) -> tuple[MagicMock, MagicMock]:
    """Stub build_context with a plex/plextv pair carrying one owned + one foreign collection.

    plextv persists writes so the engine's post-restore read-back verification is exercised
    for real rather than mocked away.
    """
    live_filters = {
        "filterAll": "",
        "filterMovies": "contentRating!=R|label!=Rowarr_mike",
        "filterTelevision": "",
        "filterMusic": "",
        "filterPhotos": "",
    }
    plextv = MagicMock()
    plextv.get_user.side_effect = lambda _id: SimpleNamespace(filters=dict(live_filters))
    plextv.update_user_filters.side_effect = lambda _id, fields: live_filters.update(fields)
    plex = MagicMock()
    ours = MagicMock(ratingKey=1)
    ours.title = "✨ Picked for You"
    ours.labels = [SimpleNamespace(tag="Rowarr_sarah")]
    kometa = MagicMock(ratingKey=2)
    kometa.title = "Kometa Trending"
    kometa.labels = [SimpleNamespace(tag="Overlay")]
    section = MagicMock()
    section.collections.return_value = [ours, kometa]
    plex.sections.return_value = [section]

    def build_context(*, dry_run: bool):
        return SimpleNamespace(plex=plex, plextv=plextv, config=SimpleNamespace(dry_run=dry_run))

    monkeypatch.setattr(client.app.state.run_service, "build_context", build_context)
    return plex, plextv


class TestUninstall:
    def test_wrong_confirmation_rejected(self, client: TestClient):
        assert client.post("/api/system/uninstall", json={"confirm": "yes"}).status_code == 422

    def test_dry_run_previews_without_writing(self, client: TestClient, monkeypatch):
        plex, plextv = fake_context(monkeypatch, client)

        r = client.post("/api/system/uninstall", json={"dry_run": True})

        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is True
        assert body["collections_deleted"] == ["✨ Picked for You"]  # ours only — Kometa untouched
        assert body["filters_restored"] == 1
        assert "Preview only" in body["message"]
        plex.delete_owned_collection.assert_not_called()
        plextv.update_user_filters.assert_not_called()  # engine restore honored dry_run

    def test_real_uninstall_restores_filters_and_deletes_only_ours(self, client: TestClient, monkeypatch):
        plex, plextv = fake_context(monkeypatch, client)

        r = client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})

        assert r.status_code == 200
        body = r.json()
        assert body["dry_run"] is False
        assert body["filters_restored"] == 1
        assert body["collections_deleted"] == ["✨ Picked for You"]
        assert "as we found it" in body["message"]
        # Filters restored to the snapshot values, byte-for-byte.
        call = plextv.update_user_filters.call_args
        assert call.args[1] == {"filterMovies": "contentRating!=R"}
        # Only the rowarr-labeled collection was deleted; the label gate is re-checked inside.
        assert plex.delete_owned_collection.call_count == 1
        deleted = plex.delete_owned_collection.call_args.args[0]
        assert deleted.title == "✨ Picked for You"

    def test_per_user_audit_events_recorded(self, client: TestClient, monkeypatch):
        fake_context(monkeypatch, client)
        client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"})
        with client.app.state.sessions() as session:
            per_user = session.query(Event).filter_by(scope="uninstall.user").all()
            summary = session.query(Event).filter_by(scope="system.uninstall").all()
        assert len(per_user) == 1
        assert per_user[0].message["user"] == "sarah"
        assert per_user[0].message["restored_to"]["filterMovies"] == "contentRating!=R"
        assert len(summary) == 1
