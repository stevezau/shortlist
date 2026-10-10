"""A "Because you watched {top_seed}" row is titled after the watch it was built from (issue #133).

Runs the real engine against the in-process fake PMS + plex.tv and a small controllable TMDB, several
runs in a row, carrying picks, recipes and the delivery ledger between runs the way the server does.
Every assertion reads the collection the fake PMS actually holds — the title a person sees and the
items in it — because the bug this pins was invisible anywhere else: the run reported fresh picks
while Plex kept last week's row.

The shape: only "similar to X" picks carry a seed. Discover and web-search picks carry none. When a
new watch has no look-alikes in the library, nothing in the rebuilt row carried a seed, the title
rendered empty, and delivery left the old collection — old title, old items — on Plex until some
later watch happened to have look-alikes.

World (fake seed_state): Movies = tmdb 9001-9030; TV Shows = tmdb 7001-7030.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta

import pytest
from fastapi import FastAPI

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.context import EngineContext
from shortlist.engine.curator import NullCurator
from shortlist.engine.delivery import strip_marker
from shortlist.engine.models import EngineConfig, MediaType, RowSpec, UserProfile, UserType, WatchedItem
from shortlist.engine.pipeline import run as engine_run
from tests.conftest import NOW
from tests.fakes.fake_plex import make_fake_plex, make_fake_plextv, seed_state
from tests.fakes.file_stores import FileSnapshotStore
from tests.uvicorn_thread import UvicornThread

pytestmark = pytest.mark.integration

SARAH = 201

MOVIE_A, MOVIE_B, MOVIE_C = 9001, 9002, 9003
SHOW_T1, SHOW_T2, SHOW_T3 = 7001, 7002, 7003
# Look-alikes the library holds.
IN_LIBRARY = {
    ("movie", MOVIE_A): list(range(9004, 9009)),
    ("movie", MOVIE_C): list(range(9014, 9019)),
    ("tv", SHOW_T3): list(range(7004, 7009)),
}
# Look-alikes TMDB returns that the library does NOT hold — a small library.
NOT_IN_LIBRARY = {
    ("movie", MOVIE_B): list(range(60001, 60006)),
    ("tv", SHOW_T1): list(range(70001, 70006)),
    ("tv", SHOW_T2): list(range(70011, 70016)),
}
DISCOVER = {"movie": list(range(9019, 9029)), "tv": list(range(7019, 7029))}


def _tmdb(similar: dict[tuple[str, int], list[int]]) -> FastAPI:
    app = FastAPI()

    def item(kind: str, tmdb_id: int) -> dict:
        key, date = ("title", "release_date") if kind == "movie" else ("name", "first_air_date")
        return {
            "id": tmdb_id,
            key: f"{kind}-{tmdb_id}",
            "vote_average": 7.5,
            "vote_count": 5000,
            "genre_ids": [1],
            date: "2015-06-01",
        }

    @app.get("/genre/{kind}/list")
    def genres(kind: str) -> dict:
        return {"genres": [{"id": 1, "name": "Drama"}]}

    @app.get("/discover/{kind}")
    def discover(kind: str) -> dict:
        return {"results": [item(kind, i) for i in DISCOVER[kind]]}

    @app.get("/{kind}/{tmdb_id}")
    def details(kind: str, tmdb_id: int) -> dict:
        return {"id": tmdb_id, "genres": [{"id": 1, "name": "Drama"}], "credits": {"cast": []}}

    @app.get("/{kind}/{tmdb_id}/{endpoint}")
    def suggestions(kind: str, tmdb_id: int, endpoint: str) -> dict:
        if endpoint not in ("recommendations", "similar"):
            return {}
        return {"results": [item(kind, i) for i in similar.get((kind, tmdb_id), [])]}

    return app


class _History:
    def __init__(self) -> None:
        self.items: list[WatchedItem] = []

    def fetch(self, user, *, min_completion: float = 0.0, since=None) -> list[WatchedItem]:
        return list(self.items)


@dataclass
class Shelf:
    """What sarah's row looks like on the fake PMS after one run, per library."""

    titles: dict[str, str] = field(default_factory=dict)
    members: dict[str, set[int]] = field(default_factory=dict)
    rewritten: dict[str, bool] = field(default_factory=dict)


