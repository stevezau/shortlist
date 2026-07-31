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
import contextlib
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

    @pytest.mark.parametrize(
        "body,content_type",
        [
            ("<html>captive portal</html>", "text/html"),  # a portal or proxy, not plex.tv
            ('{"error": "nope"}', "application/json"),  # 200, valid JSON, wrong shape
            ('["a", "b"]', "application/json"),  # a list, but not of resource objects
        ],
    )
    @respx.mock
    def test_a_malformed_200_raises_the_error_callers_actually_catch(self, body, content_type):
        """Both call sites catch `httpx.HTTPError` and only that. A 200 carrying HTML, or JSON of the
        wrong shape, used to escape as JSONDecodeError/AttributeError — an unhandled 500 with nothing
        logged, right in the middle of the owner's first sign-in."""
        respx.get(url__startswith=RESOURCES).mock(
            return_value=httpx.Response(200, text=body, headers={"content-type": content_type})
        )
        with pytest.raises(httpx.HTTPError):
            asyncio.run(owned_machine_ids("client-1", "tok"))

    @pytest.mark.parametrize("flag", ["0", "false", "", "no", 0, None])
    @respx.mock
    def test_a_non_boolean_owned_flag_is_never_read_as_owned(self, flag):
        """`owned: "0"` and `owned: "false"` are TRUTHY strings in Python. Testing the flag for
        truthiness would hand back a server the account does not own — failing OPEN on the one check
        that decides who may write to a stranger's PMS."""
        respx.get(url__startswith=RESOURCES).mock(return_value=httpx.Response(200, json=[{**OWNED, "owned": flag}]))
        assert asyncio.run(owned_machine_ids("client-1", "tok")) == set()

    @respx.mock
    def test_provides_must_list_server_as_its_own_capability(self):
        """`provides` is comma-separated. A substring match would accept "media-server-client"."""
        respx.get(url__startswith=RESOURCES).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {**OWNED, "clientIdentifier": "real", "provides": "server,player"},
                    {**OWNED, "clientIdentifier": "bogus", "provides": "media-server-client"},
                ],
            )
        )
        assert asyncio.run(owned_machine_ids("client-1", "tok")) == {"real"}

    @respx.mock
    def test_a_malformed_entry_is_skipped_without_taking_the_whole_answer_down(self):
        """One junk entry must not cost the owner their real server."""
        respx.get(url__startswith=RESOURCES).mock(
            return_value=httpx.Response(200, json=["junk", None, {**OWNED}, {"owned": True}])
        )
        assert asyncio.run(owned_machine_ids("client-1", "tok")) == {"machine-owned"}


class TestAgainstTheRecordedPlexTvShape:
    """plex-safety rule 11: the ownership parse is pinned to a REAL recorded response, not to the
    hand-built dicts above. This is the check that decides who may write to a Plex server, so the
    assumption behind it gets a fixture rather than a guess."""

    @staticmethod
    def _fixture() -> list[dict]:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "fixtures" / "plextv_resources.json"
        return json.loads(path.read_text())["entries"]

    @respx.mock
    def test_the_recorded_response_yields_only_the_owned_server(self):
        entries = self._fixture()
        respx.get(url__startswith=RESOURCES).mock(return_value=httpx.Response(200, json=entries))
        owned = asyncio.run(owned_machine_ids("client-1", "tok"))
        # Exactly the one entry that is both owned AND provides a server.
        expected = {
            e["clientIdentifier"] for e in entries if e["owned"] is True and "server" in e["provides"].split(",")
        }
        assert owned == expected
        assert len(owned) == 1

    def test_the_recorded_owned_flag_is_a_real_boolean(self):
        """If plex.tv ever serialised this as a string, the `(True, 1)` comparison would start
        rejecting the owner's own server — a loud failure, not a silent one. Pin the shape so that
        change is caught here rather than in production."""
        for entry in self._fixture():
            assert isinstance(entry["owned"], bool), entry["clientIdentifier"]

    def test_a_player_is_owned_but_is_not_a_linkable_server(self):
        """`owned: true` alone is not enough — an Apple TV is owned and is not a Plex server."""
        players = [e for e in self._fixture() if "player" in e["provides"].split(",")]
        assert players, "fixture should carry a player entry"
        for p in players:
            assert p["owned"] is True and "server" not in p["provides"].split(",")


