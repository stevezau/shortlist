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
from fastapi.responses import JSONResponse, Response

from rowarr.cli import FileSnapshotStore
from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import NullCurator
from rowarr.engine.history import PlexHistorySource
from rowarr.engine.models import EngineConfig, MediaType, RowSpec, UserProfile, UserType
from rowarr.engine.pipeline import EngineContext
from rowarr.engine.pipeline import run as engine_run
from rowarr.engine.probe import run_privacy_probe
from rowarr.engine.verify import check_t1, check_t2, collection_id_from_hub
from tests.fakes.fake_plex import FakeCollection, FakePlexState, make_fake_plex, make_fake_plextv, seed_state

pytestmark = pytest.mark.integration


def _make_fake_tmdb(state: FakePlexState) -> FastAPI:
    """Suggestions = the next 10 catalog titles after the seed — deterministic, always in-library.

    Movie seeds suggest movies, TV seeds suggest shows — so a run produces picks of both types
    and delivery has to get each into the right library.
    """
    app = FastAPI()
    # How TV lookups fail, if they do. TMDB has two very different failure modes and they take
    # different code paths: "empty" is a polite 200/404 that yields no candidates, while 429/500
    # RAISE out of TmdbClient. A fake that can only express the polite one hides every bug that
    # lives on the raised path — which is exactly what happened here.
    app.state.tv_status = "ok"  # "ok" | "empty" | 429 | 500
    movies = sorted(state.movies.values(), key=lambda m: m.tmdb_id)
    shows = sorted(state.shows.values(), key=lambda m: m.tmdb_id)

    def _suggest(catalog: list, tmdb_id: int, key: str) -> dict:
        index = {item.tmdb_id: i for i, item in enumerate(catalog)}
        base = index.get(tmdb_id, 0)
        results = []
        for offset in range(1, 11):
            item = catalog[(base + offset) % len(catalog)]
            results.append(
                {
                    "id": item.tmdb_id,
                    key: item.title,
                    "vote_average": item.audience_rating,
                    "genre_ids": [1],
                    ("release_date" if key == "title" else "first_air_date"): f"{item.year}-06-01",
                }
            )
        return {"results": results}

    @app.get("/genre/movie/list")
    @app.get("/genre/tv/list")
    def genres() -> dict:
        return {"genres": [{"id": 1, "name": "Drama"}]}

    @app.get("/movie/{tmdb_id}/{endpoint}")
    def movie_suggestions(tmdb_id: int, endpoint: str) -> dict:
        return _suggest(movies, tmdb_id, "title")

    @app.get("/tv/{tmdb_id}/{endpoint}")
    def tv_suggestions(tmdb_id: int, endpoint: str) -> Response:
        status = app.state.tv_status
        if status == "ok":
            return JSONResponse(_suggest(shows, tmdb_id, "name"))
        if status == "empty":
            return JSONResponse({"results": []})
        return JSONResponse({"status_message": "the api is unhappy"}, status_code=int(status))

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
    tmdb_app = _make_fake_tmdb(state)
    servers = [
        _UvicornThread(make_fake_plex(state)).start(),
        _UvicornThread(make_fake_plextv(state)).start(),
        _UvicornThread(tmdb_app).start(),
    ]
    pms, plextv, tmdb = servers
    monkeypatch.setattr("rowarr.engine.clients.plex.PLEXTV", plextv.url)
    monkeypatch.setattr("rowarr.engine.clients.tmdb.API", tmdb.url)
    yield state, pms.url, tmdb_app
    for server in servers:
        server.stop()


