"""Full engine pipeline against the in-process fake PMS/plex.tv/TMDB.

Real plexapi and real httpx over real (loopback) HTTP — the only stand-ins are the servers
themselves (tests/fakes/fake_plex.py plus a tiny TMDB app below). No mocks on the engine side.
The per-user row hiding is asserted directly (each account's own Home shows only its own rows).

Every roster built here filters out accounts with a parental restriction profile, because
``context_builder.enabled_profiles`` does — Plex refuses a label filter for such an account, so it can
never have a private row, and the server never hands one to the engine. Feeding one in would test an
input the product does not produce. What DOES happen to those accounts is measured in the privacy
phase, which reports the rows they can see but nothing can hide (#76).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.context import EngineContext
from shortlist.engine.curator import NullCurator
from shortlist.engine.delivery import row_marker
from shortlist.engine.history import ShareTokenWatchSource
from shortlist.engine.models import EngineConfig, MediaType, RowOverride, RowSpec, UserProfile, UserType
from shortlist.engine.pipeline import run as engine_run
from shortlist.engine.privacy import shortlist_labels_in
from tests.fakes.fake_plex import (
    FakeCollection,
    FakeHistoryEntry,
    FakePlexState,
    FakeSection,
    make_fake_plex,
    make_fake_plextv,
    seed_state,
)
from tests.fakes.file_stores import FileSnapshotStore

pytestmark = pytest.mark.integration

_COLLECTION_KEY = re.compile(r"/library/collections/(\d+)")


def collection_id_from_hub(hub: dict) -> int | None:
    """Collection id behind a Home hub, or None for non-collection hubs — so a test can assert which
    of a user's rows are (in)visible on their own Home."""
    match = _COLLECTION_KEY.search(str(hub.get("key") or hub.get("hubKey") or ""))
    return int(match.group(1)) if match else None


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
    monkeypatch.setattr("shortlist.engine.clients.plextv.PLEXTV", plextv.url)
    monkeypatch.setattr("shortlist.engine.clients.tmdb.API", tmdb.url)
    yield state, pms.url, tmdb_app
    for server in servers:
        server.stop()


def test_engine_run_end_to_end(fakes, tmp_path):
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    assert plex.machine_id == state.machine_id
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        # row_size is wide enough that a both-types watcher gets picks of both types — a narrow
        # row can fill up with movies alone and never exercise cross-library delivery.
        #
        # `rows=` is explicit ON PURPOSE. Left empty, `_promote_phase`'s title->spec map is empty and
        # every collection falls to the no-spec fallback — so this test's Home-flag matrix, the
        # load-bearing privacy assertion in the file, would validate the fallback instead of the
        # spec-carrying branch a real server always takes, and a placement-decoding regression would
        # be invisible here.
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=12)],
            rows_defined=True,
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(
            username=u.username,
            plex_account_id=u.id,
            user_type=UserType.MANAGED if u.home else UserType.SHARED,
        )
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]
    assert [u.username for u in users] == ["sarah", "mike", "canary"]

    report = engine_run(ctx, users)

    assert report.ok, [(u.username, u.error) for u in report.users]

    # Provenance survives the whole pipeline — candidate source -> ranking -> curator -> Pick.
    # Asserted here rather than in a unit test because every link in that chain is real: the actual
    # TmdbClient calling the actual /recommendations and /similar endpoints, then the actual ranking.
    delivered = [p for u in report.users for p in u.picks]
    assert delivered, "nothing was delivered, so provenance proves nothing"
    for pick in delivered:
        assert pick.sources, f"{pick.title} reached the row with no record of what suggested it"
        assert 0 < pick.affinity <= 1.0, f"{pick.title} has a nonsense affinity {pick.affinity}"
    seeded = [p for p in delivered if "tmdb_similar" in p.sources]
    assert seeded, "the TMDB source produced nothing, so affinity was never exercised"
    assert any(p.affinity < 1.0 for p in seeded), (
        "every TMDB pick scored a perfect 1.0 — position is being discarded again, which is the "
        "exact bug that filled a medical-drama row with fantasy"
    )
    by_slug = {u.slug: u for u in report.users}
    assert by_slug["sarah"].status == "ok"
    assert by_slug["mike"].status == "ok"
    assert by_slug["canary"].status == "cold_start"  # no watch history seeded for the canary

    # Every user's rows, found by title-cased label. A user gets one collection per library they
    # have picks in — never one collection holding both types, which no share filter can hide.
    owned = plex.owned_collections()
    assert {slug: row.label for slug, row in owned.items()} == {
        "sarah": "Shortlist_sarah",
        "mike": "Shortlist_mike",
        "canary": "Shortlist_canary",
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
    user_by_slug = {u.username.lower(): u for u in users}
    for slug, row in owned.items():
        for rating_key in row.rating_keys:
            collection = state.collections[rating_key]
            assert collection.item_keys, slug
            assert collection.mode == 0  # hidden from library browsing
            # Plex splits Home by OWNER vs everyone-else, not by Home-membership: `promotedToOwnHome`
            # "applies to the server owner", `promotedToSharedHome` "applies to all shared users,
            # including managed users" — https://support.plex.tv/articles/manage-recommendations/.
            # So only the owner gets own-home; shared AND managed both get shared-home.
            if user_by_slug[slug].user_type == UserType.OWNER:
                assert collection.promoted_own_home and not collection.promoted_shared_home, (
                    f"{slug}: the owner gets own home only"
                )
            else:
                assert collection.promoted_shared_home and not collection.promoted_own_home, (
                    f"{slug}: shared and managed users get shared home only"
                )
            # Every item matches the library the collection lives in, so a `label!=` exclude can
            # actually match it. A mixed-type collection is unfilterable and leaks to everyone.
            assert state.filterable(collection), f"{slug}: row in section {collection.section_id} is unfilterable"

    # Filters merged on the fake plex.tv: every user excludes the OTHER two users' stored labels.
    remote = {u.id: u for u in plextv.list_users()}
    expected = {
        201: "label!=Shortlist_canary,Shortlist_mike",
        202: "label!=Shortlist_canary,Shortlist_sarah",
        203: "label!=Shortlist_mike,Shortlist_sarah",
    }
    for account_id, merged in expected.items():
        assert remote[account_id].filters["filterMovies"] == merged
        assert remote[account_id].filters["filterTelevision"] == merged

    # Snapshots captured the PRE-merge filters (all empty at seed time).
    for account_id in (201, 202, 203):
        snapshot = ctx.snapshots.get(account_id)
        assert snapshot is not None
        assert snapshot.filters["filterMovies"] == ""

    # Owner /hubs shows the OWNER's own rows and nobody else's. `promotedToOwnHome` "applies to the
    # server owner"; shared AND managed users are both covered by `promotedToSharedHome`
    # (https://support.plex.tv/articles/manage-recommendations/), so a managed user's row belongs on
    # THEIR Home, not the owner's. Anything else here is a leak of someone's row onto the owner's Home.
    owner_ids = {
        key for slug, row in owned.items() if user_by_slug[slug].user_type is UserType.OWNER for key in row.rating_keys
    }
    other_ids = {
        key
        for slug, row in owned.items()
        if user_by_slug[slug].user_type is not UserType.OWNER
        for key in row.rating_keys
    }
    r = httpx.get(f"{pms_url}/hubs", headers={"X-Plex-Token": state.owner_token, "Accept": "application/json"})
    owner_hub_ids = {collection_id_from_hub(h) for h in r.json()["MediaContainer"]["Hub"]}
    assert not (other_ids & owner_hub_ids), "nobody else's row may appear on the owner's Home"
    assert owner_ids <= owner_hub_ids, "the owner's own rows should appear on their Home"

    # Canary /hubs (switch -> resources -> server token) shows its own row and NONE of the others'
    # — including sarah's TV row, which lives in a different library than her movie row.
    canary_token = plextv.canary_server_token(203)
    assert canary_token == "server-203"
    canary_hub_ids = {collection_id_from_hub(h) for h in plex.user_hubs(canary_token)}
    assert set(owned["canary"].rating_keys) <= canary_hub_ids
    foreign = set(owned["sarah"].rating_keys) | set(owned["mike"].rating_keys)
    assert not (foreign & canary_hub_ids), "another user's row is visible to the canary"

    # Second run is a steady-state no-op: same rows, zero filter writes, update path exercised
    # (sortUpdate + moveItem run against the existing collections instead of createCollection).
    report2 = engine_run(ctx, users)
    assert report2.ok
    assert all(not u.privacy_synced for u in report2.users)
    assert len(state.collections) == len(owner_ids) + len(other_ids)  # no duplicate rows created on a re-run
    for account_id, merged in expected.items():
        assert state.users[account_id].filters["filterMovies"] == merged


def _add_4k_movie_library(state: FakePlexState) -> FakeSection:
    """Mirror the movie catalog into a second movie library: "4K Movies", key 3.

    Same titles, same TMDB ids, DIFFERENT ratingKeys — which is the entire point. A Plex collection
    can only hold items from its own library, so a row built in "4K Movies" out of the "Movies"
    library's ratingKeys is not a cosmetic mistake: it is a collection of items that library does
    not contain.
    """
    section = state.add_section(key=3, kind="movie", title="4K Movies")
    for movie in state.default_section("movie").items.values():
        section.items[movie.rating_key + 400] = replace(movie, rating_key=movie.rating_key + 400)
    return section


def test_a_row_builds_in_every_movie_library_with_that_librarys_own_rating_keys(fakes, tmp_path):
    """ "Movies" + "4K Movies": the very common layout that hid two live bugs.

    An unpinned row targets EVERY library of its type, so a user with movie picks gets a collection
    in both movie libraries. Two things have to hold, and neither can be observed on a server with
    one library per type:

    * Each collection holds its OWN library's ratingKeys for the same picks — the other library's
      keys name items this library does not have.
    * BOTH collections are promoted. `promote()` is the only call that hides a collection from the
      library's normal browse view (`modeUpdate(mode="hide")`), so a row promoted in only the
      lowest-keyed library sits browse-visible to every user in whatever other library it landed in
      — a leak that the `label!=` excludes, which govern browse, do nothing about while the mode is
      still "library default".
    """
    state, pms_url, _tmdb_app = fakes
    movies_4k = _add_4k_movie_library(state)
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    # sarah watches films, so she has a row in EVERY movie library — plus her TV row.
    owned = plex.owned_collections()
    rows_by_library: dict[int, FakeCollection] = {}
    for rating_key in owned["sarah"].rating_keys:
        collection = state.collections[rating_key]
        assert collection.section_id not in rows_by_library, "two rows for one user in one library"
        rows_by_library[collection.section_id] = collection
    assert sorted(rows_by_library) == [state.section_id, state.show_section_id, movies_4k.key]

    movies_row = rows_by_library[state.section_id]
    movies_4k_row = rows_by_library[movies_4k.key]

    # Each collection holds only items its own library actually has...
    for section_id, row in ((state.section_id, movies_row), (movies_4k.key, movies_4k_row)):
        assert row.item_keys, f"section {section_id}: the row is empty"
        assert set(row.item_keys) <= set(state.items_in(section_id)), (
            f"section {section_id}: the row holds ratingKeys from another library"
        )
    # ...and they are DIFFERENT keys for the SAME films: the picks were remapped per library, not
    # copied. Identical key sets would mean one library's keys were written into both collections.
    assert not set(movies_row.item_keys) & set(movies_4k_row.item_keys)
    assert {state.item(k).title for k in movies_row.item_keys} == {state.item(k).title for k in movies_4k_row.item_keys}

    # BOTH are promoted: hidden from library browse (mode 0) and on shared Home.
    for section_id, row in rows_by_library.items():
        assert row.mode == 0, f"the row in section {section_id} is still visible in library browse"
        assert row.promoted_shared_home and not row.promoted_own_home, (
            f"the row in section {section_id} was not promoted"
        )
        assert state.filterable(row)

    # And the excludes hide every one of them from everyone else — through the canary's own eyes.
    for account_id in (202, 203):
        assert "Shortlist_sarah" in state.users[account_id].filters["filterMovies"]
        visible = {collection_id_from_hub(h) for h in plex.user_hubs(f"server-{account_id}")}
        assert not (set(owned["sarah"].rating_keys) & visible), f"account {account_id} sees sarah's row"


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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]

    report = engine_run(ctx, users)
    assert report.ok, [(u.username, u.error) for u in report.users]

    owned = plex.owned_collections()
    assert owned["sarah"].label == "Shortlist_sarah"  # one label for all of a user's rows
    sarah_titles = {_strip_marker(state.collections[k].title) for k in owned["sarah"].rating_keys}
    # Sarah watched movies AND shows, so each of her two rows lands in both libraries. The default
    # 'picked' row renders {library_name} per library, so its title differs (Movies vs TV Shows).
    assert sarah_titles == {"✨ Movies Picked for You", "✨ TV Shows Picked for You", "Hidden Gems"}
    assert len(owned["sarah"].rating_keys) == 4  # 2 rows x 2 libraries
    for rating_key in owned["sarah"].rating_keys:
        collection = state.collections[rating_key]
        assert collection.item_keys
        # Friends' rows show on Friends' Home, not the owner's Home
        assert collection.promoted_shared_home and not collection.promoted_own_home
        assert state.filterable(collection)

    # One exclude of the single label hides all of sarah's rows from mike (and vice-versa).
    remote = {u.id: u for u in plextv.list_users()}
    assert "Shortlist_sarah" in remote[202].filters["filterMovies"]
    assert "Shortlist_mike" in remote[201].filters["filterMovies"]


