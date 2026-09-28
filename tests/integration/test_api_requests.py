"""API contract tests: the request inbox size cap, and the "wanted by" name filter that must
reach past it."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.db.models import User
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


class TestRequestInboxIsBounded:
    """The sent log only grows — every run that wants a missing title adds a row — so an unbounded
    read is a query that gets slower for ever and eventually times out the page."""

    def test_the_inbox_is_capped_and_keeps_pending_first(self, client: TestClient):
        from shortlist.server.api.requests import MAX_INBOX
        from shortlist.server.db.models import RequestCandidate

        with client.app.state.sessions() as session:
            # More sent history than the cap, plus a handful of pending the owner must still see.
            for i in range(MAX_INBOX + 25):
                session.add(
                    RequestCandidate(
                        tmdb_id=1000 + i,
                        media_type="movie",
                        title=f"Sent {i}",
                        status="sent",
                        demand=1,
                        rating=5.0,
                    )
                )
            for i in range(5):
                session.add(
                    RequestCandidate(
                        tmdb_id=1 + i,  # (tmdb_id, media_type) is unique
                        media_type="movie",
                        title=f"Pending {i}",
                        status="pending",
                        demand=9,
                        rating=9.0,
                    )
                )
            session.commit()

        rows = client.get("/api/requests").json()

        assert len(rows) == MAX_INBOX, "the read must be bounded"
        # The cap can only ever truncate the OLDEST history — never the things awaiting a decision.
        assert [r["title"] for r in rows[:5]] == [f"Pending {i}" for i in range(5)]


def _seed_buried_titles(client: TestClient, owner: str, buried: int = 3) -> None:
    """Fill the inbox past :data:`MAX_INBOX` with titles nobody is recorded as wanting, then add
    ``buried`` titles for ``owner`` that sort BELOW the cut (lowest demand, lowest rating).

    That is the shape the filter has to survive: everything ``owner`` wants is past the cap, so a
    filter applied to the response can only ever return an empty list.
    """
    from shortlist.server.api.requests import MAX_INBOX
    from shortlist.server.db.models import RequestCandidate

    with client.app.state.sessions() as session:
        for i in range(MAX_INBOX):
            session.add(
                RequestCandidate(
                    tmdb_id=1000 + i,
                    media_type="movie",
                    title=f"Filler {i}",
                    status="sent",
                    demand=5,
                    rating=8.0,
                    wanters=[],
                )
            )
        for i in range(buried):
            session.add(
                RequestCandidate(
                    tmdb_id=1 + i,  # (tmdb_id, media_type) is unique
                    media_type="movie",
                    title=f"{owner} Buried {i}",
                    status="sent",
                    demand=1,
                    rating=1.0,
                    wanters=[owner, "mike"] if i == 0 else [owner],
                )
            )
        session.commit()


class TestWantedByFilter:
    """ "Wanted by" narrows the inbox to the people named, SERVER-side — the whole point being to
    answer "what does this new person still need?" across the whole history, not just the most
    recent :data:`MAX_INBOX` rows the page happened to load."""

    def test_the_filter_reaches_past_the_cap(self, client: TestClient):
        from shortlist.server.api.requests import MAX_INBOX

        _seed_buried_titles(client, "sarah")

        unfiltered = client.get("/api/requests").json()
        filtered = client.get("/api/requests", params={"wanted_by": "sarah"}).json()

        # Sarah's titles are nowhere on the unfiltered page — the cap ate them.
        assert len(unfiltered) == MAX_INBOX
        assert not [r for r in unfiltered if "sarah" in r["wanters"]]
        # ...and every one of them comes back when she is named, because the filter runs first.
        assert [r["title"] for r in filtered] == [f"sarah Buried {i}" for i in range(3)]

    def test_several_names_are_a_union(self, client: TestClient):
        _seed_buried_titles(client, "sarah")

        filtered = client.get("/api/requests", params={"wanted_by": ["sarah", "mike"]}).json()

        # Mike wanted only the first title, and it is listed once, not twice.
        assert [r["title"] for r in filtered] == [f"sarah Buried {i}" for i in range(3)]

        mike_only = client.get("/api/requests", params={"wanted_by": "mike"}).json()
        assert [r["title"] for r in mike_only] == ["sarah Buried 0"]

    def test_a_name_nobody_carries_returns_nothing(self, client: TestClient):
        """A filter, not a hint: an unmatched name must empty the list rather than fall back to all."""
        _seed_buried_titles(client, "sarah")

        assert client.get("/api/requests", params={"wanted_by": "nobody"}).json() == []

    def test_no_name_is_the_whole_inbox_unchanged(self, client: TestClient):
        """The default must mean exactly what it meant before the parameter existed."""
        from shortlist.server.api.requests import MAX_INBOX

        _seed_buried_titles(client, "sarah")

        # An empty repeated parameter is the same as omitting it — the SPA sends no `wanted_by` at
        # all when no name is picked, but a blank one must not empty the inbox either.
        assert len(client.get("/api/requests").json()) == MAX_INBOX
        assert len(client.get("/api/requests", params={"wanted_by": ""}).json()) == MAX_INBOX

    def test_the_status_order_survives_the_filter(self, client: TestClient):
        """Filtering must not reorder the inbox: pending is still the owner's to-do list, on top."""
        from shortlist.server.db.models import RequestCandidate

        with client.app.state.sessions() as session:
            for i, status in enumerate(("rejected", "sent", "pending")):
                session.add(
                    RequestCandidate(
                        tmdb_id=10 + i,
                        media_type="movie",
                        title=f"Sarah {status}",
                        status=status,
                        demand=1,
                        rating=5.0,
                        wanters=["sarah"],
                    )
                )
            session.commit()

        rows = client.get("/api/requests", params={"wanted_by": "sarah"}).json()

        assert [r["status"] for r in rows] == ["pending", "sent", "rejected"]


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _connect_overseerr(client: TestClient) -> None:
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("requests.overseerr.url", "http://seerr")
        store.set("requests.overseerr.apikey", "k")
        session.commit()


