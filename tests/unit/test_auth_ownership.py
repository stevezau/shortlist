"""Only the owner of a Plex server may sign in, and only their OWN server may be linked.

Two separate gates, because a friend with a share on someone else's server passes the wrong one:

* `poll_pin` on an UNCLAIMED instance — there is no stored owner to compare against yet, so plex.tv
  is asked directly whether this account owns any server at all. Someone who owns none can never
  legitimately finish setup, so they are turned away instead of handed a session.
* `POST /setup/link` — the request carries `owner_account_id`, but that was only ever checked
  against the caller's own session, which is circular and passes for anybody. The machine id has to
  be a server plex.tv says the account OWNS.

The repo has no async test runner, so the handlers are driven with `asyncio.run`; respx patches the
httpx transport globally, so it holds across the loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import HTTPException, Response

from shortlist.server.api.setup import LinkRequest, link_server
from shortlist.server.auth import PLEXTV, owned_machine_ids, poll_pin

OWNED = {
    "name": "SFLIX",
    "clientIdentifier": "machine-owned",
    "provides": "server",
    "owned": True,
    "connections": [],
}
SHARED = {
    "name": "A Friend's Server",
    "clientIdentifier": "machine-shared",
    "provides": "server",
    "owned": False,
    "connections": [],
}
# A player/controller, not a server — it can be "owned" and is still not something to link.
PLAYER = {"name": "Living Room", "clientIdentifier": "machine-player", "provides": "player", "owned": True}

RESOURCES = f"{PLEXTV}/api/v2/resources"


def _resources(*servers, **kwargs):
    return respx.get(url__startswith=RESOURCES).mock(
        return_value=httpx.Response(200, json=list(servers)) if not kwargs else None, **kwargs
    )


class TestOwnedMachineIds:
    @respx.mock
    def test_returns_only_servers_the_account_owns(self):
        _resources(OWNED, SHARED, PLAYER)
        assert asyncio.run(owned_machine_ids("client-1", "tok")) == {"machine-owned"}

    @respx.mock
    def test_sends_the_token_so_plextv_answers_for_the_right_account(self):
        route = _resources(OWNED)
        asyncio.run(owned_machine_ids("client-1", "sekrit"))
        assert route.calls.last.request.headers["X-Plex-Token"] == "sekrit"

    @respx.mock
    def test_raises_rather_than_reporting_no_servers_when_plextv_fails(self):
        """The caller must fail closed. Returning an empty set on an error would read identically to
        "this account owns nothing", and an HTTP 500 from plex.tv is not an ownership answer."""
        respx.get(url__startswith=RESOURCES).mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPError):
            asyncio.run(owned_machine_ids("client-1", "tok"))


def _pin_routes(*, account_id: int = 42) -> None:
    respx.get(url__startswith=f"{PLEXTV}/api/v2/pins/").mock(
        return_value=httpx.Response(200, json={"authToken": "user-token"})
    )
    respx.get(f"{PLEXTV}/api/v2/user").mock(
        return_value=httpx.Response(200, json={"id": account_id, "username": "friend"})
    )


def _request(owner: int | None):
    state = SimpleNamespace(
        client_id="client-1",
        session_secret="s",
        pending_plex_tokens={},
        owner_account_id=lambda: owner,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), url=SimpleNamespace(scheme="http"))


class TestLoginRejectsNonOwners:
    @respx.mock
    def test_rejects_an_account_that_owns_no_plex_server(self):
        """The reported bug: someone who only has a SHARE on the owner's server signs in and gets a
        session. They own nothing, so there is nothing they could ever set Shortlist up against."""
        _pin_routes()
        _resources(SHARED)
        response = Response()
        with pytest.raises(HTTPException) as caught:
            asyncio.run(poll_pin(1, _request(owner=None), response))
        assert caught.value.status_code == 403
        assert "does not own one" in caught.value.detail
        # Rejected means rejected: no session cookie was minted on the way out.
        assert not response.headers.get("set-cookie")

    @respx.mock
    def test_signs_in_an_account_that_owns_a_server(self):
        _pin_routes()
        _resources(OWNED, SHARED)
        response = Response()
        body = asyncio.run(poll_pin(1, _request(owner=None), response))
        assert body["linked"] is True and body["account_id"] == 42
        assert response.headers.get("set-cookie")

    @respx.mock
    def test_fails_closed_when_plextv_cannot_be_reached(self):
        """An unreachable plex.tv must never read as "sure, they own a server"."""
        _pin_routes()
        respx.get(url__startswith=RESOURCES).mock(side_effect=httpx.ConnectError("boom"))
        response = Response()
        with pytest.raises(HTTPException) as caught:
            asyncio.run(poll_pin(1, _request(owner=None), response))
        assert caught.value.status_code == 503
        assert not response.headers.get("set-cookie")

    @respx.mock
    def test_a_claimed_instance_matches_the_stored_owner_without_asking_plextv(self):
        """Once claimed, the stored owner id is authoritative — no extra plex.tv round trip per login."""
        _pin_routes(account_id=42)
        resources = _resources(OWNED)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(poll_pin(1, _request(owner=7), Response()))
        assert caught.value.status_code == 403
        assert not resources.called


def _link_request(account_id: int):
    state = SimpleNamespace(
        client_id="client-1",
        session_secret="s",
        pending_plex_tokens={account_id: "user-token"},
        owner_account_id=lambda: None,
        secrets=SimpleNamespace(encrypt=lambda v: v),
        sessions=None,  # never reached: every case here is rejected before the DB write
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=state),
        method="POST",
        headers={"x-shortlist-csrf": "1"},
        cookies={},
    )
    return request


class TestLinkRejectsAServerTheAccountDoesNotOwn:
    @respx.mock
    def test_rejects_linking_a_server_only_shared_with_the_account(self, monkeypatch):
        """`owner_account_id` in the body is the caller vouching for themselves — it matches their own
        session by construction. Without asking plex.tv, a friend with a share could link the owner's
        PMS and have Shortlist write collections and share filters on a server that isn't theirs."""
        import shortlist.server.api.setup as setup_module

        monkeypatch.setattr(setup_module, "require_setup_access", lambda request: {"account_id": 99})
        _resources(SHARED)  # account 99 owns nothing; the friend's server is merely shared with them
        body = LinkRequest(plex_url="http://pms:32400", machine_id="machine-shared", owner_account_id=99)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(link_server(body, _link_request(99)))
        assert caught.value.status_code == 403
        assert "own" in caught.value.detail

    @respx.mock
    def test_rejects_when_plextv_cannot_confirm_ownership(self, monkeypatch):
        import shortlist.server.api.setup as setup_module

        monkeypatch.setattr(setup_module, "require_setup_access", lambda request: {"account_id": 99})
        respx.get(url__startswith=RESOURCES).mock(side_effect=httpx.ConnectError("boom"))
        body = LinkRequest(plex_url="http://pms:32400", machine_id="machine-owned", owner_account_id=99)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(link_server(body, _link_request(99)))
        assert caught.value.status_code == 502