def test_the_owners_own_row_is_built_from_their_history_and_hidden_from_everyone_else(fakes, tmp_path):
    """The server owner as a row-owning user (issue #1 — plex.tv's user list never returns them).

    Two things have to hold at once, and only one of them is about the owner's own account:
      * their row is built from THEIR watch history — which PMS files under a local account id, not
        the plex.tv id every other user is found by;
      * every other account excludes the owner's label, exactly like any other user's row. The owner
        is the one account Plex cannot restrict, so nothing is written to their own filter — that
        skip must not be mistaken for "this row needs no hiding".
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    owner = UserProfile(
        username=state.owner_username,
        plex_account_id=state.owner_account_id,
        user_type=UserType.OWNER,
    )
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)

    report = engine_run(ctx, [owner, sarah])

    assert report.ok, [(u.username, u.error) for u in report.users]
    by_slug = {u.slug: u for u in report.users}
    # Their seeded history was found: a cold start here would mean we asked PMS for the wrong id and
    # got an empty list back, which looks like a working row but is really the popular-titles row.
    assert by_slug["steve"].status == "ok"

    owned = plex.owned_collections()
    assert owned["steve"].label == "Shortlist_steve"
    for rating_key in owned["steve"].rating_keys:
        collection = state.collections[rating_key]
        assert collection.item_keys
        assert collection.promoted_own_home  # it does reach the owner's own Home
        assert state.filterable(collection)

    # The load-bearing half: everyone else's share filter hides it, in BOTH media types.
    remote = {u.id: u for u in plextv.list_users()}
    assert state.owner_account_id not in remote  # plex.tv genuinely never lists the owner
    for account_id in (201, 202, 203):
        for field_name in ("filterMovies", "filterTelevision"):
            assert "Shortlist_steve" in remote[account_id].filters[field_name], (
                f"account {account_id} can see the owner's row in {field_name}"
            )
    # And nothing was written to the OWNER's own share (rule 5 — Plex cannot restrict them). The
    # fake 404s a filter write for an account it does not know, and the owner is not one of its
    # users, so an attempt to restrict them would have failed the run rather than passing quietly.
    assert state.owner_account_id not in state.users
    assert report.error is None


def test_the_owners_history_is_never_confused_with_a_shared_users(fakes, tmp_path):
    """Each person's watched set is read AS them, so two people never get each other's picks.

    The owner reads with the admin token (they own the server, not shared to it), and their watched
    state is filed under a LOCAL PMS account id — not their plex.tv one. Sarah reads with the per-user
    server token plex.tv minted for her share. Route either token to the wrong account and the owner's
    row becomes someone else's picks."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    source = ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token)

    owner_items = source.fetch(
        UserProfile(username=state.owner_username, plex_account_id=state.owner_account_id, user_type=UserType.OWNER),
        min_completion=0.7,
    )
    sarah_items = source.fetch(
        UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED),
        min_completion=0.7,
    )

    assert owner_items, "the owner's history came back empty — the admin token read the wrong account"
    assert {i.title for i in owner_items}.isdisjoint({i.title for i in sarah_items})


def _watch(state: FakePlexState, account_id: int, rating_key: int) -> None:
    """Record that an account watched a title (used to create shared-history overlap in tests)."""
    state.history.append(FakeHistoryEntry(account_id=account_id, rating_key=rating_key, viewed_at=1_752_100_000))


def _shared_rows(plex: PlexClient, label: str) -> list:
    return [row for row in plex.owned_collections().values() if row.label.lower() == label.lower()]


def _run(plex, plextv, tmp_path, rows, owner_token) -> tuple:
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12, rows=rows),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]
    return ctx, users, engine_run(ctx, users)


def test_shared_row_is_public_built_from_aggregate_and_never_excluded(fakes, tmp_path):
    """A shared 'popular on this server' row: one public collection built from aggregate history,
    promoted to everyone, excluded from NOBODY's share filter, and framed aggregately (never
    'because you watched'). The per-person rows keep their private label and excludes as before."""
    state, pms_url, _tmdb_app = fakes
    _watch(state, 202, 301)  # mike now shares show 301 with sarah -> it clears the 2-watcher floor
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    _ctx, _users, report = _run(
        plex,
        plextv,
        tmp_path,
        [
            RowSpec(slug="picked", name_template="", size=12),
            RowSpec(slug="popular", name_template="Popular on this server", size=6, shared=True),
        ],
        state.owner_token,
    )
    assert report.ok, [(u.username, u.error) for u in report.users]

    shared = _shared_rows(plex, "shortlist__shared_popular")
    assert shared, "the shared row was not delivered"
    for rating_key in shared[0].rating_keys:
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
    assert "Shortlist_sarah" in remote[202].filters["filterMovies"]

    # Aggregate framing — never per-person, and no seed leaks through.
    shared_report = next(r for r in report.users if r.slug == "shared_popular")
    assert shared_report.picks
    # Aggregate framing, and now the actual number: a shared row is the server's most-watched titles,
    # so "N people watched it" is both the reason it is here and the thing that ranked it. The old
    # fixed "Popular on this server" sat on picks that came from a TMDB similar-titles search and was
    # untrue of every one of them.
    assert all(re.fullmatch(r"\d+ people watched it", pick.reason) for pick in shared_report.picks), [
        pick.reason for pick in shared_report.picks
    ]
    # The regression guard the suite lacked: the trace came from the SEARCH, so removing the search
    # silently emptied it and the row's Trace button vanished — no test noticed, because they all
    # asserted what the trace page renders when GIVEN one.
    assert shared_report.trace.get("history"), "a shared row must still record why it picked what it did"
    assert shared_report.trace["history"]["total"] > 0
    assert all(pick.seed_title is None for pick in shared_report.picks), (
        "a shared row must never surface one person's title as its seed"
    )
    assert all(pick.seed_title is None for pick in shared_report.picks)


