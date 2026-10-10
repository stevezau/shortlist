"""Server building blocks: migrations, settings store, secrets, SSE bus."""

from __future__ import annotations

import asyncio
import signal
import stat
import threading
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from shortlist.server.db.models import Setting, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus, close_on_stop_signals
from shortlist.server.settings_store import SettingsStore
from tests.db_helpers import disposing_engine
from tests.shared_app import app_for


@pytest.fixture
def db_sessions(tmp_path: Path):
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine:
        yield make_session_factory(engine)


class TestMigrations:
    def test_migrations_create_all_v1_tables(self, tmp_path: Path, db_sessions):
        from sqlalchemy import inspect

        with disposing_engine(make_engine(tmp_path)) as engine:
            tables = set(inspect(engine).get_table_names())
            assert {
                "settings",
                "server",
                "users",
                "runs",
                "run_users",
                "picks",
                "restriction_snapshots",
                "caches",
                "events",
            } <= tables

    def test_models_round_trip(self, db_sessions):
        with db_sessions() as session:
            session.add(User(plex_account_id=555000100, username="sarah", slug="sarah"))
            session.commit()
        with db_sessions() as session:
            user = session.query(User).one()
            assert user.enabled is False
            assert user.prefs == {}


class TestSecretBox:
    def test_round_trip_and_key_permissions(self, tmp_path: Path):
        box = SecretBox(tmp_path)
        token = box.encrypt("plex-token-value")
        assert token != "plex-token-value"
        assert box.decrypt(token) == "plex-token-value"
        mode = stat.S_IMODE((tmp_path / "secret.key").stat().st_mode)
        assert mode == 0o600
        # A second box with the same key file decrypts values from the first.
        assert SecretBox(tmp_path).decrypt(token) == "plex-token-value"


class TestSettingsStore:
    def test_defaults_and_set_get(self, db_sessions):
        with db_sessions() as session:
            store = SettingsStore(session)
            assert store.get("row.size") == 15
            store.set("row.size", 20)
            assert store.get("row.size") == 20

    def test_secrets_encrypted_at_rest_and_redacted_in_public_view(self, tmp_path: Path, db_sessions):
        box = SecretBox(tmp_path)
        with db_sessions() as session:
            store = SettingsStore(session, box)
            store.set("plex.token", "super-secret-token")
            raw = session.get(Setting, "plex.token").value["v"]
            assert "super-secret-token" not in raw  # Fernet ciphertext only
            assert store.get("plex.token") == "super-secret-token"
            assert store.all_public()["plex.token"] == "•••••"

    def test_env_seeding_happens_exactly_once(self, tmp_path: Path, db_sessions):
        box = SecretBox(tmp_path)
        with db_sessions() as session:
            store = SettingsStore(session, box)
            store.seed_from_env({"PLEX_URL": "http://pms:32400", "PLEX_TOKEN": "tok"})
            assert store.get("plex.url") == "http://pms:32400"
            assert store.get("plex.token") == "tok"
            # Second boot with different env values: DB wins, env ignored.
            store.seed_from_env({"PLEX_URL": "http://other:32400"})
            assert store.get("plex.url") == "http://pms:32400"


class TestEventBus:
    def test_publish_reaches_subscriber_as_sse_frame(self):
        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            hello = await stream.__anext__()
            assert hello.startswith("event: hello")
            bus.publish("run.finished", {"run_id": 7})
            frame = await asyncio.wait_for(stream.__anext__(), timeout=1)
            assert "event: run.finished" in frame
            assert '"run_id": 7' in frame
            await stream.aclose()

        asyncio.run(scenario())

    def test_close_ends_an_open_stream_normally_when_it_is_waiting(self):
        """A stopping uvicorn waits for every open connection; a stream that never ends held it for ever."""

        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            await stream.__anext__()  # hello
            rest = asyncio.create_task(_drain(stream))
            await asyncio.sleep(0.05)  # parked on its empty queue, as an idle tab is
            assert not rest.done()

            bus.close()

            assert await asyncio.wait_for(rest, timeout=1) == []
            assert not rest.cancelled()

        asyncio.run(scenario())

    def test_close_delivers_frames_already_published_in_order_when_closing(self):
        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            await stream.__anext__()  # hello
            bus.publish("run.progress", {"n": 1})
            bus.publish("run.finished", {"n": 2})

            bus.close()

            frames = await asyncio.wait_for(_drain(stream), timeout=1)
            assert [frame.splitlines()[0] for frame in frames] == ["event: run.progress", "event: run.finished"]

        asyncio.run(scenario())

    def test_close_ends_a_stream_publish_already_dropped_when_its_queue_overflowed(self):
        """`publish` stops feeding a subscriber whose queue is full, but its stream stays open — close must reach it."""

        async def scenario():
            bus = EventBus(max_queue=1)
            stream = bus.stream()
            await stream.__anext__()  # hello
            bus.publish("run.progress", {"n": 1})
            bus.publish("run.progress", {"n": 2})  # queue full: dropped from publishing
            rest = asyncio.create_task(_drain(stream))
            await asyncio.sleep(0.05)

            bus.close()

            frames = await asyncio.wait_for(rest, timeout=1)
            assert frames == ['event: run.progress\ndata: {"n": 1}\n\n']

        asyncio.run(scenario())

    def test_a_stream_opened_after_close_ends_immediately_when_closed(self):
        """uvicorn keeps accepting for up to a tick after the signal — a stream started then must not hold it."""

        async def scenario():
            bus = EventBus()
            bus.close()

            assert await asyncio.wait_for(_drain(bus.stream()), timeout=1) == []

        asyncio.run(scenario())

    def test_close_is_harmless_when_called_twice(self):
        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            await stream.__anext__()  # hello

            bus.close()
            bus.close()

            assert await asyncio.wait_for(_drain(stream), timeout=1) == []
            bus.publish("run.finished", {})  # reaches nobody, raises nothing

        asyncio.run(scenario())