def test_engine_run_and_privacy_probe_end_to_end(fakes, tmp_path):
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    assert plex.machine_id == state.machine_id
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        # row_size is wide enough that a both-types watcher gets picks of both types — a narrow
        # row can fill up with movies alone and never exercise cross-library delivery.
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
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

    # Every user's rows, found by title-cased label. A user gets one collection per library they
    # have picks in — never one collection holding both types, which no share filter can hide.
    owned = plex.owned_collections()
    assert {slug: row.label for slug, row in owned.items()} == {
        "sarah": "Rowarr_sarah",
        "mike": "Rowarr_mike",
        "canary": "Rowarr_canary",
    }
    rows_by_library = {
        slug: sorted(state.collections[key].section_id for key in row.rating_keys) for slug, row in owned.items()
    }
    assert rows_by_library == {
        "sarah": [state.section_id, state.show_section_id],  # watched both -> a row in each
        "mike": [state.show_section_id],  # watched only TV -> only a TV row
        # Cold start draws from EVERY library, so a thin-history TV watcher gets shows rather
        # than a row of films they never asked for.
        "canary": [state.section_id, state.show_section_id],
    }
    for slug, row in owned.items():
        for rating_key in row.rating_keys:
            collection = state.collections[rating_key]
            assert collection.item_keys, slug
            assert collection.mode == 0  # hidden from library browsing
            assert collection.promoted_shared_home and collection.promoted_own_home  # promoted post-sync
            # Every item matches the library the collection lives in, so a `label!=` exclude can
            # actually match it. A mixed-type collection is unfilterable and leaks to everyone.
            assert state.filterable(collection), f"{slug}: row in section {collection.section_id} is unfilterable"

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
    all_ids = {key for row in owned.values() for key in row.rating_keys}
    r = httpx.get(f"{pms_url}/hubs", headers={"X-Plex-Token": state.owner_token, "Accept": "application/json"})
    owner_hub_ids = {collection_id_from_hub(h) for h in r.json()["MediaContainer"]["Hub"]}
    assert all_ids <= owner_hub_ids

    # Canary /hubs (switch -> resources -> server token) shows its own row and NONE of the others'
    # — including sarah's TV row, which lives in a different library than her movie row.
    canary_token = plextv.canary_server_token(203)
    assert canary_token == "server-203"
    canary_hub_ids = {collection_id_from_hub(h) for h in plex.user_hubs(canary_token)}
    assert set(owned["canary"].rating_keys) <= canary_hub_ids
    foreign = set(owned["sarah"].rating_keys) | set(owned["mike"].rating_keys)
    assert not (foreign & canary_hub_ids), "another user's row is visible to the canary"

    canary = next(u for u in users if u.slug == "canary")
    known = {u.plex_account_id: u.slug for u in users}
    assert check_t1(plextv, known, {slug: row.label for slug, row in owned.items()}).passed
    assert check_t2(plex, plextv, canary, owned).passed

    # Second run is a steady-state no-op: same rows, zero filter writes, update path exercised
    # (sortUpdate + moveItem run against the existing collections instead of createCollection).
    report2 = engine_run(ctx, users)
    assert report2.ok
    assert all(not u.privacy_synced for u in report2.users)
    assert len(state.collections) == len(all_ids)  # no duplicate rows created on a re-run
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


def _strip_marker(title: str) -> str:
    """Drop the invisible zero-width marker to recover the human-readable row title."""
    return "".join(ch for ch in title if ch not in "​‌")