def test_a_solo_watched_title_never_reaches_a_shared_row(fakes, tmp_path):
    """The aggregate-privacy floor: with no title watched by >= 2 distinct people, a shared row is
    written at all — so one person's viewing can never shape (or appear in) a public row. The
    seeded fixture has zero sarah/mike overlap, so min_watchers=2 (the enforced minimum) skips it."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    # min_watchers=1 is even floored to 2 in the engine, so a lone watcher still can't get through.
    _ctx, _users, report = _run(
        plex,
        plextv,
        tmp_path,
        [RowSpec(slug="popular", name_template="Popular", size=6, shared=True, min_watchers=1)],
        state.owner_token,
    )
    assert not _shared_rows(plex, "shortlist__shared_popular")

    # …and it SAYS why. A silent "skipped" is what made a beta user file the working behaviour as a
    # bug (issue #3), so the reason has to travel with the report, not just the server log.
    skipped = next(u for u in report.users if u.slug == "shared_popular")
    assert skipped.status == "skipped"
    assert skipped.error is None, "a skip is not a failure — the UI counts every error as a failed user"
    assert "2 or more of the 3 people" in skipped.reason


def test_a_run_scoped_to_one_person_leaves_shared_rows_alone(fakes, tmp_path):
    """ "Run now" for one person hands the engine a SUBSET of the roster. A shared row must not be
    built from it — three selected people's overlap is not "popular on this server", and the row is
    published to everyone — and the engine must not judge the row against that subset either, or it
    reports "only 1 person is in this row's audience, it can never build" about a healthy row.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    _watch(state, 202, 301)  # sarah + mike overlap, so the row WOULD build on a full run
    spec = RowSpec(slug="popular", name_template="Popular", size=6, shared=True)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12, rows=[spec], users_scoped=True
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    report = engine_run(ctx, [UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)])

    assert not _shared_rows(plex, "shortlist__shared_popular"), "a subset of the roster built a public row"
    # Not reported at all — the same silence as a row that's out of scope for a per-row run. A
    # "skipped, it can never build" line here would be a lie about a row that builds fine.
    assert not [u for u in report.users if u.slug == "shared_popular"]


def test_a_shared_row_that_can_never_build_says_so_instead_of_just_skipping(fakes, tmp_path):
    """The exact configuration from issue #3: one enabled user and a shared row. The 2-watcher floor
    is then arithmetically unreachable, so the row is skipped every single run — and the report has
    to say that it CAN'T work, not merely that it didn't.

    `users_scoped` stays False here on purpose: this is a FULL run of a one-person server, where
    "only 1 person is in this row's audience" is the literal truth. The scoped-run counterpart is
    the test above."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="popular", name_template="Popular", size=6, shared=True)],
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    only_user = [UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)]

    report = engine_run(ctx, only_user)

    shared = next(u for u in report.users if u.slug == "shared_popular")
    assert shared.status == "skipped"
    assert "at least 2 people with overlapping viewing" in shared.reason
    assert "can never build" in shared.reason
    # "in this row's audience and active in runs", never "enabled" — the audience is already
    # narrowed by enabled AND paused, so "only 1 is enabled" would contradict the Users page.
    assert "in this row's audience and active in runs" in shared.reason
    assert "enabled users" not in shared.reason
    # And the one enabled person is told why THEY got nothing: their only row is a shared row.
    sarah = next(u for u in report.users if u.slug == "sarah")
    assert sarah.status == "skipped"
    assert "no per-person rows" in sarah.reason.lower()
    assert "shared" in sarah.reason.lower()


def test_shared_row_restricted_to_a_subset_is_hidden_from_the_rest(fakes, tmp_path):
    """A shared row with a chosen audience is hidden from everyone else — the same hide-from-
    outsiders machinery a private row uses, generalized to an arbitrary audience (Phase D)."""
    state, pms_url, _tmdb_app = fakes
    _watch(state, 202, 301)  # sarah + mike both watched 301 -> the staff aggregate has content
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    _ctx, _users, report = _run(
        plex,
        plextv,
        tmp_path,
        [RowSpec(slug="staff", name_template="Staff Picks", size=6, shared=True, audience={201, 202})],
        state.owner_token,
    )
    assert report.ok, [(u.username, u.error) for u in report.users]
    assert _shared_rows(plex, "shortlist__shared_staff")

    remote = {u.id: u for u in plextv.list_users()}
    # In the audience (sarah 201, mike 202) -> not excluded.
    assert "shared" not in remote[201].filters.get("filterTelevision", "").lower()
    assert "shared" not in remote[202].filters.get("filterTelevision", "").lower()
    # Outside it (canary 203) -> the shared label IS excluded, hiding the row from them.
    assert "Shortlist__shared_staff" in remote[203].filters["filterTelevision"]


def test_a_per_person_row_only_builds_for_its_audience(fakes, tmp_path):
    """A per-person row restricted to a subset is built ONLY for those people; others get no such
    row (and privacy is untouched — it's just not created for them)."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    _ctx, _users, report = _run(
        plex,
        plextv,
        tmp_path,
        [
            RowSpec(slug="picked", name_template="", size=12),
            RowSpec(slug="gems", name_template="Hidden Gems", size=8, audience={201}),  # sarah only
        ],
        state.owner_token,
    )
    assert report.ok, [(u.username, u.error) for u in report.users]

    owned = plex.owned_collections()
    sarah_titles = {_strip_marker(state.collections[k].title) for k in owned["sarah"].rating_keys}
    mike_titles = {_strip_marker(state.collections[k].title) for k in owned["mike"].rating_keys}
    assert "Hidden Gems" in sarah_titles  # sarah is in the audience
    assert "Hidden Gems" not in mike_titles  # mike is not -> no such row was built for him
    # mike watched only shows, so his default row lands in TV Shows -> the library-named title.
    assert "✨ TV Shows Picked for You" in mike_titles  # the everyone row is still his


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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
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
            labels=[f"Shortlist_{username}"],
            item_keys=items,
            mode=0,
            promoted_own_home=True,
            promoted_shared_home=True,
        )
        state.collections[rating_key] = collection
        broken[username] = collection

    for user in state.users.values():
        excludes = ",".join(sorted(f"Shortlist_{u.username.lower()}" for u in state.users.values() if u is not user))
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


def test_a_bad_night_upstream_does_not_destroy_an_established_row(fakes, tmp_path):
    """One library going quiet must not delete the row in it.

    TMDB turns a 404 into an empty result rather than an error, so a single removed/unknown TV id
    can leave a user with zero show candidates for a night. Their TV row still holds its items and
    its `shortlist_<slug>` label, so every other user's `label!=` exclude still hides it — it is
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    stranded = FakeCollection(
        rating_key=99003,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: no share filter can touch it
        labels=["Shortlist_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Shortlist_mike"
    state.users[201].filters["filterTelevision"] = "label!=Shortlist_mike"
    assert stranded.rating_key in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}

    # mike watches only TV, so a TV outage leaves him with nothing to recommend at all.
    tmdb_app.state.tv_status = "empty"

    report = engine_run(ctx, [mike])

    assert report.users[0].picks == [], "this test is meaningless unless mike produces no picks"
    assert stranded.rating_key not in state.collections, "a leaking row survived a run that produced no picks"
    assert report.users[0].diff.deleted == ["✨ Picked for You"]  # and the audit trail says so
    assert stranded.rating_key not in {collection_id_from_hub(h) for h in plex.user_hubs("server-201")}


def test_a_cold_user_set_to_skip_loses_the_row_they_already_have(fakes, tmp_path):
    """Issue #66, against a real (fake) server: "skip" has to mean GONE, not "not refreshed".

    The dangerous case is not the new user with no row — it is the one who WAS warm. Their row was
    built from taste they no longer clear the bar for (a quiet month, a history-source outage), and
    an engine that merely declines to rebuild leaves it on their Home for ever, going stale, with
    nothing that ever cleans it up. Two real runs, because the row has to be genuinely delivered by
    the real delivery path before the removal means anything.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    rows = [RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=12)]

    def context(**overrides) -> EngineContext:
        return EngineContext(
            config=EngineConfig(
                row_size=12,
                candidates_pre_rank=40,
                max_seeds=12,
                rows=rows,
                rows_defined=True,
                **overrides,
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )

    sarah = next(
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in plextv.list_users()
        if u.username == "sarah"
    )

    warm = engine_run(context(min_history=5), [sarah])
    assert warm.users[0].picks, "the row was never built, so its removal proves nothing"
    delivered_keys = set(plex.owned_collections()["sarah"].rating_keys)
    assert delivered_keys

    # Same server, same row — but nobody clears the threshold now, and the row says skip.
    cold = engine_run(context(min_history=999, cold_start="skip"), [sarah])

    assert cold.users[0].status == "cold_start"  # not "skipped": the Users page reads this flag
    assert cold.users[0].picks == []
    assert "sarah" not in plex.owned_collections(), "the stale row survived a run that skipped it"
    assert not (delivered_keys & set(state.collections)), "the collection is still on the server"
    # Every library, not just one: the row is removed wherever it landed, and the audit trail names
    # each copy. A per-library title (`{library_name}`) makes them distinct rows to Plex.
    assert sorted(cold.users[0].diff.deleted) == ["✨ Movies Picked for You", "✨ TV Shows Picked for You"]


def test_a_cold_user_set_to_skip_still_gets_the_popular_row_from_a_row_that_wants_it(fakes, tmp_path):
    """Per-row beats global, on a real server: one row skips, its sibling still delivers popular
    titles. Global-only could not express this, which is half the point of the issue."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=6,
            min_history=999,  # everybody is cold
            candidates_pre_rank=40,
            max_seeds=12,
            cold_start="skip",  # ...and the server-wide answer is "build nothing"
            rows=[
                RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=6),
                # ...which THIS row overrides.
                RowSpec(slug="popular", name_template="🔥 Popular Right Now", size=6, cold_start="popular"),
            ],
            rows_defined=True,
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    sarah = next(
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in plextv.list_users()
        if u.username == "sarah"
    )

    report = engine_run(ctx, [sarah])

    assert {p.collection_slug for p in report.users[0].picks} == {"popular"}
    titles = {_strip_marker(state.collections[k].title) for k in plex.owned_collections()["sarah"].rating_keys}
    assert titles == {"🔥 Popular Right Now"}


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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    stranded = FakeCollection(
        rating_key=99004,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: unhidable
        labels=["Shortlist_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Shortlist_mike"
    state.users[201].filters["filterTelevision"] = "label!=Shortlist_mike"
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    stranded = FakeCollection(
        rating_key=99005,
        title="✨ Picked for You",
        section_id=state.section_id,  # movie library...
        subtype="show",  # ...full of shows: unhidable
        labels=["Shortlist_mike"],
        item_keys=[301, 302, 303],
        mode=0,
        promoted_own_home=True,
        promoted_shared_home=True,
    )
    state.collections[stranded.rating_key] = stranded
    state.users[201].filters["filterMovies"] = "label!=Shortlist_mike"
    state.users[201].filters["filterTelevision"] = "label!=Shortlist_mike"
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    stranded = FakeCollection(
        rating_key=99006,
        title="✨ Picked for You",
        section_id=state.section_id,
        subtype="show",
        labels=["Shortlist_mike"],
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    stranded = FakeCollection(
        rating_key=99007,
        title="✨ Picked for You",
        section_id=state.section_id,
        subtype="show",
        labels=["Shortlist_sarah"],
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    for rating_key, slug, section_id in ((99008, "mike", state.section_id), (99009, "sarah", state.section_id)):
        state.collections[rating_key] = FakeCollection(
            rating_key=rating_key,
            title=f"Row for {slug}",
            section_id=section_id,
            subtype="show",  # unhidable
            labels=[f"Shortlist_{slug}"],
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
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
    assert "Shortlist_sarah" in mike_filters["filterMovies"], "a live row that nobody's filter hides"
    assert "Shortlist_sarah" in mike_filters["filterTelevision"]

    # It is NOT promoted: a failed run does not put a half-built row on anyone's Home.
    assert not state.collections[sarah_rows[0]].promoted_shared_home


def test_every_account_that_shares_the_server_gets_the_excludes_not_just_the_managed_ones(fakes, tmp_path):
    """The leak that was live on a real server: 45 of its 48 accounts could see three other
    people's private rows.

    Shortlist had only ever written share filters for the three users it MANAGED. Everyone else —
    every account the owner shares the server with but never enabled in Shortlist — had empty
    filters, so all three rows showed up on their Home screen. A row is visible to anyone whose
    filter does not exclude it; Plex does not care whether we call its owner "enabled".

    This also covers the documented rollout path (processing one user at a time, 5 -> 15 -> 40
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
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
        assert "Shortlist_sarah" in filters["filterMovies"], f"account {account_id} can see sarah's row"
        assert "Shortlist_sarah" in filters["filterTelevision"], f"account {account_id} can see sarah's row"

    # And sarah is never excluded from her own row.
    assert "Shortlist_sarah" not in state.users[201].filters["filterMovies"]

    # Proof through their eyes: nobody but sarah can see sarah's rows.
    for account_id in (202, 203):
        visible = {collection_id_from_hub(h) for h in plex.user_hubs(f"server-{account_id}")}
        assert not (set(sarah_rows) & visible), f"account {account_id} sees sarah's row on their Home"


def test_a_user_who_is_no_longer_shared_with_does_not_block_everyone_elses_rows(fakes, tmp_path):
    """A stale user row must not stop the whole server working.

    `POST /users/sync` never deletes users, so un-sharing the server with someone leaves a ghost
    in Shortlist's table. If the privacy sync errors on an account plex.tv no longer lists, that one
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
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

    Shortlist's slug — and therefore the label on a user's row — is fixed the first time it sees an
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
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
    assert "Shortlist_mike" not in state.users[202].filters["filterMovies"]
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 202: "mike", 203: "canary"},
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
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
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        known_slugs={201: "sarah", 202: "mike", 203: "canary"},
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
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
            labels=[f"Shortlist_{slug}"],
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


def test_delivery_records_the_rating_key_of_the_collection_it_built(fakes, tmp_path):
    """The delivery ledger's whole input, against a real PMS rather than a mock.

    Every on-demand reconcile has to answer "which object on the server is this row, for this person,
    in this library?". A title cannot: a `{top_seed}` row renders differently every run. The engine
    therefore reports the collection's ratingKey per (row, library) in the breakdown, and the server
    persists it — so this asserts the key is real and points at the collection the run actually wrote,
    not merely that the field is populated.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=12)],
            rows_defined=True,
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(
            username=u.username,
            plex_account_id=u.id,
            user_type=UserType.MANAGED if u.home else UserType.SHARED,
        )
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]

    report = engine_run(ctx, users)

    account_by_slug = {u.slug: u.plex_account_id for u in users}
    checked = 0
    for user_report in report.users:
        marker = row_marker(account_by_slug[user_report.slug])
        for entry in user_report.breakdown:
            key = entry.get("rating_key")
            assert key, f"{entry['row_title']} in {entry['library_title']} carries no ratingKey"
            # The key must name the collection the run actually WROTE — a stale or invented one would
            # send a later reconcile at the wrong object, or at nothing at all.
            collection = state.collections.get(key)
            assert collection is not None, f"ratingKey {key} is not a collection on this server"
            assert collection.title == entry["row_title"] + marker
            checked += 1
    assert checked, "nothing was delivered, so there is no ledger input to check"


