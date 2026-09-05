"""`GET /api/picks/{rating_key}/poster` — a delivered pick's artwork, proxied from the PMS.

Two properties this file exists to hold. The owner's Plex token must never reach the browser (rule
9), and a read that FAILS must never be answered with a picture — a placeholder tile in the SPA and
a missing item on the server have to stay distinguishable, or "why isn't X in my row" becomes
unanswerable from the UI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from plexapi.exceptions import NotFound

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server
from shortlist.server.main import create_app
from shortlist.server.settings_store import SettingsStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

OWNER_ID = 555000001

#: A real recorded PMS thumb path, lifted verbatim from `pms_collections_listing.json` (PMS 1.43.3,
#: recorded 2026-08-10). The trailing number is the artwork's own stamp, which is what makes the
#: ETag below self-invalidating.
REAL_THUMB = "/library/metadata/575662/thumb/1786296858"


@pytest.fixture(autouse=True)
def _clear_thumb_memo():
    """The thumb-path memo is module state that outlives a test.

    Without this, a test that resolved a path leaves it behind and the NEXT test's assertion about
    whether Plex was called depends on which order the file ran in — passing today and failing the
    day a test is inserted above it.
    """
    from shortlist.server.api import picks

    picks._THUMB_MEMO.clear()
    picks._CLIENT.clear()
    yield
    picks._THUMB_MEMO.clear()
    picks._CLIENT.clear()


@pytest.fixture
def app_client(tmp_path):
    """The real app, a linked server, and Plex credentials in settings."""
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
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
            store = SettingsStore(session, app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "PLEXTOKEN")
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


def _fake_plex(monkeypatch, *, thumb: str | None = REAL_THUMB) -> MagicMock:
    """Stand in for the module's PMS client, recording every call it receives."""
    from shortlist.server.api import picks

    client = MagicMock()
    client.item_thumb_path.return_value = thumb
    client.read_artwork.return_value = (b"\x89PNG\r\n\x1a\n", "image/png")
    monkeypatch.setattr(picks, "_plex_client", lambda store: client)
    return client


class TestTheClientReadsTheThumbPath:
    """`PlexClient.item_thumb_path` — plexapi owns the `/library/metadata/{key}` shape, not us."""

    def test_it_returns_the_recorded_thumb_shape(self, mock_plex):
        mock_plex._server.fetchItems.return_value = [SimpleNamespace(ratingKey=575662, thumb=REAL_THUMB)]

        assert mock_plex.item_thumb_path(575662) == REAL_THUMB

    def test_an_item_plex_no_longer_holds_has_no_artwork(self, mock_plex):
        """Recorded behaviour: Plex 404s only when NOT ONE requested key exists
        (`pms_metadata_batch_partial.json`), and `fetch_items` turns that into an empty list."""
        mock_plex._server.fetchItems.side_effect = NotFound("gone")

        assert mock_plex.item_thumb_path(1) is None

    def test_an_item_with_no_artwork_is_not_an_error(self, mock_plex):
        mock_plex._server.fetchItems.return_value = [SimpleNamespace(ratingKey=1, thumb=None)]

        assert mock_plex.item_thumb_path(1) is None

    @pytest.mark.parametrize(
        "thumb",
        [
            "https://evil.example/steal",  # an absolute URL is not a path on this server
            "//evil.example/steal",  # protocol-relative, which `startswith("/")` alone admits
            "library/metadata/1/thumb/2",  # no leading slash: would resolve against the base URL
        ],
    )
    def test_a_thumb_that_is_not_a_path_on_this_server_is_refused(self, mock_plex, thumb):
        """No recorded fixture says a `thumb` is always server-relative, so the code does not assume
        it. Following one would make this endpoint fetch from a host the owner never pointed us at."""
        mock_plex._server.fetchItems.return_value = [SimpleNamespace(ratingKey=1, thumb=thumb)]

        assert mock_plex.item_thumb_path(1) is None

    def test_reading_artwork_refuses_a_path_that_is_not_server_relative(self, mock_plex):
        mock_plex._token = "PLEXTOKEN"
        mock_plex._timeout = 5

        with pytest.raises(ValueError, match="relative to this server"):
            mock_plex.read_artwork("https://evil.example/steal")

    @respx.mock
    def test_the_token_is_sent_as_a_header_and_never_in_the_url(self, mock_plex):
        """Rule 9. These bytes are handed to a browser, and a token in the URL is a token in the
        browser's history and in every proxy log between here and there."""
        mock_plex._token = "PLEXTOKEN"
        mock_plex._timeout = 5
        mock_plex._server.url.return_value = f"http://pms:32400{REAL_THUMB}"
        route = respx.get(f"http://pms:32400{REAL_THUMB}").mock(
            return_value=httpx.Response(200, content=b"JPEGBYTES", headers={"content-type": "image/jpeg"})
        )

        body, content_type = mock_plex.read_artwork(REAL_THUMB)

        assert (body, content_type) == (b"JPEGBYTES", "image/jpeg")
        request = route.calls[0].request
        assert request.headers["X-Plex-Token"] == "PLEXTOKEN"
        assert "PLEXTOKEN" not in str(request.url)
        # `includeToken=False` is the kwarg the SUT is responsible for; without it plexapi puts the
        # token in the query string and the header assertion above still passes.
        assert mock_plex._server.url.call_args.kwargs["includeToken"] is False