class TestRowSourcesSetupCheck:
    """`GET /requests/row-sources` is the owner's "is the requests row going to work?" screen: which
    source is connected, whether Overseerr tags what it sends, and who is linked to an account."""

    def test_row_sources_reports_each_source_and_who_is_linked(self, client: TestClient):
        _connect_overseerr(client)
        with client.app.state.sessions() as session:
            session.query(User).filter_by(username="mike").update({"enabled": True})
            session.commit()
            ids = {u.username: u.id for u in session.query(User).all()}
        reqs = json.loads((FIXTURES / "overseerr_requests_page.json").read_text())
        # Sarah is the recorded requester 10 (one completed show); the request carries her Plex id.
        # Mike has an Overseerr account but asked for nothing — he must still read as linked, so
        # "linked" is proven to mean "has an account", not "has a request".
        for r in reqs["results"]:
            if r["requestedBy"]["id"] == 10:
                r["requestedBy"]["plexId"] = 555000100
        users = {
            "pageInfo": {"results": 2, "pages": 1},
            "results": [
                {"id": 10, "plexId": 555000100, "displayName": "Sarah"},
                {"id": 99, "plexId": 555000200, "displayName": "Mike"},
            ],
        }
        with respx.mock:
            respx.get("http://seerr/api/v1/request").mock(return_value=httpx.Response(200, json=reqs))
            respx.get("http://seerr/api/v1/user").mock(return_value=httpx.Response(200, json=users))
            respx.get("http://seerr/api/v1/settings/radarr").mock(
                return_value=httpx.Response(200, json=[{"name": "r", "is4k": False, "tagRequests": True}])
            )
            respx.get("http://seerr/api/v1/settings/sonarr").mock(return_value=httpx.Response(200, json=[]))
            respx.get("http://seerr/api/v1/media").mock(
                return_value=httpx.Response(200, json={"pageInfo": {"results": 0}, "results": []})
            )
            r = client.get("/api/requests/row-sources")

        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["overseerr"], out["radarr"], out["sonarr"], out["complete"]) == ("connected", "off", "off", True)
        assert out["servers"] == [{"kind": "radarr", "name": "r", "is4k": False, "tag_requests": True}]
        assert out["seerr_requests"] == len(reqs["results"])
        assert out["seerr_linked"] == 1  # of the requesters, only Sarah maps to someone on the roster
        people = {p["display_name"]: p for p in out["people"]}
        assert people["sarah"] == {"user_id": ids["sarah"], "display_name": "sarah", "linked": True, "ready": 1}
        assert people["mike"] == {"user_id": ids["mike"], "display_name": "mike", "linked": True, "ready": 0}

    def test_row_sources_radarr_alone_is_connected_even_when_its_tags_need_overseerr(self, client: TestClient):
        """The Arr-tags-without-Overseerr setup this screen exists for: the engine's advice names
        Overseerr, and that must not read as a Radarr outage."""
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("requests.radarr.url", "http://radarr")
            store.set("requests.radarr.apikey", "k")
            session.commit()
        with respx.mock:
            respx.get("http://radarr/api/v3/tag").mock(
                return_value=httpx.Response(200, json=[{"id": 1, "label": "10-sarah"}])
            )
            respx.get("http://radarr/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
            r = client.get("/api/requests/row-sources")

        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["overseerr"], out["radarr"], out["sonarr"], out["complete"]) == ("off", "connected", "off", True)
        assert any("Overseerr isn't connected" in p for p in out["problems"])

    def test_row_sources_says_off_when_nothing_is_configured(self, client: TestClient):
        out = client.get("/api/requests/row-sources").json()

        assert (out["overseerr"], out["radarr"], out["sonarr"], out["complete"]) == ("off", "off", "off", True)
        assert out["people"] and all(p["linked"] is False and p["ready"] == 0 for p in out["people"])

    def test_row_sources_says_unreachable_when_overseerr_is_down(self, client: TestClient):
        _connect_overseerr(client)
        with respx.mock:
            respx.get(url__startswith="http://seerr/").mock(side_effect=httpx.ConnectError("refused"))
            r = client.get("/api/requests/row-sources")

        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["overseerr"], out["complete"]) == ("unreachable", False)
        assert any(p.startswith("Overseerr could not be read") for p in out["problems"])