def test_a_scoped_run_never_rebuilds_another_row_as_itself(fakes, tmp_path):
    """Rows have their own crons, so EVERY scheduled run is scoped to a subset — and delivery is
    allowed to treat a title mismatch as an in-place rename when a user has only one row.

    Deriving "only one row" from the rows this run BUILDS rather than the rows the user HAS made row
    A's 3am cron claim to be the user's sole row, grab row B's collection (they share one label; only
    the title tells them apart) and rebuild it as row A. Row B was destroyed nightly, and the run
    reported it as a normal delivery.

    Found by running a scoped build against a real PMS, where the second row's collection vanished.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    rows = [
        RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=8),
        RowSpec(slug="gems", name_template="Hidden Gems", size=8),
    ]

    def ctx_for(build_only):
        return EngineContext(
            config=EngineConfig(
                row_size=8,
                min_history=5,
                candidates_pre_rank=40,
                max_seeds=12,
                rows=rows,
                rows_defined=True,
                build_only=build_only,
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )

    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]

    # Row A's own cron fires first — the ordinary state of a server with per-row schedules, where a
    # newly added row has not been built yet.
    assert engine_run(ctx_for(frozenset({"picked"})), users).ok
    label = f"shortlist_{users[0].slug}"
    before = {c.ratingKey for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    assert before, "row A built nothing, so there is nothing for row B to clobber"

    # Now row B's cron fires. In each library it finds exactly ONE collection under this user's label
    # — row A's — which is the shape that used to license the in-place-rename guess.
    assert engine_run(ctx_for(frozenset({"gems"})), users).ok

    after = {c.ratingKey for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    assert before <= after, f"row B's scoped run destroyed row A's collection(s): {sorted(before - after)}"
    assert len(after) > len(before), "row B should have built its own collection alongside row A's"


def test_a_muted_unrenderable_row_is_not_taken_over_by_another_row(fakes, tmp_path):
    """The other door into the same incident, and the one the first fix left open.

    A muted row is skipped for delivery, and `remove_row` can only remove one whose title is
    unrenderable — a `{top_seed}` template has no title without picks — when the delivery ledger holds
    a ratingKey for it. Without one (delivered before the ledger existed, or an entry two rows both
    claim, which is dropped as ambiguous) its collection is still on the server. Counting only un-muted
    rows therefore said "this user has one row" while two collections sat under the label, and the live
    row's build renamed the muted orphan into itself.

    Two rows would then claim one ratingKey in the ledger, and deleting the muted row later would take
    the live one with it.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    seeded = RowSpec(slug="because", name_template="Because you watched {top_seed}", size=8)
    gems = RowSpec(slug="gems", name_template="Hidden Gems", size=8)

    def ctx_for(rows, overrides=None):
        return EngineContext(
            config=EngineConfig(
                row_size=8,
                min_history=5,
                candidates_pre_rank=40,
                max_seeds=12,
                rows=rows,
                rows_defined=True,
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )

    def users_now(overrides=None):
        return [
            UserProfile(
                username=u.username,
                plex_account_id=u.id,
                user_type=UserType.SHARED,
                row_overrides=overrides or {},
            )
            for u in sorted(plextv.list_users(), key=lambda u: u.id)
            if not u.restriction_profile  # the server never passes the engine a profiled account
        ]

    # Build the {top_seed} row, then mute it — its collection stays, because its title cannot be
    # rendered to match.
    assert engine_run(ctx_for([seeded]), users_now()).ok
    label = f"shortlist_{users_now()[0].slug}"
    before = {c.ratingKey for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    assert before, "the {top_seed} row built nothing, so there is nothing to be taken over"

    muted = {"because": RowOverride(muted=True)}
    assert engine_run(ctx_for([seeded, gems]), users_now(muted)).ok

    after = {c.ratingKey for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    assert before <= after, f"the live row took over the muted row's collection: {sorted(before - after)}"
    assert len(after) > len(before), "'Hidden Gems' should have built its own collection"


def test_a_multi_row_user_gets_a_rename_in_place_that_counting_could_never_do(fakes, tmp_path):
    """The reason identity beats counting, rather than merely being safer than it.

    A row whose rendered title moves — a changed template, a renamed library, a nickname edit — must be
    RETITLED, not orphaned and rebuilt. `sole_row` could only ever authorise that for a user with
    exactly ONE row, because with two it had no way to tell which one moved. So every multi-row user
    accumulated a stale duplicate on every rename: still labelled (so hidden from others), still
    promoted onto their own Home by `_promote_one`'s no-spec branch, and swept by nothing.

    The ledger names the exact object, so the rename works however many rows the user has.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)

    def ctx_for(rows, delivered_keys=None):
        return EngineContext(
            config=EngineConfig(
                row_size=8, min_history=5, candidates_pre_rank=40, max_seeds=12, rows=rows, rows_defined=True
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
            delivered_keys=delivered_keys or {},
        )

    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]
    before_rows = [
        RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=8),
        RowSpec(slug="gems", name_template="Hidden Gems", size=8),
    ]
    assert engine_run(ctx_for(before_rows), users).ok

    slug = users[0].slug
    label = f"shortlist_{slug}"
    # The ledger as the server writes it: one entry per (row, user, LIBRARY). A row delivers into
    # every library of its media type, so a per-row-only entry would leave the others orphaned.
    ledger = {
        (slug, "gems", str(section.key)): c.ratingKey
        for section in plex.sections()
        for c in plex.find_owned_collections(section, label)
        if c.title.startswith("Hidden Gems")
    }
    assert ledger, "the gems row built nothing, so there is nothing to rename"
    gems_keys = set(ledger.values())

    # Rename "Hidden Gems" -> "Buried Treasure". Two rows, so counting cannot authorise a rename.
    after_rows = [before_rows[0], RowSpec(slug="gems", name_template="Buried Treasure", size=8)]
    assert engine_run(ctx_for(after_rows, ledger), users).ok

    now = {c.ratingKey: c.title for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    assert gems_keys <= set(now), "the ledger names these objects — they must be retitled, not orphaned"
    for key in gems_keys:
        assert now[key].startswith("Buried Treasure"), f"expected a rename in place, got {now[key]!r}"
    assert not any(t.startswith("Hidden Gems") for t in now.values()), "a stale duplicate was left behind"


def test_a_ledger_key_naming_another_row_cannot_hijack_it_mid_run(fakes, tmp_path):
    """Identity matching trusts the ledger, so a key naming the WRONG live collection would retitle it
    and replace its membership — the takeover bug again, through the ledger this time.

    Plex ratingKeys are reused rowids: the sweep can free row A's id at the top of a run, row B create
    and be handed it, and row A then match B's brand-new collection. The run's own breakdown records
    what has already been written, so a key already delivered to this run is withheld.

    Simulated here by pointing `gems`' ledger entries at `picked`'s collections — the same end state,
    without needing to provoke id reuse.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    rows = [
        RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=8),
        RowSpec(slug="gems", name_template="Hidden Gems", size=8),
    ]

    def ctx_for(these_rows, delivered_keys=None):
        return EngineContext(
            config=EngineConfig(
                row_size=8, min_history=5, candidates_pre_rank=40, max_seeds=12, rows=these_rows, rows_defined=True
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
            delivered_keys=delivered_keys or {},
        )

    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]
    assert engine_run(ctx_for(rows), users).ok

    slug = users[0].slug
    label = f"shortlist_{slug}"
    # A POISONED ledger: `gems` claims the ratingKeys that are actually `picked`'s.
    poisoned = {
        (slug, "gems", str(section.key)): c.ratingKey
        for section in plex.sections()
        for c in plex.find_owned_collections(section, label)
        if c.title.startswith("✨")
    }
    assert poisoned, "picked built nothing, so there is nothing to hijack"

    # `gems` is also RENAMED, so its own title no longer matches and it reaches the key branch —
    # without that it matches by title and the poisoned key is never consulted.
    renamed = [rows[0], RowSpec(slug="gems", name_template="Buried Treasure", size=8)]
    assert engine_run(ctx_for(renamed, poisoned), users).ok

    now = {c.ratingKey: c.title for s in plex.sections() for c in plex.find_owned_collections(s, label)}
    for key in poisoned.values():
        assert key in now, "picked's collection was destroyed"
        assert now[key].startswith("✨"), f"gems hijacked picked's collection: now titled {now[key]!r}"


def test_a_managed_user_with_a_parental_profile_is_left_out_of_the_filters(fakes, tmp_path):
    """The full-stack cell for issue #20: the profile has to survive `/api/home/users` → `list_users()`
    → `sync_user_restrictions` and actually change the outcome.

    Every other test here has a home user with NO profile, so the join runs but the skip branch never
    does — the feature could have been a no-op through the whole integration layer.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)

    kid = next(u for u in state.users.values() if u.home)
    kid.restricted = True
    kid.restriction_profile = "little_kid"
    before = dict(next(u for u in plextv.list_users() if u.id == kid.id).filters)

    ctx = EngineContext(
        config=EngineConfig(
            row_size=8,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=8)],
            rows_defined=True,
        ),
        plex=plex,
        plextv=PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0),
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    users = [
        UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]

    report = engine_run(ctx, users)

    fresh = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    after = {u.id: u for u in fresh.list_users()}
    assert after[kid.id].restriction_profile == "little_kid", "the profile did not survive the join"
    assert after[kid.id].filters == before, "a profiled account must be left out of the filter writes"
    # And everyone else still got theirs — one profiled account cannot stop the server (#14).
    others = [u for u in after.values() if u.id != kid.id and not u.restricted]
    assert any("label!=" in u.filters.get("filterMovies", "") for u in others)
    assert not report.promotion_blockers


def test_a_profiled_account_that_can_see_other_peoples_rows_is_measured_and_reported(fakes, tmp_path):
    """Issue #76 end to end: Plex refuses a hide-list for a profiled account, so the run has to look
    AS them and report what they can actually see.

    This is the cell nothing covered. `_record_unhideable` early-returns when `ctx.pms_for_user` is
    None, and every other context here leaves it None — so the whole measurement was exercised by no
    test at all, at either end of a feature whose entire purpose is to end a silence.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    history = ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token)

    kid = state.users[204]  # `older_kid` — the profile that DOES see collections on a real server
    assert kid.restriction_profile == "older_kid"

    ctx = EngineContext(
        config=EngineConfig(
            row_size=8,
            min_history=5,
            candidates_pre_rank=40,
            max_seeds=12,
            rows=[RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=8)],
            rows_defined=True,
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=history,
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    # What the server always supplies: the PMS as ONE user sees it. Mirrors
    # `ContextBuilder._pms_for_user`, including its canary fallback for a managed account that was
    # never separately shared — which is precisely the archetype here.
    ctx.pms_for_user = lambda profile: PlexClient(pms_url, token) if (token := history._token_for(profile)) else None

    report = engine_run(ctx, _users(plextv))

    # It looked — and that fact is recorded separately from the findings, so a run that never got
    # here cannot publish an empty result and clear a live alert.
    assert report.unhideable_measured is True
    assert kid.username in report.unhideable_rows, "the profiled account's exposure was not reported"
    assert report.unhideable_rows[kid.username], "reported the account but named no rows"
    # Not a blocker: one account nothing can hide must not stop everyone else being served.
    assert report.ok
    assert not report.promotion_blockers


def test_a_run_that_never_reaches_the_privacy_phase_is_not_recorded_as_having_measured(fakes, tmp_path):
    """The other half of the guard, at the level that actually sets it."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(rows=[RowSpec(slug="picked", name_template="x", size=8)], rows_defined=True),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    # Fail the PMS read the SWEEP makes, not the one `run()` makes before it — the sweep fails
    # CLOSED and returns a report rather than raising, and that returned report is exactly the shape
    # that used to publish an empty finding and clear a live alert.
    real_sections = ctx.plex.sections
    calls = {"n": 0}

    def sections_failing_inside_the_sweep():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("PMS unreachable")
        return real_sections()

    ctx.plex.sections = sections_failing_inside_the_sweep

    report = engine_run(ctx, [])

    assert report.error and "sweep failed" in report.error
    assert report.unhideable_measured is False


def _rating_ctx(state, pms_url, tmp_path, *, dislike_threshold):
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    return EngineContext(
        config=EngineConfig(
            row_size=12,
            min_history=5,
            candidates_pre_rank=40,
            # Wide enough that every one of sarah's watches becomes a seed — so a title MISSING from
            # her seeds is the rating acting, not the cap truncating.
            max_seeds=30,
            dislike_threshold=dislike_threshold,
            rows=[RowSpec(slug="picked", name_template="✨ {library_name} Picked for You", size=12)],
            rows_defined=True,
        ),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    ), plextv


def _users(plextv):
    return [
        UserProfile(
            username=u.username,
            plex_account_id=u.id,
            user_type=UserType.MANAGED if u.home else UserType.SHARED,
        )
        for u in sorted(plextv.list_users(), key=lambda u: u.id)
        if not u.restriction_profile  # the server never passes the engine a profiled account
    ]


def _seed_titles(report, slug: str) -> set[str]:
    return {s["title"] for s in next(u for u in report.users if u.slug == slug).trace.get("seeds", [])}


class TestPlexRatingsEndToEnd:
    """Issue #69 over the real wire: fake PMS -> real HTTP -> real plexapi -> parse -> seeds.

    Every other test of this feature works on in-memory `WatchedItem`s. This is the only one where
    the rating actually travels as an XML attribute on the same `unwatched=0` read the engine already
    makes, read with a per-user share token — which is the entire feasibility claim.
    """

    #: One of sarah's watched movies (`seed_state` gives her 101..108).
    DISLIKED = 103
    DISLIKED_TITLE = "Movie 03"

    def test_a_title_sarah_rated_low_stops_seeding_her_row(self, fakes, tmp_path):
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 2.0)  # sarah, one star
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        assert report.ok, [(u.username, u.error) for u in report.users]
        seeds = _seed_titles(report, "sarah")
        assert seeds, "sarah produced no seeds at all, so the absence below proves nothing"
        assert self.DISLIKED_TITLE not in seeds
        assert "Movie 04" in seeds, "her other watches must still seed — this is not a blanket drop"

    def test_the_same_title_still_seeds_when_the_feature_is_off(self, fakes, tmp_path):
        """The control. Without it, a title missing from the seeds could be the fixture, the cap, or
        anything else — this pins the difference to the rating and nothing else."""
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 2.0)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=None)

        report = engine_run(ctx, _users(plextv))

        assert self.DISLIKED_TITLE in _seed_titles(report, "sarah")

    def test_a_rating_above_the_threshold_changes_nothing(self, fakes, tmp_path):
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 10.0)  # five stars
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        assert self.DISLIKED_TITLE in _seed_titles(report, "sarah")

    def test_one_persons_rating_never_reaches_another_persons_row(self, fakes, tmp_path):
        """The leak this feature could have caused. `userRating` is per-account on a real server, so
        mike rating a show badly must not remove it from sarah's seeds — and the only thing making
        that true is that each read uses that person's OWN share token."""
        state, pms_url, _ = fakes
        shared_show = 305  # in mike's watched set (305..312) AND sarah's (301..304 + ...) neighbours
        state.rate(202, shared_show, 2.0)  # mike hates it
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        assert "Show 05" not in _seed_titles(report, "mike"), "mike's own rating must act on mike"
        # sarah never rated it, so nothing about it changed for her.
        sarah_watched = {"Show 01", "Show 02", "Show 03", "Show 04"}
        assert sarah_watched & _seed_titles(report, "sarah") == sarah_watched

    def test_a_tool_written_rating_is_ignored_over_the_real_wire(self, fakes, tmp_path):
        """The Kometa case, end to end: a fractional value arrives on the XML exactly as it does on a
        real server, and must not act. Proves the guard survives the parse, not just in isolation."""
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 1.6)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        assert self.DISLIKED_TITLE in _seed_titles(report, "sarah")

    def test_the_run_trace_says_why_the_title_dropped_out(self, fakes, tmp_path):
        """The owner-facing half. A seed silently absent is the hardest thing to explain about a run,
        so the trace has to name the reason."""
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 2.0)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        sarah = next(u for u in report.users if u.slug == "sarah")
        recent = sarah.trace["history"]["recent"]
        dropped = next(w for w in recent if w["title"] == self.DISLIKED_TITLE)
        assert dropped["rating"] == 2.0
        assert dropped["rating_blocked"] is True
        kept = next(w for w in recent if w["title"] == "Movie 04")
        assert kept["rating"] is None and kept["rating_blocked"] is False

    def test_the_trace_records_the_policy_the_run_actually_used(self, fakes, tmp_path):
        """Not just which titles dropped — the settings behind the drop, so a run read weeks later
        explains itself without anyone inferring it from Settings as they read today."""
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 2.0)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        sarah = next(u for u in report.users if u.slug == "sarah")
        assert sarah.trace["history"]["ratings"] == {
            "enabled": True,
            "threshold": 2.0,
            "trusted": True,
            "blocked": 1,
            "rated": 1,
            "rated_human": 1,
        }

    def test_the_trace_reports_a_tool_managed_account_rather_than_a_clean_run(self, fakes, tmp_path):
        """The silent no-op this summary exists for. Kometa-style fractional ratings mean NOTHING is
        dropped all run — outwardly identical to a healthy run, including the 1-star that would
        otherwise have acted. Without `trusted` on the trace, nobody could tell the two apart."""
        state, pms_url, _ = fakes
        for movie_id in range(101, 107):  # sarah's watched movies, scored by a tool
            state.rate(201, movie_id, 7.3)
        state.rate(201, 107, 2.0)  # ...and one real 1-star among them
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        ratings = next(u for u in report.users if u.slug == "sarah").trace["history"]["ratings"]
        assert ratings["enabled"] is True, "the setting WAS on — the trace must not read as switched off"
        assert ratings["trusted"] is False
        assert ratings["blocked"] == 0, "a distrusted account drops nothing, including its whole-number ratings"
        assert ratings["rated"] == 7
        assert ratings["rated_human"] == 1, "only the real 1-star could have been typed by a person"

    def test_the_trace_says_the_feature_was_off_rather_than_staying_silent(self, fakes, tmp_path):
        """Third indistinguishable silence: off. Same low rating, same empty drop list, different cause."""
        state, pms_url, _ = fakes
        state.rate(201, self.DISLIKED, 2.0)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=None)

        report = engine_run(ctx, _users(plextv))

        ratings = next(u for u in report.users if u.slug == "sarah").trace["history"]["ratings"]
        assert ratings["enabled"] is False
        assert ratings["threshold"] is None
        assert ratings["blocked"] == 0
        assert ratings["rated"] == 1, "the rating is still READ and shown — it just isn't acted on"


