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

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.server.db.models import (
    Collection,
    Delivery,
    PickRow,
    RowDeliverySnapshot,
    Server,
    SharedRowWatch,
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.run_persistence import reconcile_from_events
from shortlist.server.services.watch_events import RowMembership, event_credits, ingest_play_history, shared_credits
from shortlist.server.services.watch_stream import WatchStream
from shortlist.server.settings_store import SettingsStore

OWNER_SESSION = """<MediaContainer size="1">
<Video ratingKey="100" sessionKey="42" type="movie" duration="3000000" viewOffset="0">
<User id="1" title="owner" />
<Player machineIdentifier="test-web-player" state="playing" product="Plex Web" />
</Video></MediaContainer>"""


@pytest.fixture
def sessions(threaded_sessions):
    return threaded_sessions


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


HISTORY_WHEN = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _history_xml(*, account=1, when=HISTORY_WHEN, key="owner-completion", copies=1):
    history_key = f' historyKey="{key}"' if key else ""
    row = (
        f'<Video accountID="{account}" ratingKey="100" type="movie" viewedAt="{int(when.timestamp())}"{history_key} />'
    )
    return f'<MediaContainer size="{copies}">{row * copies}</MediaContainer>'


def _history_rows(sessions, *, personal=True):
    """Delivered personal/shared identity, with run logs already absent."""
    delivered = HISTORY_WHEN - timedelta(days=1)
    with sessions() as session:
        session.add_all(
            [
                Server(machine_id="linked-machine", url="http://pms:32400", token_enc="unused", owner_account_id=99),
                User(id=7, plex_account_id=99, username="owner", slug="owner", enabled=personal),
                Collection(slug="shared", name="Shared", build="shared", enabled=True),
                Delivery(collection_slug="shared", user_slug="shared_shared", library_key="1", rating_key=700),
                RowDeliverySnapshot(
                    source_key="shared-history-delivery",
                    collection_slug="shared",
                    user_slug="shared_shared",
                    library_key="1",
                    shared=True,
                    rating_key=700,
                    delivered_at=delivered,
                    ended_at=HISTORY_WHEN + timedelta(hours=1),
                    audience=[99],
                    muted=[],
                    picks=[{"tmdb_id": 10, "rating_key": 100, "media_type": "movie"}],
                ),
            ]
        )
        if personal:
            session.add_all(
                [
                    Collection(slug="personal", name="Personal", enabled=True),
                    Delivery(collection_slug="personal", user_slug="owner", library_key="1", rating_key=701),
                    PickRow(
                        id=1,
                        user_id=7,
                        collection_slug="personal",
                        section_key="1",
                        tmdb_id=10,
                        media_type="movie",
                        rating_key=100,
                        rank=1,
                        created_at=delivered,
                    ),
                    RowDeliverySnapshot(
                        source_key="personal-history-delivery",
                        collection_slug="personal",
                        user_id=7,
                        user_slug="owner",
                        library_key="1",
                        shared=False,
                        rating_key=701,
                        delivered_at=delivered,
                        ended_at=HISTORY_WHEN + timedelta(hours=1),
                        picks=[{"pick_id": 1, "tmdb_id": 10, "rating_key": 100, "media_type": "movie"}],
                    ),
                ]
            )
        session.commit()


@pytest.mark.parametrize("personal", [True, False])
def test_history_parser_ingest_and_credit_use_verified_owner_without_run_logs(sessions, plex, personal):
    _history_rows(sessions, personal=personal)
    with patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=_history_xml())):
        # Keep the server's raw parser evidence distinct from the verified ingestion boundary.
        assert plex.play_history()[0].plex_account_id == 1
        with sessions() as session:
            assert ingest_play_history(session, plex, SettingsStore(session)) == 1
            session.commit()

    with sessions() as session:
        event = session.query(WatchEvent).one()
        assert event.plex_account_id == 99
        assert event.viewed_at.replace(tzinfo=UTC) == HISTORY_WHEN
        membership = RowMembership(session)
        assert shared_credits(session, membership) == {(7, "shared", 10, "movie"): HISTORY_WHEN}
        assert bool(event_credits(session, membership)) is personal

    assert reconcile_from_events(sessions) == 1
    with sessions() as session:
        assert session.query(SharedRowWatch).one().watched_at.replace(tzinfo=UTC) == HISTORY_WHEN
        if personal:
            assert session.get(PickRow, 1).watched_at.replace(tzinfo=UTC) == HISTORY_WHEN


