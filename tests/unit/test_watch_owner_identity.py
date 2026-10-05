"""PMS's local owner ID must be resolved before recording a credit-bearing session.

The XML below is representative, not a verbatim capture. A genuine Plex Web play on
2026-10-06 decoded 112 seconds and produced User id=1; its persisted session retained
that local ID instead of the linked owner's plex.tv account ID.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.server.db.models import Base, Collection, Delivery, RowDeliverySnapshot, Server, User, WatchSession
from shortlist.server.services.watch_events import RowMembership, shared_credits
from shortlist.server.services.watch_stream import WatchStream

OWNER_SESSION = """<MediaContainer size="1">
<Video ratingKey="100" sessionKey="42" type="movie" duration="3000000" viewOffset="0">
<User id="1" title="owner" />
<Player machineIdentifier="test-web-player" state="playing" product="Plex Web" />
</Video></MediaContainer>"""


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def plex():
    with patch("shortlist.engine.clients.plex_pms.PlexServer"):
        client = PlexClient("http://pms:32400", "test-token")
    client._server.machineIdentifier = "linked-machine"
    client._server.url = lambda path, includeToken=True: f"http://pms:32400{path}"
    return client


def test_real_parser_listener_and_shared_credit_use_canonical_owner(sessions, plex):
    """Disabling personal rows must not prevent an owner earning a visible shared-row credit."""
    with sessions() as session:
        session.add_all(
            [
                Server(machine_id="linked-machine", url="http://pms:32400", token_enc="unused", owner_account_id=99),
                User(id=7, plex_account_id=99, username="owner", slug="owner", user_type="owner", enabled=False),
                Collection(slug="shared", name="Shared", build="shared", enabled=True),
                Delivery(collection_slug="shared", user_slug="shared_shared", library_key="1", rating_key=700),
                RowDeliverySnapshot(
                    source_key="confirmed-delivery",
                    collection_slug="shared",
                    user_slug="shared_shared",
                    library_key="1",
                    shared=True,
                    rating_key=700,
                    delivered_at=datetime.now(UTC) - timedelta(minutes=5),
                    audience=[99],
                    muted=[],
                    picks=[{"tmdb_id": 10, "rating_key": 100, "media_type": "movie"}],
                ),
            ]
        )
        session.commit()

    stream = WatchStream(sessions, MagicMock())
    ctx = SimpleNamespace(plex=plex)
    response = MagicMock(text=OWNER_SESSION)
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=response):
        asyncio.run(stream._on_playing(ctx, {"sessionKey": "42", "ratingKey": 100, "state": "playing"}))
        stream._live["42"].started_at -= timedelta(seconds=112)
        asyncio.run(
            stream._on_playing(ctx, {"sessionKey": "42", "ratingKey": 100, "state": "stopped", "viewOffset": 112_000})
        )

    with sessions() as session:
        actual = session.query(WatchSession).one()
        assert actual.plex_account_id == 99
        assert actual.max_offset_ms == 112_000
        assert actual.end_reason == "stopped"
        credits = shared_credits(session, RowMembership(session))
        assert set(credits) == {(7, "shared", 10, "movie")}


@pytest.mark.parametrize("owner_account_id", [None, 0, -9, True, "99", 99.0])
def test_local_owner_without_a_valid_canonical_identity_is_unresolved(plex, owner_account_id):
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=OWNER_SESSION)):
        actual = plex.active_sessions(owner_account_id=owner_account_id)
    assert actual["42"]["account_id"] is None


@pytest.mark.parametrize("account_id", [2, 99, 501])
def test_shared_and_home_account_ids_are_never_rewritten(plex, account_id):
    xml = OWNER_SESSION.replace('<User id="1"', f'<User id="{account_id}"')
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=xml)):
        assert plex.active_sessions(owner_account_id=99)["42"]["account_id"] == account_id
        assert plex.active_sessions()["42"]["account_id"] == account_id


@pytest.mark.parametrize("user_xml", ["", '<User id="" />', '<User id="unknown" />'])
def test_unknown_user_is_not_inferred_to_be_the_owner(plex, user_xml):
    xml = OWNER_SESSION.replace('<User id="1" title="owner" />', user_xml)
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=xml)):
        assert plex.active_sessions(owner_account_id=99)["42"]["account_id"] is None


@pytest.mark.parametrize(
    ("stored_machine", "actual_machine", "owner_account_id"),
    [
        (None, "linked-machine", 99),
        ("", "linked-machine", 99),
        ("another-machine", "linked-machine", 99),
        ("linked-machine", "", 99),
        ("linked-machine", None, 99),
        ("linked-machine", "linked-machine", None),
        ("linked-machine", "linked-machine", 0),
        ("linked-machine", "linked-machine", -9),
    ],
)
def test_listener_refuses_unproven_owner_but_still_resolves_other_users(
    sessions, plex, stored_machine, actual_machine, owner_account_id
):
    if stored_machine is not None:
        with sessions() as session:
            session.add(
                Server(
                    machine_id=stored_machine,
                    url="http://pms:32400",
                    token_enc="unused",
                    owner_account_id=owner_account_id,
                )
            )
            session.commit()
    plex._server.machineIdentifier = actual_machine
    stream = WatchStream(sessions, MagicMock())
    ctx = SimpleNamespace(plex=plex)
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=OWNER_SESSION)):
        asyncio.run(stream._on_playing(ctx, {"sessionKey": "42", "ratingKey": 100, "state": "playing"}))
    assert stream._live == {}

    other = OWNER_SESSION.replace('<User id="1"', '<User id="501"')
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=other)):
        assert stream._read_active_sessions(ctx)["42"]["account_id"] == 501