class TestPlexRatingsCannotReachSharedRows:
    """One person's rating must never reshape a row everyone sees.

    Added after review: the exclusion was real (the shared-row `derive_seeds` call simply omits the
    kwarg) but NOTHING pinned it — adding the kwarg back left the entire suite green while one
    person's 1-star quietly deleted a title from a public row. This is the cell of the matrix that
    was missing, and it is the one with the widest blast radius in the whole feature.
    """

    SHARED_SHOW = 301  # sarah already watched it; `_watch` below gives it a second watcher

    def test_a_shared_row_ignores_one_persons_rating(self, fakes, tmp_path):
        state, pms_url, _ = fakes
        _watch(state, 202, self.SHARED_SHOW)  # mike too, clearing the 2-watcher floor
        state.rate(201, self.SHARED_SHOW, 2.0)  # ...and sarah rates it one star
        plex = PlexClient(pms_url, state.owner_token)
        plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)

        _ctx, _users, report = _run(
            plex,
            plextv,
            tmp_path,
            [
                RowSpec(slug="picked", name_template="", size=12),
                RowSpec(slug="popular", name_template="Popular on this server", size=6, shared=True),
            ],
            state.owner_token,
        )

        assert report.ok, [(u.username, u.error) for u in report.users]
        shared = next(u for u in report.users if u.slug == "shared_popular")
        # Show 01 is the ONLY title two people share in this fixture (see
        # `test_a_solo_watched_title_never_reaches_a_shared_row`: sarah/mike overlap is otherwise
        # zero), so it is the single seed the shared row can be built from. If sarah's 1-star reached
        # the aggregate, the row derives nothing and comes back empty — which makes "does it still
        # have picks?" an exact test of the exclusion rather than a proxy for it.
        assert shared.status == "ok", f"the shared row did not build: {shared.status} / {shared.reason}"
        assert shared.picks, (
            "sarah's 1-star emptied a row EVERYONE sees — an individual preference became a "
            "server-wide edit nobody else can see or undo"
        )

    def test_her_own_row_still_honours_it(self, fakes, tmp_path):
        """The other half of the same claim: scoping ratings out of shared rows must not quietly
        scope them out of the per-person rows they exist for."""
        state, pms_url, _ = fakes
        _watch(state, 202, self.SHARED_SHOW)
        state.rate(201, self.SHARED_SHOW, 2.0)
        plex = PlexClient(pms_url, state.owner_token)
        plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
        ctx = EngineContext(
            config=EngineConfig(
                row_size=12,
                min_history=5,
                candidates_pre_rank=40,
                max_seeds=30,
                dislike_threshold=2.0,
                rows=[
                    RowSpec(slug="picked", name_template="", size=12),
                    RowSpec(slug="popular", name_template="Popular on this server", size=6, shared=True),
                ],
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )
        users = [
            UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
            for u in sorted(plextv.list_users(), key=lambda u: u.id)
            if not u.restriction_profile  # the server never passes the engine a profiled account
        ]

        report = engine_run(ctx, users)

        assert "Show 01" not in _seed_titles(report, "sarah"), "her own row must respect her rating"
        # …while the shared row, whose only possible seed is that same title, still builds. A shared
        # row records no seed trace of its own, so its contents are the observable (see the sibling
        # test for why "has picks" is exact here rather than a proxy).
        shared = next(u for u in report.users if u.slug == "shared_popular")
        assert shared.status == "ok" and shared.picks


class TestTrustIsJudgedPerPersonNotPerRow:
    """Whether someone's ratings are believed is an ACCOUNT-level verdict, decided once.

    Added after review, which reproduced the bug: `derive_seeds` is handed a row's SLICE of history
    (narrowed by the row's media and libraries), and `ratings_are_trustworthy` abstains below five
    ratings. Deriving the verdict inside `derive_seeds` therefore let a TV-only row see one whole
    rating, abstain, and act on it — while the account as a whole (five fractional tool-written
    values on the movie side) was correctly disbelieved. Kometa managing one library while the person
    rates in another is the ordinary shape of this, not a corner case.
    """

    def test_a_tv_only_row_inherits_the_accounts_verdict(self, fakes, tmp_path):
        state, pms_url, _ = fakes
        # Tool-written values on her MOVIES (fractional — no person can enter these) …
        for offset, value in enumerate([7.9, 8.8, 6.2, 5.4, 9.1]):
            state.rate(201, 101 + offset, value)
        # … and one whole low rating on a SHOW. Taken alone the show slice looks like a real rater.
        state.rate(201, 301, 2.0)
        plex = PlexClient(pms_url, state.owner_token)
        plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
        ctx = EngineContext(
            config=EngineConfig(
                row_size=12,
                min_history=5,
                candidates_pre_rank=40,
                max_seeds=30,
                dislike_threshold=2.0,
                # A TV-ONLY row: its history slice holds a single rating, too few to judge.
                rows=[RowSpec(slug="tv", name_template="TV", size=12, media="show")],
                rows_defined=True,
            ),
            plex=plex,
            plextv=plextv,
            tmdb=TmdbClient("test-key"),
            history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )
        users = [
            UserProfile(username=u.username, plex_account_id=u.id, user_type=UserType.SHARED)
            for u in sorted(plextv.list_users(), key=lambda u: u.id)
            if not u.restriction_profile  # the server never passes the engine a profiled account
        ]

        report = engine_run(ctx, users)

        assert "Show 01" in _seed_titles(report, "sarah"), (
            "the TV row judged its own slice, abstained, and acted on a rating the account-level "
            "verdict rejects — the row and the person disagree about whose ratings are real"
        )

    def test_the_trace_agrees_with_what_the_row_actually_did(self, fakes, tmp_path):
        """The trace reads the full history and the rows read slices, so a recomputed verdict could
        make the explanation contradict the run in either direction."""
        state, pms_url, _ = fakes
        for offset, value in enumerate([7.9, 8.8, 6.2, 5.4, 9.1]):
            state.rate(201, 101 + offset, value)
        state.rate(201, 301, 2.0)
        ctx, plextv = _rating_ctx(state, pms_url, tmp_path, dislike_threshold=2.0)

        report = engine_run(ctx, _users(plextv))

        sarah = next(u for u in report.users if u.slug == "sarah")
        seeds = {s["title"] for s in sarah.trace.get("seeds", [])}
        for watch in sarah.trace["history"]["recent"]:
            if watch["rating_blocked"]:
                assert watch["title"] not in seeds, f"trace calls {watch['title']} blocked, but it seeded"
            elif watch["title"] in ("Show 01",):
                assert watch["title"] in seeds, "trace stayed silent about a title the run kept — consistent"


class TestLabelReadsAgainstTheRealServerShape:
    """The label read is the whole of Shortlist's identity model, and until now nothing exercised the
    path production actually takes.

    A real PMS returns NO `<Label>` children in the collections listing (recorded in
    `tests/fixtures/pms_collections_listing.json`); they arrive only because plexapi silently re-reads
    each collection behind `collection.labels`. The fake used to serve them inline, so every test
    proved label identity against a server shape Plex does not produce. These drive the real
    implementations through the corrected fake — no mocking of our own helpers.
    """

    def test_the_listing_really_does_not_carry_labels(self, fakes):
        """The premise. If this ever fails, the fake has drifted back to being easier than Plex."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        section = plex.sections()[0]
        state.collections[9101] = FakeCollection(
            rating_key=9101, title="✨ Movies Picked for You", section_id=section.key, labels=["Shortlist_mike"]
        )
        import xml.etree.ElementTree as ET

        import httpx

        raw = httpx.get(
            f"{pms_url}/library/sections/{section.key}/all",
            params={"type": "18"},
            headers={"X-Plex-Token": state.owner_token},
            timeout=10,
        ).text
        listed = [d for d in ET.fromstring(raw) if d.get("ratingKey") == "9101"]
        assert listed, "the collection must be in the listing"
        assert listed[0].findall("Label") == [], "a real PMS serves no labels here"

    def test_owned_collections_still_finds_the_label(self, fakes):
        """...and it works anyway, because plexapi re-reads. This is the production path."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9102] = FakeCollection(
            rating_key=9102, title="✨ Movies Picked for You", section_id=state.section_id, labels=["Shortlist_mike"]
        )

        assert "mike" in plex.owned_collections("shortlist")

    def test_confirm_unlabelled_says_no_for_a_labelled_collection(self, fakes):
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9103] = FakeCollection(
            rating_key=9103, title="✨ Movies Picked for You", section_id=state.section_id, labels=["Shortlist_mike"]
        )
        collection = plex._section_collections(plex.sections()[0])[-1]

        assert plex.confirm_unlabelled(collection, "shortlist") is False

    def test_confirm_unlabelled_says_yes_for_a_genuinely_unlabelled_one(self, fakes):
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9104] = FakeCollection(
            rating_key=9104, title="✨ Movies Picked for You", section_id=state.section_id, labels=[]
        )
        collection = plex._section_collections(plex.sections()[0])[-1]

        assert plex.confirm_unlabelled(collection, "shortlist") is True

    def test_confirm_unlabelled_refuses_when_the_server_cannot_be_read(self, fakes):
        """ "I could not ask" must never authorise a delete."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9105] = FakeCollection(
            rating_key=9105, title="✨ Movies Picked for You", section_id=state.section_id, labels=["Shortlist_mike"]
        )
        collection = plex._section_collections(plex.sections()[0])[-1]
        del state.collections[9105]  # the re-read now 404s

        assert plex.confirm_unlabelled(collection, "shortlist") is False

    def test_owned_row_surfaces_reports_label_marker_and_flags(self, fakes):
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9106] = FakeCollection(
            rating_key=9106,
            title="✨ Movies Picked for You" + row_marker(202),
            section_id=state.section_id,
            labels=["Shortlist_mike"],
            promoted_recommended=True,
            promoted_shared_home=True,
        )

        row = next(r for r in plex.owned_row_surfaces("shortlist") if r["rating_key"] == 9106)

        assert row["label"] == "Shortlist_mike"
        assert row["marked"] is True
        assert (row["recommended"], row["own_home"], row["shared_home"]) == (True, False, True)

    def test_owned_row_surfaces_finds_a_marked_row_that_lost_its_label(self, fakes):
        """The state issue #76 looked like: ours by title, invisible to every `label!=` filter."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9107] = FakeCollection(
            rating_key=9107, title="✨ Movies Picked for You" + row_marker(203), section_id=state.section_id, labels=[]
        )

        row = next(r for r in plex.owned_row_surfaces("shortlist") if r["rating_key"] == 9107)

        assert (row["label"], row["marked"]) == ("", True)

    def test_owned_row_surfaces_skips_a_foreign_collection(self, fakes):
        """Kometa coexistence (rule 4): neither our label nor our marker means it is not ours."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9108] = FakeCollection(
            rating_key=9108, title="Kometa: Best of the 90s", section_id=state.section_id, labels=["Overlay"]
        )

        assert not [r for r in plex.owned_row_surfaces("shortlist") if r["rating_key"] == 9108]

    def test_flags_false_skips_the_surface_read_entirely(self, fakes):
        """The cheap mode drift uses — one round-trip per collection is worth paying to answer
        "where is this row showing", and not worth paying to count rows."""
        state, pms_url, _tmdb = fakes
        plex = PlexClient(pms_url, state.owner_token)
        state.collections[9109] = FakeCollection(
            rating_key=9109, title="✨ Movies Picked for You", section_id=state.section_id, labels=["Shortlist_mike"]
        )

        row = next(r for r in plex.owned_row_surfaces("shortlist", flags=False) if r["rating_key"] == 9109)

        assert "recommended" not in row and row["label"] == "Shortlist_mike"


def test_an_account_left_alone_keeps_its_own_filter_and_everyone_else_still_hides_from_it(fakes, tmp_path):
    """ "Don't change this person's Plex sharing settings" (discussion #92), end to end.

    The reporter's shape: a managed account whose own "allow only" label list is what keeps Shortlist
    out of its view, and whose owner wants our excludes out of the Restrictions tab. Three things have
    to be true afterwards, and only the first is the feature:

    1. Nothing of ours is left in that account's filters, and its own conditions are byte-identical.
    2. It is still excluded on EVERY OTHER account — leaving one account alone must not make its
       owner's row visible to the rest of the server.
    3. The run still promotes. One account nobody wants managed cannot be a promotion blocker.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )

    # Account 203 already carries an allow-only list of its owner's AND an exclude of ours from an
    # earlier run — exactly the state the setting has to clean up.
    state.users[203].filters["filterMovies"] = "label=Kids%20Safe|label!=Shortlist_sarah"
    state.users[203].filters["filterTelevision"] = "label=Kids%20Safe"
    ctx.unmanaged_account_ids = {203}

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah, mike])

    assert report.ok
    # 1. Their own filter is theirs again — the allow list survives byte for byte.
    assert state.users[203].filters["filterMovies"] == "label=Kids%20Safe"
    assert state.users[203].filters["filterTelevision"] == "label=Kids%20Safe"

    # 2. Everyone else still hides the left-alone account nothing changed about.
    assert "Shortlist_sarah" in state.users[202].filters["filterMovies"]
    assert "Shortlist_mike" in state.users[201].filters["filterMovies"]

    # 3. The run promoted rather than treating an unmanaged account as a blocker.
    assert not report.promotion_blockers
    assert plex.owned_collections()["sarah"].rating_keys

    # And the widening write is auditable like every other share write (rule 10).
    assert report.filter_writes[203]["fields"]["filterMovies"][1] == "label=Kids%20Safe"