def test_two_per_person_rows_share_one_label_and_are_both_hidden(fakes, tmp_path):
    """Multiple per-person rows: each is its own collection (told apart by title) but they all
    carry the user's single label, so one `label!=` exclude on everyone else hides the whole set —
    and every row is promoted and filterable. This is the core of the collections feature."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[
                RowSpec(slug="picked", name_template="", size=12),
                RowSpec(slug="gems", name_template="Hidden Gems", size=8),
            ],
        ),
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

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    owned = plex.owned_collections()
    assert owned["sarah"].label == "Rowarr_sarah"  # one label for all of a user's rows
    sarah_titles = {_strip_marker(state.collections[k].title) for k in owned["sarah"].rating_keys}
    # Sarah watched movies AND shows, so each of her two rows lands in both libraries.
    assert sarah_titles == {"✨ Picked for You", "Hidden Gems"}
    assert len(owned["sarah"].rating_keys) == 4  # 2 rows x 2 libraries
    for rating_key in owned["sarah"].rating_keys:
        collection = state.collections[rating_key]
        assert collection.item_keys
        assert collection.promoted_shared_home and collection.promoted_own_home
        assert state.filterable(collection)

    # One exclude of the single label hides all of sarah's rows from mike (and vice-versa).
    remote = {u.id: u for u in plextv.list_users()}
    assert "Rowarr_sarah" in remote[202].filters["filterMovies"]
    assert "Rowarr_mike" in remote[201].filters["filterMovies"]


def test_shared_row_is_public_built_from_aggregate_and_never_excluded(fakes, tmp_path):
    """A shared 'popular on this server' row: one public collection built from aggregate history,
    promoted to everyone, excluded from NOBODY's share filter, and framed aggregately (never
    'because you watched'). The per-person rows keep their private label and excludes as before."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[
                RowSpec(slug="picked", name_template="", size=12),
                RowSpec(
                    slug="popular",
                    name_template="Popular on this server",
                    size=6,
                    shared=True,
                    min_watchers=1,  # union of all histories -> guaranteed content in the fixture
                ),
            ],
        ),
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

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    owned = plex.owned_collections()
    assert "shared_popular" in owned, "the shared row was not delivered"
    assert owned["shared_popular"].label == "Rowarr_shared_popular"
    for rating_key in owned["shared_popular"].rating_keys:
        collection = state.collections[rating_key]
        assert collection.item_keys
        assert collection.promoted_shared_home  # public on Home for everyone
        assert state.filterable(collection)

    # The shared label is excluded from NOBODY — it is public by design.
    for account in plextv.list_users():
        assert "shared" not in account.filters.get("filterMovies", "").lower()
        assert "shared" not in account.filters.get("filterTelevision", "").lower()
    # The per-person rows are still hidden from each other.
    remote = {u.id: u for u in plextv.list_users()}
    assert "Rowarr_sarah" in remote[202].filters["filterMovies"]

    # Aggregate framing — never per-person.
    shared_report = next(r for r in report.users if r.slug == "shared_popular")
    assert shared_report.picks
    assert all(pick.reason == "Popular on this server" for pick in shared_report.picks)


def test_a_shared_row_below_the_watcher_threshold_is_skipped(fakes, tmp_path):
    """No title watched by enough distinct people -> no public row is written at all."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="popular", name_template="Popular", size=6, shared=True, min_watchers=99)],
        ),
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

    engine_run(ctx, users)

    assert "shared_popular" not in plex.owned_collections()


def test_shared_row_restricted_to_a_subset_is_hidden_from_the_rest(fakes, tmp_path):
    """A shared row with a chosen audience is hidden from everyone else — the same hide-from-
    outsiders machinery a private row uses, generalized to an arbitrary audience (Phase D)."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[
                RowSpec(
                    slug="staff",
                    name_template="Staff Picks",
                    size=6,
                    shared=True,
                    min_watchers=1,
                    audience={201},  # sarah only
                )
            ],
        ),
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

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]
    assert "shared_staff" in plex.owned_collections()

    remote = {u.id: u for u in plextv.list_users()}
    # Sarah (201) is in the audience -> the shared label is NOT excluded from her.
    assert "shared_staff" not in remote[201].filters.get("filterMovies", "").lower()
    # Everyone else is outside it -> the shared label IS excluded, hiding the row from them.
    assert "Rowarr_shared_staff" in remote[202].filters["filterMovies"]
    assert "Rowarr_shared_staff" in remote[203].filters["filterMovies"]