class Harness:
    def __init__(self, monkeypatch, tmp_path, similar: dict[tuple[str, int], list[int]]):
        self.state = seed_state()
        self.servers = [
            UvicornThread(make_fake_plex(self.state)).start(),
            UvicornThread(make_fake_plextv(self.state)).start(),
            UvicornThread(_tmdb(similar)).start(),
        ]
        pms, plextv, tmdb = self.servers
        monkeypatch.setattr("shortlist.engine.clients.plextv.PLEXTV", plextv.url)
        monkeypatch.setattr("shortlist.engine.clients.tmdb.API", tmdb.url)
        self.history = _History()
        plex = PlexClient(pms.url, self.state.owner_token)
        spec = RowSpec(
            slug="because",
            name_template="Because you watched {top_seed}",
            size=6,
            media="both",
            refresh_days=1,
            max_seeds=2,  # one film and one show — what the row editor recommends for a named movies+TV row
            seed_window=1,
            candidate_sources=["tmdb_similar", "tmdb_discover"],
        )
        self.ctx = EngineContext(
            config=EngineConfig(min_history=1, candidates_pre_rank=50, rows=[spec], rows_defined=True),
            plex=plex,
            plextv=PlexTvClient(self.state.owner_token, plex.machine_id, min_write_interval=0.0),
            tmdb=TmdbClient("test-key"),
            history_source=self.history,
            curator=NullCurator(),
            snapshots=FileSnapshotStore(tmp_path / "snapshots"),
        )
        self._previous_members: dict[str, set[int]] = {}

    def stop(self) -> None:
        for server in self.servers:
            server.stop()

    def watch(self, *tmdb_ids: int) -> None:
        """Sarah's history, oldest first: the LAST id is her newest watch."""
        n = len(tmdb_ids)
        self.history.items = [self._watched(t, days_ago=n - i) for i, t in enumerate(tmdb_ids)]

    def _watched(self, tmdb_id: int, days_ago: int) -> WatchedItem:
        is_movie = tmdb_id >= 9000
        fake = next(m for m in (self.state.movies if is_movie else self.state.shows).values() if m.tmdb_id == tmdb_id)
        return WatchedItem(
            title=fake.title,
            media_type=MediaType.MOVIE if is_movie else MediaType.SHOW,
            watched_at=NOW - timedelta(days=days_ago),
            tmdb_id=tmdb_id,
            rating_key=fake.rating_key,
            viewed_leaf_count=None if is_movie else 10,
            leaf_count=None if is_movie else 10,
        )

    def named(self, tmdb_id: int) -> str:
        library = self.state.movies if tmdb_id >= 9000 else self.state.shows
        return "Because you watched " + next(m.title for m in library.values() if m.tmdb_id == tmdb_id)

    def run(self) -> Shelf:
        self.ctx.run_day = 0  # the direct-call sentinel: every row is due
        report = engine_run(self.ctx, [UserProfile(username="sarah", plex_account_id=SARAH, user_type=UserType.SHARED)])
        assert report.ok, [(u.username, u.error) for u in report.users]
        user = report.users[0]
        # Carried to the next run the way `context_builder._previous_picks` does: per (user, row, library),
        # tonight's group replacing last night's, the ratingKey remapped at delivery.
        groups: dict[tuple[str, str, str], list] = {}
        for p in sorted(user.picks, key=lambda p: p.rank):
            key = ("sarah", p.collection_slug, str(p.section_key))
            groups.setdefault(key, []).append(replace(p, rating_key=0, section_key=str(p.section_key)))
        self.ctx.previous_picks = {**self.ctx.previous_picks, **groups}
        self.ctx.previous_recipes = {k: v[0].recipe for k, v in self.ctx.previous_picks.items() if v and v[0].recipe}
        for entry in user.breakdown:
            if entry.get("rating_key") and entry.get("row_slug") and entry.get("library_key"):
                self.ctx.delivered_keys[("sarah", entry["row_slug"], str(entry["library_key"]))] = int(
                    entry["rating_key"]
                )

        tmdb_by_key = {m.rating_key: m.tmdb_id for s in self.state.sections.values() for m in s.items.values()}
        shelf = Shelf()
        for collection in self.state.collections.values():
            if not any(label.lower() == "shortlist_sarah" for label in collection.labels):
                continue
            library = self.state.sections[collection.section_id].title
            shelf.titles[library] = strip_marker(collection.title)
            shelf.members[library] = {tmdb_by_key[k] for k in collection.item_keys}
        shelf.rewritten = {lib: members != self._previous_members.get(lib) for lib, members in shelf.members.items()}
        self._previous_members = shelf.members
        return shelf