def test_leaving_an_account_alone_is_a_no_op_on_the_second_run(fakes, tmp_path):
    """Steady state writes nothing. Without this the nightly run would re-write the same filter for
    ever, and every run's audit would claim a change that never happened."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    state.users[203].filters["filterMovies"] = "label=Kids%20Safe|label!=Shortlist_sarah"
    ctx.unmanaged_account_ids = {203}
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)

    engine_run(ctx, [sarah])
    second = engine_run(ctx, [sarah])

    assert 203 not in second.filter_writes
    assert state.users[203].filters["filterMovies"] == "label=Kids%20Safe"


def test_leaving_an_account_alone_does_not_make_that_persons_own_row_public(fakes, tmp_path):
    """The leak direction, and the one that would be worst to get wrong.

    "Leave this account's sharing alone" is about what THEY can see. It must not touch what everyone
    else can see of THEM: their own row still has to be excluded on every other account's filter.
    The two are separate by construction — excludes are derived from the rows that exist on the PMS,
    not from who Shortlist manages — but nothing pinned it, and a "skip this user entirely" reading
    of the setting is the obvious wrong implementation.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    # mike is left alone AND still gets a row of his own.
    ctx.unmanaged_account_ids = {202}

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah, mike])

    assert report.ok
    assert plex.owned_collections()["mike"].rating_keys, "mike should still get his row"

    # Everyone else hides mike's row, exactly as if he were managed.
    for account_id in (201, 203):
        filters = state.users[account_id].filters
        assert "Shortlist_mike" in filters["filterMovies"], f"account {account_id} can see mike's row"
        assert "Shortlist_mike" in filters["filterTelevision"], f"account {account_id} can see mike's row"

    # Mike's own filter stays empty of ours — he is the one account we do not write to.
    assert shortlist_labels_in(state.users[202].filters["filterMovies"], "Shortlist") == set()

    # And proof through their eyes: mike's row is not on anyone else's Home.
    mike_rows = set(plex.owned_collections()["mike"].rating_keys)
    for account_id in (201, 203):
        visible = {collection_id_from_hub(h) for h in plex.user_hubs(f"server-{account_id}")}
        assert not (mike_rows & visible), f"account {account_id} sees mike's row on their Home"