def test_a_run_heals_the_leaking_rows_a_previous_version_left_behind(fakes, tmp_path):
    """The upgrade path, reproduced from the live failure (SFLIX, 2026-07-12).

    The shipped version delivered every pick into the movie library regardless of type, so a TV
    watcher's row was a movie-library collection full of shows. Plex fixes a collection's subtype
    at creation and never revises it, so such a row is matched by neither `filterMovies` nor
    `filterTelevision` — its `label!=` exclude does nothing and EVERY user can see it. T1 passes
    the whole time, because the excludes really are on the filters.

    Upgrading must therefore not merely stop creating these rows: it must destroy the ones
    already on the server. Patching the contents in place is not enough — the subtype is sticky.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
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

    # The broken state the old code produced: show-subtype collections sitting in the MOVIE
    # library, promoted onto everyone's Home, with the excludes correctly in place on plex.tv.
    #
    # Both users are seeded because the two healing paths are DIFFERENT code:
    #   mike  — watches only TV, so he has no movie picks: his mistyped row must be PRUNED.
    #   sarah — watches both, so she HAS movie picks: delivery finds her mistyped movie row and
    #           must delete and RECREATE it. Merely swapping its contents leaves the sticky show
    #           subtype behind and the row goes on leaking — which is the whole trap.
    broken = {}
    for rating_key, username, items in ((99001, "mike", [301, 302, 303]), (99002, "sarah", [304, 305, 306])):
        collection = FakeCollection(
            rating_key=rating_key,
            title="✨ Picked for You",
            section_id=state.section_id,  # movie library...
            subtype="show",  # ...holding shows. Unhidable.
            labels=[f"Rowarr_{username}"],
            item_keys=items,
            mode=0,
            promoted_own_home=True,
            promoted_shared_home=True,
        )
        state.collections[rating_key] = collection
        broken[username] = collection

    for user in state.users.values():
        excludes = ",".join(sorted(f"Rowarr_{u.username.lower()}" for u in state.users.values() if u is not user))
        user.filters["filterMovies"] = f"label!={excludes}"
        user.filters["filterTelevision"] = f"label!={excludes}"

    # Sanity: these really are leaks today — the canary sees both rows despite excluding both labels.
    for collection in broken.values():
        assert not state.filterable(collection)
    before = {collection_id_from_hub(h) for h in plex.user_hubs("server-203")}
    assert {c.rating_key for c in broken.values()} <= before, "the fixture does not reproduce the leak it claims to"

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    for username, collection in broken.items():
        assert collection.rating_key not in state.collections, f"{username}'s leaking row survived the upgrade run"
    for collection in state.collections.values():
        assert state.filterable(collection), f"{collection.title!r} is still unhidable after the run"
    # sarah still has her movie row — it was rebuilt, not merely removed.
    assert state.section_id in {state.collections[k].section_id for k in plex.owned_collections()["sarah"].rating_keys}

    # And now nobody sees anyone else's row.
    owned = plex.owned_collections()
    for account_id, slug in ((201, "sarah"), (202, "mike"), (203, "canary")):
        visible = {collection_id_from_hub(h) for h in plex.user_hubs(f"server-{account_id}")}
        foreign = {key for other, row in owned.items() if other != slug for key in row.rating_keys}
        assert not (foreign & visible), f"{slug} can still see another user's row"

    canary = next(u for u in users if u.slug == "canary")
    assert check_t2(plex, plextv, canary, owned).passed


def test_a_bad_night_upstream_does_not_destroy_an_established_row(fakes, tmp_path):
    """One library going quiet must not delete the row in it.

    TMDB turns a 404 into an empty result rather than an error, so a single removed/unknown TV id
    can leave a user with zero show candidates for a night. Their TV row still holds its items and
    its `rowarr_<slug>` label, so every other user's `label!=` exclude still hides it — it is
    stale, not leaking. Deleting it would mean an upstream hiccup silently destroys a working row
    (and Plex would hand the rebuilt one a new id, so it would vanish and reappear on Home).
    """
    state, pms_url, tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    tmdb = TmdbClient("test-key")
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=tmdb,
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)

    assert engine_run(ctx, [sarah]).ok
    tv_rows = [
        key
        for key in plex.owned_collections()["sarah"].rating_keys
        if state.collections[key].section_id == state.show_section_id
    ]
    assert tv_rows, "sarah should have a TV row to lose"

    # TMDB goes quiet for TV only — driven at the HTTP boundary, so a regression in our own
    # TmdbClient can't make this test pass by accident.
    tmdb_app.state.tv_status = "empty"

    report = engine_run(ctx, [sarah])

    assert report.ok
    assert all(p.media_type is MediaType.MOVIE for p in report.users[0].picks), "no show picks this run"
    survived = plex.owned_collections()["sarah"].rating_keys
    assert set(tv_rows) <= set(survived), "an established TV row was destroyed by one quiet night"
    for key in survived:
        assert state.filterable(state.collections[key])


def test_a_stranded_row_is_removed_even_from_a_user_who_produces_no_picks(fakes, tmp_path):
    """The user least likely to produce picks is the one most likely to be holding a leak.

    On the upgrade night, a TV-only watcher is exactly who has a show-collection stranded in the
    movie library. If their recommendations also come up empty (TMDB quota, an outage, a library
    they've watched dry), an engine that skips delivery for "no picks" never removes that row —
    and it is visible to every user on the server for as long as it exists. The cleanup sweep has
    to run for every user on every run, picks or no picks.
    """
    state, pms_url, tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    stranded = FakeCollection(
        rating_key=99003,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: no share filter can touch it
        labels=["Rowarr_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Rowarr_mike"
    state.users[201].filters["filterTelevision"] = "label!=Rowarr_mike"
    assert stranded.rating_key in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}

    # mike watches only TV, so a TV outage leaves him with nothing to recommend at all.
    tmdb_app.state.tv_status = "empty"

    report = engine_run(ctx, [mike])

    assert report.users[0].picks == [], "this test is meaningless unless mike produces no picks"
    assert stranded.rating_key not in state.collections, "a leaking row survived a run that produced no picks"
    assert report.users[0].diff.deleted == ["✨ Picked for You"]  # and the audit trail says so
    assert stranded.rating_key not in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}


def test_a_stranded_row_is_removed_even_when_tmdb_errors_out(fakes, tmp_path):
    """The failure mode that actually happens: TMDB 429s, and the whole user RAISES.

    The polite outage (200 with no results) leaves the user with an empty pick list. A 429 or a
    5xx does not — it propagates out of TmdbClient and aborts that user's run. If the cleanup of
    an unhidable row sits downstream of the recommendation work, a rate limit is enough to keep a
    row visible to every user on the server for another night. So the sweep runs FIRST, before
    anything that can fail, and this test pins that ordering.
    """
    state, pms_url, tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    stranded = FakeCollection(
        rating_key=99004,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: unhidable
        labels=["Rowarr_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Rowarr_mike"
    state.users[201].filters["filterTelevision"] = "label!=Rowarr_mike"
    assert stranded.rating_key in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}

    tmdb_app.state.tv_status = 429  # mike watches only TV, so every one of his lookups blows up

    report = engine_run(ctx, [mike])

    assert report.users[0].status == "error", "this test is meaningless unless mike's run fails"
    assert "429" in report.users[0].error
    assert stranded.rating_key not in state.collections, "a leaking row survived because TMDB was rate-limited"
    assert report.users[0].diff.deleted == ["✨ Picked for You"]  # audited even though the run failed
    assert stranded.rating_key not in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}


def test_a_leaking_row_is_swept_even_when_its_owner_is_not_in_the_run(fakes, tmp_path):
    """Whether a row can be hidden has nothing to do with whether its owner runs tonight.

    Disabling or pausing a user does not delete their collection — it only stops us rebuilding
    it. So a sweep scoped to the run's user list would let one click of "pause" (or `paused_all`,
    which makes the user list empty) turn a live leak into a permanent one, silently, with every
    run reporting green. The sweep is driven by the SERVER, not by tonight's roster.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    stranded = FakeCollection(
        rating_key=99005,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: unhidable
        labels=["Rowarr_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Rowarr_mike"
    state.users[201].filters["filterTelevision"] = "label!=Rowarr_mike"
    assert stranded.rating_key in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}

    # mike is paused/disabled tonight: he is not in the user list at all.
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah])

    assert report.ok
    assert stranded.rating_key not in state.collections, "a paused user's leaking row survived the run"
    assert report.swept_rows == {"mike": ["✨ Picked for You"]}  # audited under the slug that owned it
    assert stranded.rating_key not in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}


