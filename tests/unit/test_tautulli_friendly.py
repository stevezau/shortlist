"""Test that Tautulli friendly_names includes all non-empty names."""

from shortlist.engine.clients.tautulli import TautulliClient


def test_includes_friendly_names_even_when_they_match_username():
    """Friendly names that match the username should still be included — Tautulli might have
    capitalization or formatting differences, and the UI can decide whether to show them."""
    client = TautulliClient("http://fake", "fakekey")
    client._cmd = lambda cmd: {
        "data": [
            {"user_id": "100", "username": "john", "friendly_name": "John"},  # capitalized
            {"user_id": "200", "username": "alice", "friendly_name": "Alice Smith"},  # different
            {"user_id": "300", "username": "bob", "friendly_name": "bob"},  # exact match
            {"user_id": "400", "username": "eve", "friendly_name": ""},  # empty, should drop
        ]
    }

    names = client.friendly_names()

    assert names == {
        100: "John",  # included despite username="john"
        200: "Alice Smith",  # clearly different
        300: "bob",  # included despite exact match
        # 400 not included (empty)
    }


def test_drops_empty_friendly_names():
    """Empty or whitespace-only friendly names should be dropped."""
    client = TautulliClient("http://fake", "fakekey")
    client._cmd = lambda cmd: {
        "data": [
            {"user_id": "100", "username": "john", "friendly_name": "   "},  # whitespace
            {"user_id": "200", "username": "alice", "friendly_name": None},  # None
        ]
    }

    names = client.friendly_names()

    assert names == {}  # both dropped