def test_an_account_that_is_both_switched_off_and_left_alone_keeps_the_shared_row_hidden(fakes, tmp_path):
    """The fourth cell of the `enabled` / `manage_sharing` matrix — the only one where they interact.

    Switching someone off normally hides even the PUBLIC shared rows from them
    (`hide_shared_from_disabled`); leaving them alone means we do not write their filter at all. So
    for a user with both switches off, "left alone" wins and the disabled-user hiding never applies.
    That is the intended precedence — "don't touch this account" is the stronger statement — but it
    is the cell where the docs' unconditional "off still writes their filters" stops being true, so
    it is pinned rather than left to be rediscovered.

    What must NOT change is the restricted shared row: its exclude is the only thing keeping the row
    away from someone outside its audience, so it survives both switches.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
    )
    # 203 is switched off in Shortlist AND left alone, and already carries both kinds of exclude.
    state.users[203].filters["filterMovies"] = "label!=shortlist__shared_date_night,Shortlist_sarah|label=Kids"
    ctx.disabled_account_ids = {203}
    ctx.unmanaged_account_ids = {203}

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    report = engine_run(ctx, [sarah])

    assert report.ok
    after = state.users[203].filters["filterMovies"]
    # The per-person exclude goes (that is the setting doing its job)...
    assert "Shortlist_sarah" not in after
    # ...the restricted shared row's does NOT, and their own condition is untouched.
    assert "shortlist__shared_date_night" in after
    assert "label=Kids" in after


def test_a_filter_plex_stores_but_ignores_is_caught_and_reported(fakes, tmp_path, monkeypatch):
    """Discussion #88's shape, which nothing could see before.

    Six Plex Home accounts, restriction profile None on every one, each seeing all six per-person
    rows. Everything Shortlist checked said the server was healthy: the filters were written, plex.tv
    stored them, and the read-back before promotion confirmed the exclusions were present. The one
    check that looks through a real account's eyes was gated on the account having a restriction
    profile — so the accounts we successfully write filters FOR had no verification at all, and the
    first person to notice was a user, not the owner.

    Modelled by making the fake PMS store the filter and not act on it (`excluded_labels` -> empty),
    which is precisely the difference between "stored" and "enforced".
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)

    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        token_for_user=lambda profile: f"server-{profile.plex_account_id}",
    )

    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    # First run builds the rows and writes everyone's filters, with Plex behaving normally.
    engine_run(ctx, [sarah, mike])
    assert plex.owned_collections()["sarah"].rating_keys

    # Now Plex starts storing the exclusions without applying them. Nothing else changes: the filter
    # strings stay exactly where they were, so every existing check still reports a healthy server.
    monkeypatch.setattr(type(state), "excluded_labels", staticmethod(lambda _user: set()))
    report = engine_run(ctx, [sarah, mike])

    # Both account KINDS must be represented: the check samples one per type, and the managed
    # (Plex Home) arm is the exact shape #88 reported. Asserting only "something was reported" would
    # stay green if the canary/managed path broke entirely.
    assert sorted(report.filters_not_enforced) == ["canary", "sarah"]
    assert report.filters_enforcement_measured is True, "a filter that is stored but ignored has to be reported"
    exposed = next(iter(report.filters_not_enforced.values()))
    assert exposed, "the finding names the rows the account can actually see"
    # And it stays a REPORT: the removed write gate is not coming back, so the run still completes
    # and still promotes.
    assert report.ok
    assert not report.promotion_blockers