def test_the_sweep_runs_even_when_every_user_is_paused(fakes, tmp_path):
    """`paused_all` makes the user list empty. A leak must still be cleaned up."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    stranded = FakeCollection(
        rating_key=99006,
        title="✨ Picked for You",
        section_id=state.section_id,
        subtype="show",
        labels=["Rowarr_mike"],
        item_keys=[301, 302],
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded

    report = engine_run(ctx, [])  # nobody to process

    assert report.ok
    assert report.swept_rows == {"mike": ["✨ Picked for You"]}
    assert stranded.rating_key not in state.collections


def test_a_dry_run_reports_the_sweep_without_touching_the_server(fakes, tmp_path):
    """The preview an owner reads before authorising a destructive change must be exact — and
    must change nothing."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12, dry_run=True),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    stranded = FakeCollection(
        rating_key=99007,
        title="✨ Picked for You",
        section_id=state.section_id,
        subtype="show",
        labels=["Rowarr_sarah"],
        item_keys=[301, 302],
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    before = dict(state.collections)
    filters_before = {user.id: dict(user.filters) for user in state.users.values()}

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah])

    assert report.ok
    # Reported exactly once — a preview that double-counts tells the owner twice as many of their
    # rows would be destroyed as actually would be.
    assert report.swept_rows == {"sarah": ["✨ Picked for You"]}
    assert report.users[0].diff.deleted == ["✨ Picked for You"]
    assert state.collections == before, "a dry run changed a collection"
    assert {user.id: dict(user.filters) for user in state.users.values()} == filters_before


