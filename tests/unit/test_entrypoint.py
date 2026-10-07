"""docker/entrypoint.sh: how the container's uvicorn is launched, and what that does to `docker stop`."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from shortlist.server.auth import API_TOKEN_KEY
from shortlist.server.db.models import Server
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SettingsStore
from tests.db_helpers import disposing_engine

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"

#: Docker's default `docker stop` grace before it SIGKILLs.
DOCKER_STOP_GRACE_S = 10

#: Well inside the entrypoint's `--timeout-graceful-shutdown 3`: reaching it means the backstop fired.
STREAM_CLOSED_BY_SIGNAL_S = 2.5


def _uvicorn_launches() -> list[str]:
    """Every command in the entrypoint that starts uvicorn, with its `\\` continuations joined."""
    script = ENTRYPOINT.read_text().replace("\\\n", " ")
    return [line for line in script.splitlines() if re.search(r"\buvicorn\s", line) and "exec " in line]


class TestEveryLaunchBoundsTheGracefulShutdown:
    def test_every_uvicorn_launch_carries_a_graceful_shutdown_timeout(self):
        """Without it uvicorn waits for every open connection before running the lifespan shutdown.

        The SPA's SSE stream (`EventBus.stream`) never ends on its own, so one open browser tab made
        `docker stop` wait out its grace period and SIGKILL the container (exit 137) — the app's
        shutdown code, which stops the scheduler and closes in-flight playback sessions, never ran.
        """
        launches = _uvicorn_launches()

        assert launches, "found no uvicorn launch in the entrypoint — the parse is out of date"
        for launch in launches:
            assert "--timeout-graceful-shutdown" in launch, launch


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _seed_owner_and_token(config_dir: Path, token: str) -> None:
    """Just enough for `GET /api/events` to answer 200: an owner (a linked server) and an API token."""
    run_migrations(config_dir)
    with disposing_engine(make_engine(config_dir)) as engine:
        box = SecretBox(config_dir)
        with make_session_factory(engine)() as session:
            session.add(
                Server(machine_id="scratch", url="http://127.0.0.1:9", token_enc=box.encrypt("x"), owner_account_id=1)
            )
            session.commit()
            SettingsStore(session, box).set(API_TOKEN_KEY, token)


def _hold_event_stream(url: str, token: str, first_frame: threading.Event, status: list[int]) -> None:
    """Stay connected to the SSE stream like an open browser tab, until the server closes it."""
    try:
        with (
            httpx.Client(trust_env=False, timeout=httpx.Timeout(5, read=None)) as client,
            client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as response,
        ):
            status.append(response.status_code)
            for line in response.iter_lines():
                if line.startswith("event:"):
                    first_frame.set()
    except httpx.HTTPError:
        pass  # the server going away mid-stream is the expected end


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="runs the entrypoint's non-root branch")
def test_docker_stop_with_a_browser_tab_open_still_runs_the_lifespan_shutdown(tmp_path: Path):
    """The real entrypoint, a real uvicorn, an SSE client that has received a frame, then SIGTERM.

    This is what Docker does on `docker stop`: tini forwards SIGTERM to the exec'd uvicorn, then
    SIGKILLs after the grace period. The process has to be gone inside it, having run its shutdown.
    """
    token = "scratch-token"
    _seed_owner_and_token(tmp_path, token)
    port = _free_port()
    dead_proxy = "http://127.0.0.1:9"  # anything the server tries to reach off-box fails fast instead
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "SHORTLIST_CONFIG": str(tmp_path),
        "SHORTLIST_DRY_RUN": "1",
        "PORT": str(port),
        **{name: dead_proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy")},
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    log_path = tmp_path / "uvicorn.log"
    first_frame = threading.Event()
    status: list[int] = []
    with log_path.open("wb") as log:
        server = subprocess.Popen(["sh", str(ENTRYPOINT)], stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and server.poll() is None:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/system/health", timeout=1, trust_env=False).is_success:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail(f"the server never came up:\n{log_path.read_text()[-2000:]}")

        stream = threading.Thread(
            target=_hold_event_stream,
            args=(f"http://127.0.0.1:{port}/api/events", token, first_frame, status),
            daemon=True,
        )
        stream.start()
        assert first_frame.wait(10), f"the SSE client never received a frame (status {status})"

        sent_at = time.monotonic()
        server.send_signal(signal.SIGTERM)
        try:
            code = server.wait(timeout=DOCKER_STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            pytest.fail(f"still running {DOCKER_STOP_GRACE_S}s after SIGTERM — Docker would SIGKILL it (exit 137)")
        took = time.monotonic() - sent_at
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()

    log_text = log_path.read_text()
    tail = f"exit {code} after {took:.2f}s\n{log_text[-3000:]}"
    assert code != -signal.SIGKILL, "the server was killed, not stopped"
    # The stop signal ends the stream itself (`close_on_stop_signals`), so the graceful-shutdown timeout is
    # a backstop that never fires. When it did, every stop with a tab open logged two ERRORs and a
    # CancelledError traceback — read as a crash — because the stream was ended by cancellation.
    assert took < STREAM_CLOSED_BY_SIGNAL_S, tail
    # uvicorn re-raises the signal it handled once it has shut down, so a clean stop dies of SIGTERM (143).
    assert code == -signal.SIGTERM, tail
    assert "Application shutdown complete." in log_text, tail
    assert "shutdown complete: scheduler and playback listener stopped" in log_text, tail
    assert "timeout graceful shutdown exceeded" not in log_text, tail
    assert "Exception in ASGI application" not in log_text, tail