def test_a_server_that_enforces_its_filters_reports_nothing(fakes, tmp_path):
    """The other half: on a healthy server this check must be silent, or it is noise that gets
    ignored on the one night it matters."""
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    ctx = EngineContext(
        config=EngineConfig(row_size=12, min_history=5, candidates_pre_rank=40, max_seeds=12),
        plex=plex,
        plextv=plextv,
        tmdb=TmdbClient("test-key"),
        history_source=ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token),
        curator=NullCurator(),
        snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        token_for_user=lambda profile: f"server-{profile.plex_account_id}",
    )
    sarah = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
    mike = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)

    engine_run(ctx, [sarah, mike])
    report = engine_run(ctx, [sarah, mike])

    assert report.filters_not_enforced == {}


def test_a_run_leaves_a_scheduled_off_row_hidden_and_intact(fakes, tmp_path):
    """A row on a day off must survive a full run: hidden, but not rebuilt and not deleted.

    This is the probe that shaped "When it appears" (issue #102). A row demoted BEHIND the engine's
    back is put straight back by the next run — promotion is computed from the row's placement, not
    from what is currently on the server. So the schedule cannot be a sweep bolted on the side; it
    has to resolve into the placement itself, which is what `context_builder._build_rows` does.

    The three things asserted here are the three ways this could go wrong on a real server: the row
    comes back anyway, the row gets deleted, or the row gets rebuilt (paying up to 26s per
    membership write on a large TV library, nightly, for a row nobody can see).
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    rows = [RowSpec(slug="picked", name_template="Picked for You", size=8)]
    ctx, users, report = _run(plex, plextv, tmp_path, rows, state.owner_token)
    assert report.ok

    target = next(u for u in users if any(r.slug == u.slug and r.status == "ok" and r.picks for r in report.users))
    label = f"Shortlist_{target.slug}"
    keys = {c.rating_key for c in state.collections.values() if any(t.lower() == label.lower() for t in c.labels)}
    assert keys, "nothing was delivered, so this proves nothing"
    token = f"server-{target.plex_account_id}"
    titles_before = {k: list(state.collections[k].item_keys) for k in keys}

    def on_home() -> set[int]:
        return {collection_id_from_hub(h) for h in plex.user_hubs(token)}

    assert keys <= on_home(), "the row should be up before its day off"

    # The day turns over: the server resolves this row's schedule to `off` and runs as normal.
    ctx.config.rows = [replace(rows[0], placement="off", placement_friends="off")]
    off_report = engine_run(ctx, users)

    assert off_report.ok
    assert not (keys & on_home()), "a scheduled-off row must not be on anyone's Home"
    for key in keys:
        assert key in state.collections, "hidden is not deleted — the row must survive its day off"
        assert state.collections[key].item_keys == titles_before[key], "a hidden row must not be rebuilt"

    # ...and the following day it comes back, still without a rebuild.
    ctx.config.rows = rows
    back_report = engine_run(ctx, users)

    assert back_report.ok
    assert keys <= on_home(), "the row must return on its next day"
    for key in keys:
        assert state.collections[key].item_keys == titles_before[key], "coming back must not rebuild it either"


def test_a_scoped_run_does_not_resurrect_a_scheduled_off_seeded_row(fakes, tmp_path):
    """The `{top_seed}` cell of "a run must not undo the midnight schedule" (issue #102).

    A `{top_seed}` title is different every run, so it cannot be re-rendered without picks — and a run
    that does not REBUILD a row stamps no title for it. The collection then matches no spec, and
    promotion's no-spec fallback SHOWS it. So the midnight job hides the row at 00:00 and the 03:30
    run puts it straight back on Friends' Home for the rest of its off day.

    The static-titled sibling of this test passes either way, because a static title can always be
    re-rendered — which is exactly why that test did not catch this.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    plextv = PlexTvClient(state.owner_token, plex.machine_id, min_write_interval=0.0)
    rows = [
        RowSpec(slug="picked", name_template="Picked for You", size=6),
        RowSpec(slug="seeded", name_template="Because you watched {top_seed}", size=6),
    ]
    ctx, users, report = _run(plex, plextv, tmp_path, rows, state.owner_token)
    assert report.ok

    target = next(u for u in users if any(r.slug == u.slug and r.status == "ok" and r.picks for r in report.users))
    label = f"Shortlist_{target.slug}"
    seeded = {
        c.rating_key
        for c in state.collections.values()
        if any(t.lower() == label.lower() for t in c.labels) and c.title.startswith("Because you watched")
    }
    assert seeded, "the {top_seed} row was never delivered, so this proves nothing"
    token = f"server-{target.plex_account_id}"

    def on_home() -> set[int]:
        return {collection_id_from_hub(h) for h in plex.user_hubs(token)}

    assert seeded <= on_home()

    # The server persists what a run delivered and hands the next context that ledger back
    # (`context_builder._delivered_keys`). The fake context is built fresh, so model it here — without
    # it this test proves nothing about production, where the ledger is exactly what identifies a row
    # whose title cannot be re-rendered.
    from shortlist.engine.pipeline import live_delivered_keys

    ctx.delivered_keys = live_delivered_keys(ctx, report)
    assert any(row_slug == "seeded" for (_u, row_slug, _l) in ctx.delivered_keys), "ledger did not record it"

    # Midnight: the schedule resolves this row to `off` and takes it down.
    ctx.config.rows = [rows[0], replace(rows[1], placement="off", placement_friends="off")]
    for section in plex.sections():
        for collection in plex.find_owned_collections(section, label):
            if collection.title.startswith("Because you watched"):
                plex.demote_all(collection, reason="scheduled off")
    assert not (seeded & on_home())

    # 03:30: a run scoped to the OTHER row — so nothing re-stamps the seeded row's title.
    ctx.config.build_only = ["picked"]
    later = engine_run(ctx, users)

    assert later.ok
    assert not (seeded & on_home()), "the 03:30 run put a scheduled-off {top_seed} row back on Home"


def test_two_labels_go_on_in_one_write_and_do_not_disturb_a_foreign_label(fakes):
    """The claim the label batching rests on, proved against real plexapi + the fake PMS.

    The unit tests can only assert what OUR code hands plexapi. The load-bearing behaviour is
    plexapi's: `editTags` concatenates `existing + new` and PUTs an ABSOLUTE tag set. So a plexapi
    upgrade that changed it would leave every mock-based test green while a batched write silently
    dropped a co-managing tool's label — or, far worse, another user's `shortlist_<user>` label,
    which is the only thing hiding that row from the rest of the server.

    Two things are asserted, and the second is the safety one:
      1. both labels arrive in a SINGLE PUT (the ~10%-of-a-run saving), and
      2. a label already on the collection SURVIVES that write.
    """
    state, pms_url, _tmdb_app = fakes
    plex = PlexClient(pms_url, state.owner_token)
    section = next(s for s in plex.sections() if s.type == "movie")
    item = section.all(maxresults=1)[0]

    puts: list[str] = []
    session = plex._server._session
    original = session.request

    def counting(method, url, **kwargs):
        if method.upper() == "PUT" and "/all" in url:
            puts.append(url.split("?")[0])
        return original(method, url, **kwargs)

    session.request = counting
    collection = None
    try:
        collection = plex.create_collection(section, "zz batched label probe", [item])
        # A co-managing tool's label, already on the row before we touch it.
        collection.addLabel("Kometa_managed")
        collection.reload()
        puts.clear()

        stored = plex.stored_label(collection, "shortlist_bob", extra="shortlist")

        assert len(puts) == 1, f"both labels must ride in ONE write, got {len(puts)}: {puts}"
        assert stored.lower() == "shortlist_bob", "the CRITICAL label's casing is what filters exclude"
        collection.reload()
        tags = {t.tag.lower() for t in collection.labels}
        assert "shortlist_bob" in tags
        assert "shortlist" in tags
        assert "kometa_managed" in tags, (
            "a label write is an ABSOLUTE set — a foreign label must survive it, or this write "
            "would be capable of dropping another user's shortlist_<user> label too"
        )
    finally:
        if collection is not None:
            collection.delete()