class TestPickPoster:
    def test_a_rating_key_of_zero_is_a_404_and_plex_is_never_called(self, app_client, monkeypatch):
        """`picker.py` writes `c.rating_key or 0`, so `0` is a real stored value meaning "not
        matched". Asking Plex about it would spend a round-trip per unmatched pick on every page."""
        plex = _fake_plex(monkeypatch)

        r = app_client.get("/api/picks/0/poster")

        assert r.status_code == 404
        plex.item_thumb_path.assert_not_called()

    def test_a_negative_rating_key_is_a_404_and_plex_is_never_called(self, app_client, monkeypatch):
        plex = _fake_plex(monkeypatch)

        assert app_client.get("/api/picks/-5/poster").status_code == 404
        plex.item_thumb_path.assert_not_called()

    def test_an_unknown_rating_key_returns_404_rather_than_an_empty_image(self, app_client, monkeypatch):
        _fake_plex(monkeypatch, thumb=None)

        r = app_client.get("/api/picks/999/poster")

        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json"), "a 404 must not be a picture"

    def test_a_plex_failure_returns_502_rather_than_zero_bytes(self, app_client, monkeypatch):
        from shortlist.server.api import picks

        client = MagicMock()
        client.item_thumb_path.side_effect = httpx.ConnectError("PMS down")
        monkeypatch.setattr(picks, "_plex_client", lambda store: client)

        r = app_client.get("/api/picks/575662/poster")

        assert r.status_code == 502
        assert not r.content.startswith(b"\x89PNG"), "an outage must never be answered with a picture"

    def test_it_streams_the_pms_bytes_with_the_pms_content_type(self, app_client, monkeypatch):
        plex = _fake_plex(monkeypatch)

        r = app_client.get("/api/picks/575662/poster")

        assert r.status_code == 200
        assert r.content == b"\x89PNG\r\n\x1a\n"
        assert r.headers["content-type"] == "image/png"
        plex.read_artwork.assert_called_once_with(REAL_THUMB)

    def test_the_etag_carries_the_artwork_stamp_so_new_art_invalidates_it(self, app_client, monkeypatch):
        """Plex puts the artwork's own mtime on the thumb path. Keying the ETag on it means replacing
        a poster in Plex busts the browser cache for free — no cache-busting query needed."""
        _fake_plex(monkeypatch)

        r = app_client.get("/api/picks/575662/poster")

        assert "1786296858" in r.headers["etag"]
        assert r.headers["cache-control"] == "private, max-age=604800"

    def test_a_matching_if_none_match_returns_304_and_reads_no_bytes_from_plex(self, app_client, monkeypatch):
        plex = _fake_plex(monkeypatch)
        etag = app_client.get("/api/picks/575662/poster").headers["etag"]
        plex.read_artwork.reset_mock()

        r = app_client.get("/api/picks/575662/poster", headers={"If-None-Match": etag})

        assert r.status_code == 304
        assert not r.content
        plex.read_artwork.assert_not_called(), "a 304 must not cost a PMS image read"

    def test_the_endpoint_refuses_a_request_without_an_owner_session(self, app_client, monkeypatch):
        plex = _fake_plex(monkeypatch)
        app_client.cookies.clear()

        r = app_client.get("/api/picks/575662/poster")

        assert r.status_code in (401, 403)
        plex.item_thumb_path.assert_not_called()

    def test_plex_not_connected_is_a_404_not_a_500(self, app_client, monkeypatch):
        from shortlist.server.api import picks

        monkeypatch.setattr(picks, "_plex_client", lambda store: None)

        assert app_client.get("/api/picks/575662/poster").status_code == 404


class TestTheRecordedEnvelope:
    def test_the_batch_read_this_leans_on_is_recorded_from_a_real_server(self):
        """Rule 11, stated honestly. There is no recorded single-item `/library/metadata/{key}`
        response carrying a `thumb`, which is why `item_thumb_path` goes through plexapi rather than
        parsing JSON here. What IS recorded is the envelope and the partial-batch behaviour."""
        recorded = json.loads((FIXTURES / "pms_metadata_batch_partial.json").read_text())

        # Provenance, not the server's name: the `_recorded` note must say which PMS BUILD answered,
        # which is what makes it a capture rather than something hand-authored to agree with us.
        assert re.search(r"PMS \d+\.\d+", recorded["_recorded"]), "fixture provenance: not a real capture"
        assert recorded["MediaContainer"]["Metadata"][0]["ratingKey"], "the envelope `fetch_items` reads"


def test_the_client_exposes_both_halves():
    """A guard against the endpoint growing its own PMS parsing: the shape knowledge lives in one
    place, and that place is the client."""
    assert callable(PlexClient.item_thumb_path)
    assert callable(PlexClient.read_artwork)