def test_a_sweep_that_fails_part_way_aborts_the_run_and_still_audits_what_it_deleted(fakes, tmp_path):
    """Fail closed, and never lose the record of a destructive write.

    The sweep deletes as it walks. If the PMS times out on the second of three deletions, the
    first one has already happened — so the run must (a) refuse to write anything further, since
    we can no longer prove the server has no unhidable rows, and (b) still report the row it did
    delete. Deleting someone's row and then losing the record of it because the next call failed
    would make "whose row did you delete at 03:31" unanswerable (plex-safety rule 10).
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    for rating_key, slug, section_id in ((99008, "mike", state.section_id), (99009, "sarah", state.section_id)):
        state.collections[rating_key] = FakeCollection(
            rating_key=rating_key,
            title=f"Row for {slug}",
            section_id=section_id,
            subtype="show",  # unhidable
            labels=[f"Rowarr_{slug}"],
            item_keys=[301, 302],
            promoted_shared_home=True,
        )

    # The PMS dies after the first deletion — the shape of a timeout mid-sweep.
    real_delete = plex.delete_owned_collection
    calls = {"n": 0}

    def flaky_delete(collection, label_prefix):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("PMS timed out")
        return real_delete(collection, label_prefix)

    plex.delete_owned_collection = flaky_delete
    filters_before = {user.id: dict(user.filters) for user in state.users.values()}

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah])

    assert not report.ok
    assert "PMS timed out" in report.error
    assert report.users == [], "no user may be processed once we cannot prove the server is clean"

    # The one row that WAS deleted is still audited.
    swept = [title for titles in report.swept_rows.values() for title in titles]
    assert len(swept) == 1, f"the deletion that happened was not recorded: {report.swept_rows}"
    assert len(state.collections) == 1  # one deleted, one still there

    # And nothing else was touched: no filters rewritten, no rows built.
    assert {user.id: dict(user.filters) for user in state.users.values()} == filters_before


def test_a_row_created_before_a_mid_delivery_failure_is_still_excluded_on_every_other_share(fakes, tmp_path):
    """A half-finished delivery must never leave a row that nobody's filter hides.

    A user gets one row per library, so delivery can half-succeed: the movie row is created and
    labelled, then the PMS times out building the TV row. The label of the row that DID get
    created has to reach every other user's share filter this run — otherwise it sits on the
    server, labelled, and excluded by nobody, which is precisely the leak this whole change is
    about. It is unpromoted, so it is not on anyone's Home; it is still in the library view that
    `label!=` governs.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    # sarah watches both types, so she gets a row in each library. Blow up on the SECOND create.
    real_create = plex.create_collection
    creates = {"n": 0}

    def flaky_create(section, title, items):
        creates["n"] += 1
        if creates["n"] == 2:
            raise RuntimeError("PMS timed out")
        return real_create(section, title, items)

    plex.create_collection = flaky_create

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah, mike])

    by_slug = {u.slug: u for u in report.users}
    assert by_slug["sarah"].status == "error", "this test is meaningless unless sarah's delivery fails"

    # One row of sarah's exists on the server, labelled...
    sarah_rows = plex.owned_collections()["sarah"].rating_keys
    assert len(sarah_rows) == 1

    # ...and mike's share filter excludes it, even though the run that made it failed.
    mike_filters = state.users[202].filters
    assert "Rowarr_sarah" in mike_filters["filterMovies"], "a live row that nobody's filter hides"
    assert "Rowarr_sarah" in mike_filters["filterTelevision"]

    # It is NOT promoted: a failed run does not put a half-built row on anyone's Home.
    assert not state.collections[sarah_rows[0]].promoted_shared_home