@pytest.mark.parametrize("when", [HISTORY_WHEN - timedelta(days=1, seconds=1), HISTORY_WHEN + timedelta(hours=1)])
def test_normalized_history_keeps_original_event_time_and_respects_delivery_interval(sessions, plex, when):
    _history_rows(sessions)
    with (
        patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=_history_xml(when=when))),
        sessions() as session,
    ):
        assert ingest_play_history(session, plex, SettingsStore(session)) == 1
        session.commit()
    with sessions() as session:
        event = session.query(WatchEvent).one()
        assert event.plex_account_id == 99
        assert event.viewed_at.replace(tzinfo=UTC) == when
        assert not event_credits(session, RowMembership(session))
        assert not shared_credits(session, RowMembership(session))


@pytest.mark.parametrize(
    ("stored_machine", "actual_machine", "owner"),
    [
        (None, "linked-machine", 99),
        ("another", "linked-machine", 99),
        ("linked-machine", "", 99),
        ("linked-machine", None, 99),
        ("linked-machine", "linked-machine", None),
        ("linked-machine", "linked-machine", 0),
        ("linked-machine", "linked-machine", -9),
    ],
)
def test_history_unproven_owner_stays_unresolved_and_other_account_is_preserved(
    sessions, plex, stored_machine, actual_machine, owner
):
    _history_rows(sessions, personal=False)
    with sessions() as session:
        server = session.query(Server).one()
        if stored_machine is None:
            session.delete(server)
        else:
            server.machine_id, server.owner_account_id = stored_machine, owner
        session.commit()
    plex._server.machineIdentifier = actual_machine
    xml = _history_xml().replace(
        "</MediaContainer>",
        '<Video accountID="501" ratingKey="100" type="movie" '
        f'viewedAt="{int(HISTORY_WHEN.timestamp())}" historyKey="other" /></MediaContainer>',
    )
    with (
        patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=xml)),
        sessions() as session,
    ):
        assert ingest_play_history(session, plex, SettingsStore(session)) == 2
        session.commit()
    with sessions() as session:
        assert {e.plex_account_id for e in session.query(WatchEvent)} == {1, 501}
        assert not shared_credits(session, RowMembership(session))


@pytest.mark.parametrize("key", ["owner-completion", None])
def test_existing_unresolved_history_is_never_rewritten_or_duplicated(sessions, plex, key):
    _history_rows(sessions)
    with sessions() as session:
        session.add(
            WatchEvent(
                plex_account_id=1,
                rating_key=100,
                media_type="movie",
                viewed_at=HISTORY_WHEN,
                source="history",
                history_key=key,
            )
        )
        session.commit()
    with (
        patch("shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=_history_xml(key=key))),
        sessions() as session,
    ):
        assert ingest_play_history(session, plex, SettingsStore(session)) == 0
        session.commit()
    with sessions() as session:
        assert session.query(WatchEvent).one().plex_account_id == 1
        assert not shared_credits(session, RowMembership(session))


@pytest.mark.parametrize("key", ["owner-completion", None])
def test_normalized_history_duplicates_and_overlapping_reads_are_idempotent(sessions, plex, key):
    _history_rows(sessions)
    with patch(
        "shortlist.engine.clients.plex_pms.http_retry.get", return_value=MagicMock(text=_history_xml(key=key, copies=2))
    ):
        for expected in [1, 0]:
            with sessions() as session:
                assert ingest_play_history(session, plex, SettingsStore(session)) == expected
                session.commit()
    with sessions() as session:
        assert session.query(WatchEvent).one().plex_account_id == 99


@pytest.mark.parametrize("account", [2, 99, 501])
def test_verified_history_owner_never_replaces_other_account_ids(sessions, plex, account):
    _history_rows(sessions)
    with (
        patch(
            "shortlist.engine.clients.plex_pms.http_retry.get",
            return_value=MagicMock(text=_history_xml(account=account)),
        ),
        sessions() as session,
    ):
        assert ingest_play_history(session, plex, SettingsStore(session)) == 1
        session.commit()
    with sessions() as session:
        assert session.query(WatchEvent).one().plex_account_id == account
