"""Full engine pipeline + privacy probe against the in-process fake PMS/plex.tv/TMDB.

Real plexapi and real httpx over real (loopback) HTTP — the only stand-ins are the servers
themselves (tests/fakes/fake_plex.py plus a tiny TMDB app below). No mocks on the engine side.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from rowarr.cli import FileSnapshotStore
from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import NullCurator
from rowarr.engine.history import PlexHistorySource
from rowarr.engine.models import EngineConfig, UserProfile, UserType
from rowarr.engine.pipeline import EngineContext
from rowarr.engine.pipeline import run as engine_run
from rowarr.engine.probe import run_privacy_probe
from rowarr.engine.verify import check_t1, check_t2, collection_id_from_hub
from tests.fakes.fake_plex import FakePlexState, make_fake_plex, make_fake_plextv, seed_state

pytestmark = pytest.mark.integration


def _make_fake_tmdb(state: FakePlexState) -> FastAPI:
    """Suggestions = the next 10 catalog titles after the seed — deterministic, always in-library."""
    app = FastAPI()
    catalog = sorted(state.movies.values(), key=lambda m: m.tmdb_id)
    index = {movie.tmdb_id: i for i, movie in enumerate(catalog)}

    @app.get("/genre/movie/list")
    def genres() -> dict:
        return {"genres": [{"id": 1, "name": "Drama"}]}

    @app.get("/movie/{tmdb_id}/{endpoint}")
    def suggestions(tmdb_id: int, endpoint: str) -> dict:
        base = index.get(tmdb_id, 0)
        results = []
        for offset in range(1, 11):
            movie = catalog[(base + offset) % len(catalog)]
            results.append(
                {
                    "id": movie.tmdb_id,
                    "title": movie.title,
                    "vote_average": movie.audience_rating,
                    "genre_ids": [1],
                    "release_date": f"{movie.year}-06-01",
                }
            )
        return {"results": results}

    return app


class _UvicornThread:
    """Run a FastAPI app on an ephemeral loopback port in a daemon thread."""

    def __init__(self, app: FastAPI):
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.url = ""

    def start(self) -> _UvicornThread:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start within 10s")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def fakes(monkeypatch):
    """Seeded state + three live fake servers, with the engine's absolute URLs pointed at them."""
    state = seed_state()
    servers = [
        _UvicornThread(make_fake_plex(state)).start(),
        _UvicornThread(make_fake_plextv(state)).start(),
        _UvicornThread(_make_fake_tmdb(state)).start(),
    ]
    pms, plextv, tmdb = servers
    monkeypatch.setattr("rowarr.engine.clients.plex.PLEXTV", plextv.url)
    monkeypatch.setattr("rowarr.engine.clients.tmdb.API", tmdb.url)
    yield state, pms.url
    for server in servers:
        server.stop()


def test_engine_run_and_privacy_probe_end_to_end(fakes, tmp_path):
    state, pms_url = fakes
    plex = PlexClient(pms_url, state.owner_token)
    assert plex.machine_id == state.machine_id
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=5, min_history=5, candidates_pre_rank=20, max_seeds=10),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
    ]
    assert [u.username for u in users] == ["sarah", "mike", "canary"]

    report = engine_run(ctx, users)

    assert report.ok, [(u.username, u.error) for u in report.users]
    by_slug = {u.slug: u for u in report.users}
    assert by_slug["sarah"].status == "ok"
    assert by_slug["mike"].status == "ok"
    assert by_slug["canary"].status == "cold_start"  # no watch history seeded for the canary

    # One collection per user, found by title-cased label, each with exactly row_size items.
    owned = plex.owned_collections()
    assert {slug: label for slug, (label, _) in owned.items()} == {
        "sarah": "Rowarr_sarah",
        "mike": "Rowarr_mike",
        "canary": "Rowarr_canary",
    }
    for slug, (_, rating_key) in owned.items():
        collection = state.collections[rating_key]
        assert len(collection.item_keys) == 5, slug
        assert collection.mode == 0  # hidden from library browsing
        assert collection.promoted_shared_home and collection.promoted_own_home  # promoted post-sync

    # Filters merged on the fake plex.tv: every user excludes the OTHER two users' stored labels.
    remote = {u.id: u for u in plextv.list_users()}
    expected = {
        201: "label!=Rowarr_canary,Rowarr_mike",
        202: "label!=Rowarr_canary,Rowarr_sarah",
        203: "label!=Rowarr_mike,Rowarr_sarah",
    }
    for account_id, merged in expected.items():
        assert remote[account_id].filters["filterMovies"] == merged
        assert remote[account_id].filters["filterTelevision"] == merged

    # Snapshots captured the PRE-merge filters (all empty at seed time).
    for account_id in (201, 202, 203):
        snapshot = ctx.snapshots.get(account_id)
        assert snapshot is not None
        assert snapshot.filters["filterMovies"] == ""

    # Owner /hubs shows every promoted row.
    r = httpx.get(f"{pms_url}/hubs", headers={"X-Plex-Token": state.owner_token, "Accept": "application/json"})
    owner_hub_ids = {collection_id_from_hub(h) for h in r.json()["MediaContainer"]["Hub"]}
    assert {rating_key for _, rating_key in owned.values()} <= owner_hub_ids

    # Canary /hubs (switch -> resources -> server token) shows its own row but no foreign rows.
    canary_token = plextv.canary_server_token(203)
    assert canary_token == "server-203"
    canary_hub_ids = {collection_id_from_hub(h) for h in plex.user_hubs(canary_token)}
    assert owned["canary"][1] in canary_hub_ids
    assert owned["sarah"][1] not in canary_hub_ids
    assert owned["mike"][1] not in canary_hub_ids

    canary = next(u for u in users if u.slug == "canary")
    assert check_t1(plextv, users, {slug: label for slug, (label, _) in owned.items()}).passed
    assert check_t2(plex, plextv, canary, owned).passed

    # Second run is a steady-state no-op: same rows, zero filter writes, update path exercised
    # (sortUpdate + moveItem run against the existing collections instead of createCollection).
    report2 = engine_run(ctx, users)
    assert report2.ok
    assert all(not u.privacy_synced for u in report2.users)
    assert len(state.collections) == 3
    for account_id, merged in expected.items():
        assert state.users[account_id].filters["filterMovies"] == merged

    # Privacy probe passes end to end and cleans up after itself.
    filters_before_probe = dict(state.users[203].filters)
    result = run_privacy_probe(
        plex,
        plextv,
        canary,
        ctx.snapshots,
        visible_timeout_s=2,
        hidden_timeout_s=2,
        poll_interval_s=0.01,
        sleep=lambda s: None,
    )
    assert result.passed, result.detail
    assert result.detail["baseline_visible"] is True
    assert result.detail["t1_filter_persisted"] is True
    assert result.detail["hidden_after_exclusion"] is True
    assert "probe" not in plex.owned_collections()  # probe collection deleted in finally
    assert state.users[203].filters == filters_before_probe  # canary filters restored byte-identical