def test_every_account_that_shares_the_server_gets_the_excludes_not_just_the_managed_ones(fakes, tmp_path):
    """The leak that was live on a real server: 45 of its 48 accounts could see three other
    people's private rows.

    Rowarr had only ever written share filters for the three users it MANAGED. Everyone else —
    every account the owner shares the server with but never enabled in Rowarr — had empty
    filters, so all three rows showed up on their Home screen. A row is visible to anyone whose
    filter does not exclude it; Plex does not care whether we call its owner "enabled".

    This also covers the documented rollout path (`rowarr run --user <slug>`, 5 -> 15 -> 40
    users): a run that processes ONE user must still hide that user's new row from everyone.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    # Only sarah is processed. mike and the canary share the server but are not in this run.
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah])

    assert report.ok
    sarah_rows = plex.owned_collections()["sarah"].rating_keys
    assert sarah_rows, "sarah should have rows for this test to mean anything"

    # Every OTHER account on the server excludes her label — in both filter fields.
    for account_id in (202, 203):
        filters = state.users[account_id].filters
        assert "Rowarr_sarah" in filters["filterMovies"], f"account {account_id} can see sarah's row"
        assert "Rowarr_sarah" in filters["filterTelevision"], f"account {account_id} can see sarah's row"

    # And sarah is never excluded from her own row.
    assert "Rowarr_sarah" not in state.users[201].filters["filterMovies"]

    # Proof through their eyes: nobody but sarah can see sarah's rows.
    for account_id in (202, 203):
        visible = {collection_id_from_hub(h) for h in plex.user_hubs(f"server-{account_id}")}
        assert not (set(sarah_rows) & visible), f"account {account_id} sees sarah's row on their Home"

    # T1 agrees — and would have failed before, because it only ever checked managed users.
    known = {u.plex_account_id: u.slug for u in [sarah]}
    assert check_t1(plextv, known, {slug: row.label for slug, row in plex.owned_collections().items()}).passed


def test_a_user_who_is_no_longer_shared_with_does_not_block_everyone_elses_rows(fakes, tmp_path):
    """A stale user row must not stop the whole server working.

    `POST /users/sync` never deletes users, so un-sharing the server with someone leaves a ghost
    in Rowarr's table. If the privacy sync errors on an account plex.tv no longer lists, that one
    dead row makes every OTHER user's row go unpromoted — every night, forever.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 999888: "ghost"},
    )
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    ghost = UserProfile(username="ghost", plex_account_id=999888, user_type=UserType.SHARED)

    report = engine_run(ctx, [sarah, ghost])

    assert report.ok, [(u.username, u.error) for u in report.users]
    sarah_rows = plex.owned_collections()["sarah"].rating_keys
    assert sarah_rows
    assert all(state.collections[key].promoted_shared_home for key in sarah_rows), (
        "one stale user row stopped every other user's rows from being promoted"
    )


