"""Who may use an instance, and when.

Two gates, deliberately different:

* `require_owner` — everything except the wizard (settings, runs, privacy, users, system). Strictly
  the owner. An unclaimed instance has no owner, so it refuses everyone: none of these make sense
  before setup is done, and none should be reachable then.
* `require_setup_access` — only the setup wizard. THREE states, and conflating the first two is how
  an earlier version became a way to steal the owner's Plex token:
    * empty — no server, no secret stored: open (nothing to protect, nobody to protect it for).
    * holds secrets but unclaimed — the environment can seed a real Plex/Tautulli/curator credential
      with no server row. Nobody has claimed it, and there is very much something to steal.
    * claimed — the account that linked the server, and nobody else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from rowarr.server.auth import (
    CSRF_HEADER,
    SESSION_COOKIE,
    require_owner,
    require_setup_access,
    session_serializer,
)

SECRET = "test-secret"


def _request(
    method: str = "GET",
    *,
    account_id: int | None = None,
    owner: int | None = None,
    holds_secrets: bool = False,
    csrf: bool = True,
):
    headers = {CSRF_HEADER: "1"} if csrf else {}
    cookies = {}
    if account_id is not None:
        cookies[SESSION_COOKIE] = session_serializer(SECRET).dumps({"account_id": account_id, "username": "u"})
    return SimpleNamespace(
        method=method,
        headers=headers,
        cookies=cookies,
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_secret=SECRET,
                owner_account_id=lambda: owner,
                holds_secrets=lambda: holds_secrets or owner is not None,
            )
        ),
    )


class TestRequireOwnerIsAlwaysOwnerOnly:
    """settings / runs / privacy / users / system. These never open — not even on a fresh install,
    because none of them do anything before setup is finished, and an open one would be a way in."""

    def test_an_unclaimed_instance_refuses_everyone(self):
        with pytest.raises(HTTPException) as excinfo:
            require_owner(_request("GET"))
        assert excinfo.value.status_code == 401

    def test_an_unclaimed_instance_holding_a_seeded_secret_refuses_everyone(self):
        """The window that let an anonymous caller reach the settings-test endpoint and exfiltrate
        a seeded Tautulli/curator key. These routes are owner-only, full stop."""
        with pytest.raises(HTTPException) as excinfo:
            require_owner(_request("GET", holds_secrets=True))
        assert excinfo.value.status_code == 401

    def test_a_signed_in_stranger_is_refused(self):
        with pytest.raises(HTTPException) as excinfo:
            require_owner(_request("GET", account_id=999, owner=555))
        assert excinfo.value.status_code == 403

    def test_the_owner_is_let_through(self):
        assert require_owner(_request("GET", account_id=555, owner=555))["account_id"] == 555


class TestSetupAccessOnAnEmptyInstance:
    """A fresh install must not demand a sign-in before you can configure anything: signing in with
    Plex is not a gate in front of setup, it IS step 1 — the one that claims the instance."""

    def test_a_visitor_can_read_setup_state(self):
        assert require_setup_access(_request("GET")) == {"unclaimed": True}

    def test_a_visitor_can_drive_the_wizard(self):
        assert require_setup_access(_request("POST")) == {"unclaimed": True}

    def test_but_a_mutation_still_needs_the_csrf_header(self):
        with pytest.raises(HTTPException) as excinfo:
            require_setup_access(_request("POST", csrf=False))
        assert excinfo.value.status_code == 403


class TestSetupAccessWhenSecretsExistButNobodyHasClaimed:
    """The dangerous cell: a seeded credential and no owner yet."""

    def test_a_sessionless_visitor_is_refused(self):
        with pytest.raises(HTTPException) as excinfo:
            require_setup_access(_request("GET", holds_secrets=True))
        assert excinfo.value.status_code == 401

    def test_any_signed_in_plex_account_may_proceed(self):
        """We don't know whose instance it is yet — whoever links the server becomes the owner."""
        assert require_setup_access(_request("GET", account_id=999, holds_secrets=True))["account_id"] == 999


class TestSetupAccessOnAClaimedInstance:
    def test_a_stranger_with_no_session_is_refused(self):
        with pytest.raises(HTTPException) as excinfo:
            require_setup_access(_request("GET", owner=555))
        assert excinfo.value.status_code == 403

    def test_a_signed_in_stranger_is_refused(self):
        """The session issued during the pre-link window is worthless the moment someone else
        claims the instance — owner-ness is re-checked on every request."""
        with pytest.raises(HTTPException) as excinfo:
            require_setup_access(_request("GET", account_id=999, owner=555))
        assert excinfo.value.status_code == 403

    def test_the_owner_is_let_through(self):
        assert require_setup_access(_request("GET", account_id=555, owner=555))["account_id"] == 555


class TestTheStoredTokenGoesToNobodyButTheOwner:
    """`/setup/probe` sends the token to a URL the caller supplies, so a token handed to the wrong
    person is a token mailed to an attacker's host. The stored token is the owner's, and only the
    owner may borrow it."""

    def _request_for(self, *, account_id, owner, pending: dict):
        return SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    pending_plex_tokens=pending,
                    owner_account_id=lambda: owner,
                    sessions=None,
                    secrets=None,
                )
            )
        )

    def test_an_anonymous_caller_gets_no_token(self):
        from rowarr.server.api.setup import _plex_token

        request = self._request_for(account_id=None, owner=None, pending={})
        with pytest.raises(HTTPException) as excinfo:
            _plex_token(request, {"unclaimed": True})
        assert excinfo.value.status_code == 401

    def test_a_signed_in_stranger_with_no_pending_token_gets_no_stored_token(self):
        """The exact hole: on an unclaimed, secret-seeded instance a stranger is let through the
        setup gate, and `pending_plex_tokens` is a per-process dict routinely empty for them (a
        restart, another worker). Falling back to the stored token here would mail it to them."""
        from rowarr.server.api.setup import _plex_token

        # No owner yet, and this caller's pending entry is absent — but the token IS in settings.
        request = self._request_for(account_id=999, owner=None, pending={})
        with pytest.raises(HTTPException) as excinfo:
            _plex_token(request, {"account_id": 999})
        # Refused BEFORE settings is ever read — the stored token is never handed over.
        assert excinfo.value.status_code == 409

    def test_a_caller_always_gets_their_own_pending_token(self):
        from rowarr.server.api.setup import _plex_token

        request = self._request_for(account_id=999, owner=None, pending={999: "their-own-token"})
        assert _plex_token(request, {"account_id": 999}) == "their-own-token"
