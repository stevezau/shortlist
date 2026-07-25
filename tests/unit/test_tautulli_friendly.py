"""Tautulli friendly_names: maps plex account id → friendly name for the row-title default.

These go through a real (respx-mocked) HTTP round-trip rather than stubbing ``_cmd``, because the
bug they guard was in the unwrap layer itself: Tautulli's ``get_users`` returns the user list AS the
response ``data`` (``{"response": {"result": "success", "data": [ ...users... ]}}``), whereas
``get_history`` nests another ``{"data": [...]}`` under it. ``_cmd`` unwraps ``data`` once, so for
``get_users`` its return IS the list — an old ``_cmd(...).get("data")`` raised AttributeError, which
``sync_users`` swallowed, and every user fell back to their bare Plex username (shipped to SFLIX).
A ``_cmd``-stub test could never catch that; it mocked the wrong shape.
"""

import httpx
import respx

from shortlist.engine.clients.tautulli import TautulliClient


def _get_users_response(users: list[dict]) -> httpx.Response:
    """A real Tautulli get_users envelope: the user list sits directly under response.data."""
    return httpx.Response(200, json={"response": {"result": "success", "message": None, "data": users}})


@respx.mock
def test_maps_account_id_to_friendly_name_through_the_real_get_users_shape():
    """The list-shaped `data` must be walked directly — the regression was treating it like a dict."""
    respx.get("http://fake/api/v2").mock(
        return_value=_get_users_response(
            [
                {"user_id": "100", "username": "john", "friendly_name": "John"},  # capitalized
                {"user_id": "200", "username": "alice", "friendly_name": "Alice Smith"},  # different
                {"user_id": "300", "username": "bob", "friendly_name": "bob"},  # exact match, still kept
                {"user_id": "400", "username": "eve", "friendly_name": ""},  # empty → dropped
            ]
        )
    )

    names = TautulliClient("http://fake", "fakekey").friendly_names()

    assert names == {100: "John", 200: "Alice Smith", 300: "bob"}  # 400 dropped (empty)


@respx.mock
def test_drops_empty_and_whitespace_friendly_names():
    respx.get("http://fake/api/v2").mock(
        return_value=_get_users_response(
            [
                {"user_id": "100", "username": "john", "friendly_name": "   "},  # whitespace
                {"user_id": "200", "username": "alice", "friendly_name": None},  # None
            ]
        )
    )

    assert TautulliClient("http://fake", "fakekey").friendly_names() == {}