def _pin_routes(*, account_id: int = 42) -> None:
    respx.get(url__startswith=f"{PLEXTV}/api/v2/pins/").mock(
        return_value=httpx.Response(200, json={"authToken": "user-token"})
    )
    respx.get(f"{PLEXTV}/api/v2/user").mock(
        return_value=httpx.Response(200, json={"id": account_id, "username": "friend"})
    )


def _request(owner: int | None, *, seeded_token: str = ""):
    """`seeded_token` is the env-seeded `plex.token` an unclaimed instance may be holding — the thing
    that names whose instance this is before any server is linked."""

    class _Store:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            return seeded_token if key == "plex.token" else ""

    import shortlist.server.settings_store as settings_store

    settings_store.SettingsStore = _Store  # patched per-request; restored by the fixture below

    state = SimpleNamespace(
        client_id="client-1",
        session_secret="s",
        pending_plex_tokens={},
        owner_account_id=lambda: owner,
        secrets=SimpleNamespace(decrypt=lambda v: v),
        sessions=contextlib.nullcontext,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), url=SimpleNamespace(scheme="http"))


@pytest.fixture(autouse=True)
def _restore_settings_store():
    """`_request` monkeypatches SettingsStore in place; put the real one back after every test."""
    import shortlist.server.settings_store as settings_store

    real = settings_store.SettingsStore
    yield
    settings_store.SettingsStore = real


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


class TestASeededTokenNamesWhoMayClaimTheInstance:
    """The gap the review named: on an UNCLAIMED instance the bar used to be "owns *a* Plex server",
    so anyone running their own PMS could take a session, link their own machine id, and end up
    owning an instance holding somebody else's live `PLEX_TOKEN`. When a working token IS seeded it
    names exactly one account, so that becomes the bar instead."""

    USER = f"{PLEXTV}/api/v2/user"

    @respx.mock
    def test_a_stranger_who_owns_their_own_server_is_still_refused(self):
        _pin_routes(account_id=999)  # the attacker, who genuinely owns a PMS
        respx.get(self.USER).mock(
            side_effect=[
                httpx.Response(200, json={"id": 999, "username": "attacker"}),  # who is signing in
                httpx.Response(200, json={"id": 42, "username": "steve"}),  # who the seed belongs to
            ]
        )
        _resources(OWNED)  # they DO own a server — the old bar would have let them through
        response = Response()
        with pytest.raises(HTTPException) as caught:
            asyncio.run(poll_pin(1, _request(owner=None, seeded_token="seed-tok"), response))
        assert caught.value.status_code == 403
        assert "another account" in caught.value.detail
        assert not response.headers.get("set-cookie")

    @respx.mock
    def test_the_account_the_seeded_token_belongs_to_gets_in(self):
        _pin_routes(account_id=42)
        respx.get(self.USER).mock(
            side_effect=[
                httpx.Response(200, json={"id": 42, "username": "steve"}),
                httpx.Response(200, json={"id": 42, "username": "steve"}),
            ]
        )
        response = Response()
        body = asyncio.run(poll_pin(1, _request(owner=None, seeded_token="seed-tok"), response))
        assert body["linked"] is True and body["account_id"] == 42
        assert response.headers.get("set-cookie")

    @respx.mock
    def test_a_dead_seeded_token_falls_back_rather_than_bricking_first_run(self):
        """A revoked token grants nobody anything, so it is not worth locking the wizard over —
        failing closed here would strand anyone whose seeded token had since been rotated."""
        _pin_routes(account_id=7)
        respx.get(self.USER).mock(
            side_effect=[
                httpx.Response(200, json={"id": 7, "username": "steve"}),
                httpx.Response(401),  # the seed is dead
            ]
        )
        _resources(OWNED)  # so the weaker owns-a-server bar applies, and they pass it
        response = Response()
        body = asyncio.run(poll_pin(1, _request(owner=None, seeded_token="stale"), response))
        assert body["linked"] is True
        assert response.headers.get("set-cookie")

    @respx.mock
    def test_an_unreachable_plextv_never_downgrades_to_the_weaker_bar(self):
        """ "Couldn't ask" is not "no owner". Treating it as one reopens the whole hole."""
        _pin_routes(account_id=999)
        respx.get(self.USER).mock(
            side_effect=[
                httpx.Response(200, json={"id": 999, "username": "attacker"}),
                httpx.ConnectError("boom"),
            ]
        )
        response = Response()
        with pytest.raises(HTTPException) as caught:
            asyncio.run(poll_pin(1, _request(owner=None, seeded_token="seed-tok"), response))
        assert caught.value.status_code == 503
        assert not response.headers.get("set-cookie")


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