async def _drain(stream) -> list[str]:
    return [frame async for frame in stream]


class _RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []

    def __call__(self, signum: int, frame: object) -> None:
        self.calls.append((signum, frame))


@pytest.fixture
def restore_stop_handlers():
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.mark.usefixtures("restore_stop_handlers")
class TestCloseOnStopSignals:
    """uvicorn's own SIGTERM/SIGINT handler only sets a flag; it never ends the event streams it then waits on."""

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
    def test_a_stop_signal_closes_the_bus_and_reaches_the_previous_handler_when_one_is_installed(self, sig):
        previous = _RecordingHandler()
        signal.signal(sig, previous)
        frame = object()

        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            await stream.__anext__()  # hello
            rest = asyncio.create_task(_drain(stream))
            await asyncio.sleep(0.05)

            chained = close_on_stop_signals(bus, asyncio.get_running_loop())
            signal.getsignal(sig)(sig, frame)

            assert sig in chained
            assert previous.calls == [(sig, frame)]
            assert await asyncio.wait_for(rest, timeout=1) == []

        asyncio.run(scenario())

    def test_a_real_signal_reaches_both_when_it_is_delivered(self):
        previous = _RecordingHandler()
        signal.signal(signal.SIGTERM, previous)

        async def scenario():
            bus = EventBus()
            close_on_stop_signals(bus, asyncio.get_running_loop())

            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(0)

            assert [signum for signum, _ in previous.calls] == [signal.SIGTERM]
            assert await asyncio.wait_for(_drain(bus.stream()), timeout=1) == []

        asyncio.run(scenario())

    def test_the_previous_handler_still_runs_when_the_loop_is_closed(self):
        """uvicorn restores its predecessors before the loop closes, but a late signal must stay harmless."""
        previous = _RecordingHandler()
        signal.signal(signal.SIGTERM, previous)
        loop = asyncio.new_event_loop()
        close_on_stop_signals(EventBus(), loop)
        loop.close()

        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        assert previous.calls == [(signal.SIGTERM, None)]

    @pytest.mark.parametrize("disposition", [signal.SIG_DFL, signal.SIG_IGN])
    def test_nothing_is_installed_when_no_python_handler_is_there_to_chain(self, disposition):
        signal.signal(signal.SIGTERM, disposition)
        signal.signal(signal.SIGINT, disposition)
        loop = asyncio.new_event_loop()
        try:
            chained = close_on_stop_signals(EventBus(), loop)
        finally:
            loop.close()

        assert chained == []
        assert signal.getsignal(signal.SIGTERM) == disposition
        assert signal.getsignal(signal.SIGINT) == disposition

    def test_nothing_is_installed_and_nothing_raises_when_off_the_main_thread(self):
        """Starlette's TestClient runs the lifespan in a worker thread, where `signal.signal` raises."""
        previous = _RecordingHandler()
        signal.signal(signal.SIGTERM, previous)
        loop = asyncio.new_event_loop()
        outcome: list[object] = []

        def boot() -> None:
            try:
                outcome.append(close_on_stop_signals(EventBus(), loop))
            except Exception as exc:  # the assertion below reports it
                outcome.append(exc)

        worker = threading.Thread(target=boot)
        worker.start()
        worker.join(5)
        loop.close()

        assert outcome == [[]]
        assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.integration
def test_the_lifespan_shutdown_closes_the_event_bus_when_no_signal_was_sent(tmp_path: Path):
    """Tests and in-process servers shut down with no signal; the streams must end there too."""
    with TestClient(app_for(tmp_path)) as client:
        bus = client.app.state.bus

    async def scenario():
        assert await asyncio.wait_for(_drain(bus.stream()), timeout=1) == []

    asyncio.run(scenario())


class TestSecurityHeaders:
    """Baseline headers on every response. Deliberately NOT a locked-down CSP — this app renders Plex
    avatars and TMDB artwork from hosts that vary per install, and a policy that blanks the UI on
    somebody else's server is a policy they will switch off."""

    def _client(self, tmp_path):
        from starlette.testclient import TestClient

        from tests.shared_app import app_for

        return TestClient(app_for(tmp_path))

    def test_the_baseline_headers_are_present(self, tmp_path):
        with self._client(tmp_path) as client:
            headers = client.get("/api/system/health").headers

        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "same-origin"

    def test_hsts_only_over_tls(self, tmp_path):
        """Sending HSTS over plain HTTP is meaningless, and from a LAN install it could strand
        someone on an https they have no certificate for."""
        with self._client(tmp_path) as client:
            assert "Strict-Transport-Security" not in client.get("/api/system/health").headers

    def test_health_does_not_advertise_the_version(self, tmp_path):
        """The one unauthenticated endpoint. An anonymous caller does not need to know which build to
        look advisories up for; the UI reads the version from the owner-gated endpoint."""
        with self._client(tmp_path) as client:
            body = client.get("/api/system/health").json()

        assert body == {"status": "ok"}