def test_a_user_who_renamed_themselves_is_not_hidden_from_their_own_row(fakes, tmp_path):
    """Identity is the account id, not the name.

    Rowarr's slug — and therefore the label on a user's row — is fixed the first time it sees an
    account. Plex usernames are not: people change them. If "is this row mine?" were answered from
    the CURRENT name, a renamed user who isn't in tonight's run would have their own row's label
    merged into their own filter, and `merge_label_excludes` never removes — so their row would
    vanish from their Home permanently.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 202: "mike"},
    )

    # Build mike's row while he is still called "mike".
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    assert engine_run(ctx, [mike]).ok
    mike_rows = plex.owned_collections()["mike"].rating_keys
    assert mike_rows

    # He renames himself on Plex, and tonight's run is only for sarah.
    state.users[202].username = "mike_the_second"
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    assert engine_run(ctx, [sarah]).ok

    # His own label was never merged into his own filter — he can still see his row.
    assert "Rowarr_mike" not in state.users[202].filters["filterMovies"]
    visible = {collection_id_from_hub(h) for h in plex.user_hubs("server-202")}
    assert set(mike_rows) <= visible, "a rename hid a user from their own row"


def test_each_users_row_contains_only_their_own_picks(fakes, tmp_path):
    """ "Picked for You" has to mean picked for YOU.

    A Plex collection is a TAG on items, keyed by TITLE within a library — not an independent bag.
    So two rows with the same title in one library are ONE membership, and every user's row shows
    the union of everyone's picks. On a live server this made every row identical: a film picked
    for one user alone turned up in another user's row, carrying a single collection tag (SFLIX,
    2026-07-13). The privacy still held — each collection object is hidden by its own label — but
    the recommendations were not personal at all.

    Every user's row must therefore carry a title no other row in that library uses.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 202: "mike", 203: "canary"},
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
    ]

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    owned = plex.owned_collections()
    for user_report in report.users:
        expected = {p.title for p in user_report.picks}
        got: set[str] = set()
        for rating_key in owned[user_report.slug].rating_keys:
            collection = state.collections[rating_key]
            got |= {state.item(k).title for k in state.members(collection) if state.item(k)}

        assert got == expected, (
            f"{user_report.slug}'s row does not hold their picks. Extra (somebody else's): {sorted(got - expected)}"
        )


def test_migration_night_rebuilds_every_shared_row_in_one_run(fakes, tmp_path):
    """Upgrade night on a server whose rows were all created before the marker existed.

    Every one of them shares a collection tag with every other row in its library, so each holds
    the union of everyone's picks. All of them have to be rebuilt — and the rebuilds happen one
    user at a time, so a rebuild for one user must not leave another user's row broken. (The fake
    assumes the destructive reading of Plex's tag model: deleting one same-titled collection strips
    those items from its siblings. If the code is right under that, it is right either way.)
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=PlexHistorySource(plex),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 202: "mike", 203: "canary"},
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
    ]

    # The legacy state: every user's row titled the same, in the same library, sharing one tag.
    legacy = {}
    for rating_key, (slug, items) in enumerate(
        {"sarah": [101, 102], "mike": [103, 104], "canary": [105]}.items(), start=98000
    ):
        collection = FakeCollection(
            rating_key=rating_key,
            title="✨ Picked for You",  # identical for everyone: ONE tag
            section_id=state.section_id,
            subtype="movie",
            labels=[f"Rowarr_{slug}"],
            item_keys=items,
            mode=0,
            promoted_own_home=True,
            promoted_shared_home=True,
        )
        state.collections[rating_key] = collection
        legacy[slug] = collection

    # Today they all show the same thing — the union.
    assert len(state.members(legacy["sarah"])) == 5

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    # Every legacy row is gone, and its destruction is on the record (rule 10).
    for slug, collection in legacy.items():
        assert collection.rating_key not in state.collections, f"{slug}'s shared row survived"
    by_slug = {u.slug: u for u in report.users}
    for slug in ("sarah", "mike", "canary"):
        assert "✨ Picked for You" in (by_slug[slug].diff.deleted or []), f"{slug}'s destroyed row was not recorded"

    # And every rebuilt row holds only its owner's picks.
    owned = plex.owned_collections()
    for user_report in report.users:
        expected = {p.title for p in user_report.picks}
        got: set[str] = set()
        for rating_key in owned[user_report.slug].rating_keys:
            collection = state.collections[rating_key]
            got |= {state.item(k).title for k in state.members(collection) if state.item(k)}
        assert got == expected, f"{user_report.slug}: {sorted(got - expected)} belong to someone else"
