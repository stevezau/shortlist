"""`GET /api/picks/{rating_key}/poster` — a delivered pick's artwork, proxied from the PMS.

Two properties this file exists to hold. The owner's Plex token must never reach the browser (rule
9), and a read that FAILS must never be answered with a picture — a placeholder tile in the SPA and
a missing item on the server have to stay distinguishable, or "why isn't X in my row" becomes
unanswerable from the UI.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
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
    monkeypatch.setattr(picks, "_plex_client", lambda url, token, timeout: client)
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
        monkeypatch.setattr(picks, "_plex_client", lambda url, token, timeout: client)

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

    def test_the_etag_carries_the_artwork_stamp_and_the_browser_revalidates(self, app_client, monkeypatch):
        """Plex puts the artwork's own mtime on the thumb path, so a changed poster changes the ETag.

        The `max-age` has to be short for that to be worth anything: the ETag is built from the
        MEMOISED path, and a week-long cache means the browser never asks, so new artwork would go
        unnoticed until the stale stamp 404'd and the tile broke. `must-revalidate` is what lets the
        stamp do the job."""
        _fake_plex(monkeypatch)

        r = app_client.get("/api/picks/575662/poster")

        assert "1786296858" in r.headers["etag"]
        assert r.headers["cache-control"] == "private, max-age=300, must-revalidate"

    def test_new_artwork_is_picked_up_once_the_memo_expires(self, app_client, monkeypatch):
        """The memo is what makes the ETag stale, so it is what has to expire. Without a TTL, a
        replaced poster stayed wrong until the stale stamp 404'd or the process restarted."""
        from shortlist.server.api import picks

        plex = _fake_plex(monkeypatch)
        first = app_client.get("/api/picks/575662/poster").headers["etag"]

        plex.item_thumb_path.return_value = "/library/metadata/575662/thumb/1799999999"
        # Age the entry rather than patching `time.monotonic`, which is the real clock every other
        # thread and pytest itself are using.
        path, stamp = picks._THUMB_MEMO[575662]
        picks._THUMB_MEMO[575662] = (path, stamp - picks._THUMB_MEMO_TTL_S - 1)

        assert app_client.get("/api/picks/575662/poster").headers["etag"] != first

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

    def test_plex_not_connected_is_a_404_and_no_thread_is_spent_on_it(self, app_client, monkeypatch):
        """The real condition — blank credentials — not a stubbed client. Answered on the event loop
        before the threadpool hop, because there is nothing to ask."""
        from shortlist.server.api import picks

        plex = _fake_plex(monkeypatch)
        with app_client.app.state.sessions() as session:
            SettingsStore(session, app_client.app.state.secrets).set("plex.token", "")
            session.commit()
        picks._CLIENT.clear()

        assert app_client.get("/api/picks/575662/poster").status_code == 404
        plex.item_thumb_path.assert_not_called()


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


class TestTheEventLoopIsNeverBlocked:
    """Every PMS call here blocks: a plexapi `GET /` handshake, a `requests` read, and `http_retry`,
    which `time.sleep`s between retries. Inline on an `async def` handler, one slow PMS would stall
    the whole ASGI loop — SSE progress and every other API call — for `plex.timeout_s` PER IMAGE,
    and a pick list fires ten to twenty at once."""

    def test_the_handler_offloads_every_plex_call_to_the_threadpool(self, app_client, monkeypatch):
        """Asks the RUNNING LOOP, not thread identity.

        Comparing `threading.get_ident()` against the test body's thread is vacuous: `TestClient`
        runs the event loop on a thread of its own, so the test thread is never the loop thread and
        the assertion passes whether or not the work was offloaded (measured both ways). A threadpool
        worker has no running loop and the event-loop thread does — that difference is the property,
        and it is the one that actually breaks when the `run_in_threadpool` hop is removed.
        """
        from shortlist.server.api import picks

        offloaded: list[bool] = []

        def record(*_args, **_kwargs):
            try:
                asyncio.get_running_loop()
                offloaded.append(False)
            except RuntimeError:
                offloaded.append(True)
            return SimpleNamespace(
                item_thumb_path=lambda _key: REAL_THUMB,
                read_artwork=lambda _path: (b"\x89PNG", "image/png"),
            )

        monkeypatch.setattr(picks, "_plex_client", record)

        assert app_client.get("/api/picks/575662/poster").status_code == 200

        assert offloaded, "the client was never built"
        assert all(offloaded), "a blocking PMS call ran on the event loop thread"

    def test_the_client_cache_is_guarded_against_concurrent_builds(self):
        """The check-then-set is only safe because of the lock: the threadpool really does run two
        poster requests on two OS threads, and without it a ten-poster page could build ten
        `PlexServer`s — ten handshakes — and keep the last."""
        from shortlist.server.api import picks

        assert isinstance(picks._CLIENT_LOCK, type(threading.Lock()))
        source = inspect.getsource(picks._plex_client)
        assert "with _CLIENT_LOCK:" in source, "the cache write is not guarded"


def test_the_client_exposes_both_halves():
    """A guard against the endpoint growing its own PMS parsing: the shape knowledge lives in one
    place, and that place is the client."""
    assert callable(PlexClient.item_thumb_path)
    assert callable(PlexClient.read_artwork)