@pytest.fixture
def harness(monkeypatch, tmp_path):
    made: list[Harness] = []

    def make(similar: dict[tuple[str, int], list[int]]) -> Harness:
        h = Harness(monkeypatch, tmp_path, similar)
        made.append(h)
        return h

    yield make
    for h in made:
        h.stop()


class TestTheTitleNamesTheWatchTheRowWasBuiltFrom:
    def test_a_new_watch_with_look_alikes_in_the_library_renames_the_row(self, harness):
        h = harness({**IN_LIBRARY})
        h.watch(SHOW_T3, MOVIE_A)
        first = h.run()
        h.watch(SHOW_T3, MOVIE_A, MOVIE_C)
        second = h.run()

        assert first.titles["Movies"] == h.named(MOVIE_A)
        assert second.titles["Movies"] == h.named(MOVIE_C)
        assert second.rewritten["Movies"]

    def test_a_rerun_with_nothing_new_watched_keeps_the_title(self, harness):
        """What the reporter saw after the nightly run, and by design: the row follows the newest finished
        watch, so a manual run with nothing new watched names the same one."""
        h = harness({**IN_LIBRARY})
        h.watch(SHOW_T3, MOVIE_A)
        first = h.run()
        second = h.run()

        assert second.titles == first.titles == {"Movies": h.named(MOVIE_A), "TV Shows": h.named(SHOW_T3)}

    def test_a_new_watch_with_no_look_alikes_in_the_library_still_renames_the_row(self, harness):
        """The #133 freeze. B's look-alikes are not in the library, so nothing in the rebuilt row carries a
        seed. The row used to render no name, and delivery left "Because you watched A" — and A's items —
        on Plex, rerun after rerun, until a later watch happened to have look-alikes."""
        h = harness({**IN_LIBRARY, **NOT_IN_LIBRARY})
        h.watch(SHOW_T1, MOVIE_A)
        h.run()
        h.watch(SHOW_T1, MOVIE_A, MOVIE_B)
        after_b = h.run()
        rerun = h.run()

        assert after_b.titles["Movies"] == h.named(MOVIE_B)
        assert after_b.rewritten["Movies"], "the row must be rebuilt for B, not left holding A's picks"
        assert not set(IN_LIBRARY[("movie", MOVIE_A)]) & after_b.members["Movies"], "A's look-alikes outlived A"
        assert rerun.titles["Movies"] == h.named(MOVIE_B)

    def test_a_row_named_without_a_seeded_pick_rebuilds_when_the_watch_moves_on(self, harness):
        """Last night's row carried no seeded pick — it was named after B, the watch it was built from.
        When C replaces B, the row must rebuild for C, not carry two-thirds of B's row forward under C's
        name: it must hold exactly what a first build from the same history holds."""
        h = harness({**IN_LIBRARY, **NOT_IN_LIBRARY})
        h.watch(SHOW_T1, MOVIE_A, MOVIE_B)
        h.run()
        h.watch(SHOW_T1, MOVIE_A, MOVIE_B, MOVIE_C)
        after_c = h.run()
        first_build = harness({**IN_LIBRARY, **NOT_IN_LIBRARY})
        first_build.watch(SHOW_T1, MOVIE_A, MOVIE_B, MOVIE_C)
        built_fresh = first_build.run()

        assert after_c.titles["Movies"] == h.named(MOVIE_C)
        assert after_c.members["Movies"] == built_fresh.members["Movies"]

    def test_the_tv_row_names_their_show_not_a_film(self, harness):
        """The reporter's side note. Their show's look-alikes were not in a 309-show library, so the TV row
        borrowed the film's name and only moved when they watched a film."""
        h = harness({**IN_LIBRARY, **NOT_IN_LIBRARY})
        h.watch(SHOW_T1, MOVIE_A)
        first = h.run()
        h.watch(SHOW_T1, MOVIE_A, SHOW_T2)
        second = h.run()
        h.watch(SHOW_T1, MOVIE_A, SHOW_T2, SHOW_T3)
        third = h.run()

        assert first.titles == {"Movies": h.named(MOVIE_A), "TV Shows": h.named(SHOW_T1)}
        assert second.titles == {"Movies": h.named(MOVIE_A), "TV Shows": h.named(SHOW_T2)}
        assert third.titles == {"Movies": h.named(MOVIE_A), "TV Shows": h.named(SHOW_T3)}
