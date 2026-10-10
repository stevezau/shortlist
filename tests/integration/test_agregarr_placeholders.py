"""Agregarr's trailer stand-ins ("placeholders") are not the titles they stand in for (issue #151).

Agregarr can drop a short trailer into Plex for a film or show the server does not have. Plex matches it
to the real title, so it carries the film's own `tmdb://` guid. Agregarr labels every one
`trailer-placeholder`: a movie stand-in is its own item (a `{edition-Trailer}` file), and a TV stand-in is a
Season 00 / S00E00 trailer with the label on the SHOW.

Run against the fake PMS with the real `PlexClient`, because what is under test is how Plex's answers are
read. The label filter's behaviour is recorded in `tests/fixtures/pms_label_filter.json`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import PLACEHOLDER_LABEL, PlexClient
from shortlist.engine.history import ShareTokenWatchSource
from shortlist.engine.models import MediaType, UserProfile, UserType
from tests.fakes.fake_plex import FakeHistoryEntry, FakeMovie, FakePlexState, make_fake_plex, seed_state
from tests.uvicorn_thread import UvicornThread

pytestmark = pytest.mark.integration

SARAH = UserProfile(username="sarah", plex_account_id=201, user_type=UserType.SHARED)
REAL_FILM = 120  # seeded, and nobody has watched it
TRAILER = 6001
PLACEHOLDER_FILM, PLACEHOLDER_SHOW, ARRIVING_SHOW = 6002, 6003, 6004


def _trailer_of(rating_key: int, film: FakeMovie) -> FakeMovie:
    return FakeMovie(
        rating_key=rating_key,
        title=film.title,
        year=film.year,
        added_at=film.added_at + 1,
        tmdb_id=film.tmdb_id,
        audience_rating=film.audience_rating,
        labels=[PLACEHOLDER_LABEL],
        edition_title="Trailer",
    )


def _show(state: FakePlexState, rating_key: int, tmdb_id: int, *, seasons: dict[int, int], labelled: bool = True):
    episodes = [(season, n) for season, count in seasons.items() for n in range(count)]
    state.shows[rating_key] = FakeMovie(
        rating_key=rating_key,
        title=f"Show {rating_key}",
        year=2026,
        added_at=1_800_000_000,
        tmdb_id=tmdb_id,
        audience_rating=7.0,
        media_type="show",
        leaf_count=len(episodes),
        labels=[PLACEHOLDER_LABEL] if labelled else [],
    )
    for offset, (season, n) in enumerate(episodes, start=1):
        state.shows[rating_key * 100 + offset] = FakeMovie(
            rating_key=rating_key * 100 + offset,
            title=f"S{season:02}E{n:02}",
            year=2026,
            added_at=1_800_000_000,
            tmdb_id=tmdb_id * 100 + offset,
            audience_rating=0.0,
            media_type="episode",
            grandparent_rating_key=rating_key,
            parent_index=season,
            index=n,
        )


@pytest.fixture
def state() -> FakePlexState:
    state = seed_state()
    film = state.movies[REAL_FILM]
    state.movies[TRAILER] = _trailer_of(TRAILER, film)
    state.movies[PLACEHOLDER_FILM] = FakeMovie(
        rating_key=PLACEHOLDER_FILM,
        title="Not Here Yet",
        year=2026,
        added_at=1_800_000_000,
        tmdb_id=99_001,
        audience_rating=7.0,
        labels=[PLACEHOLDER_LABEL],
        edition_title="Trailer",
    )
    # Season 00 / S00E00 only — and Plex counts Season 0 in leafCount (pms_label_filter.json).
    _show(state, PLACEHOLDER_SHOW, 99_002, seasons={0: 1})
    # Real episodes have landed in the show and Agregarr has not taken its label off yet.
    _show(state, ARRIVING_SHOW, 99_003, seasons={0: 1, 1: 8})
    return state


@pytest.fixture
def server(state: FakePlexState):
    server = UvicornThread(make_fake_plex(state)).start()
    yield server
    server.stop()


@pytest.fixture
def plex(server: UvicornThread, state: FakePlexState) -> PlexClient:
    return PlexClient(server.url, state.owner_token)


def _section(plex: PlexClient, kind: MediaType):
    return plex.sections_by_type()[kind]


def _watch(state: FakePlexState, *keys: int) -> None:
    for key in keys:
        state.history.append(
            FakeHistoryEntry(account_id=SARAH.plex_account_id, rating_key=key, viewed_at=1_760_000_000)
        )


def _source(plex: PlexClient, state: FakePlexState) -> ShareTokenWatchSource:
    plextv = MagicMock()
    plextv.shared_server_tokens.return_value = {SARAH.plex_account_id: f"server-{SARAH.plex_account_id}"}
    return ShareTokenWatchSource(plex, plextv, owner_token=state.owner_token)


def _watched(plex: PlexClient, state: FakePlexState) -> set[tuple[int, MediaType]]:
    return {(w.tmdb_id, w.media_type) for w in _source(plex, state).fetch(SARAH, min_completion=0.9)}


class TestPlaceholderKeys:
    def test_a_library_without_placeholders_has_none(self, plex: PlexClient, state: FakePlexState):
        for key in (TRAILER, PLACEHOLDER_FILM, PLACEHOLDER_SHOW, ARRIVING_SHOW):
            state.movies.pop(key, None)
            state.shows.pop(key, None)
        assert plex.placeholder_keys(_section(plex, MediaType.MOVIE).key, MediaType.MOVIE) == frozenset()

    def test_every_labelled_movie_is_a_placeholder(self, plex: PlexClient):
        keys = plex.placeholder_keys(_section(plex, MediaType.MOVIE).key, MediaType.MOVIE)
        assert keys == {TRAILER, PLACEHOLDER_FILM}

    def test_the_label_matches_in_whatever_case_plex_stored_it(self, plex: PlexClient, state: FakePlexState):
        """Plex title-cases a tag it creates, and its label filter is case-insensitive (measured)."""
        state.movies[TRAILER].labels = ["Trailer-Placeholder"]
        keys = plex.placeholder_keys(_section(plex, MediaType.MOVIE).key, MediaType.MOVIE)
        assert TRAILER in keys

    def test_a_labelled_show_is_one_only_while_the_trailer_is_its_only_episode(self, plex: PlexClient):
        """The TV label is on the SHOW and stays until Agregarr's cleanup, after real episodes land.
        Treating that show as a trailer would throw away real episode plays in the meantime."""
        keys = plex.placeholder_keys(_section(plex, MediaType.SHOW).key, MediaType.SHOW)
        assert keys == {PLACEHOLDER_SHOW}

    def test_a_labelled_show_with_one_real_episode_is_not_a_placeholder(self, plex: PlexClient, state: FakePlexState):
        """One real episode and no trailer reads `leafCount=1` exactly like a trailer-only show would — the
        seasons tell them apart (the label outlives the trailer if Agregarr's cleanup is part-done)."""
        _show(state, 6005, 99_005, seasons={1: 1})
        assert 6005 not in plex.placeholder_keys(_section(plex, MediaType.SHOW).key, MediaType.SHOW)

    def test_one_read_per_library_however_many_people_ask_at_once(self, plex: PlexClient, state: FakePlexState):
        """A sync reads every person on a thread pool, and each asks for the same library."""
        from concurrent.futures import ThreadPoolExecutor

        key = _section(plex, MediaType.MOVIE).key
        calls: list[str] = []
        real = plex._read_placeholder_keys

        def counted(section_key, media_type):
            calls.append(section_key)
            return real(section_key, media_type)

        plex._read_placeholder_keys = counted
        with ThreadPoolExecutor(8) as pool:
            answers = set(pool.map(lambda _: plex.placeholder_keys(key, MediaType.MOVIE), range(8)))
        assert calls == [str(key)] and answers == {frozenset({TRAILER, PLACEHOLDER_FILM})}

    def test_an_ignored_label_filter_reads_as_unknown_never_as_every_item(self, plex: PlexClient, state: FakePlexState):
        """An ignored parameter is answered with the whole library. Read as an answer, every film on the
        server would be a trailer: nobody's watch history would count and nothing would ever look watched."""
        state.ignores_label_filter = True
        assert plex.placeholder_keys(_section(plex, MediaType.MOVIE).key, MediaType.MOVIE) is None


class TestWatchedRead:
    def test_playing_the_trailer_is_not_watching_the_film(self, plex: PlexClient, state: FakePlexState):
        _watch(state, TRAILER)
        assert (state.movies[REAL_FILM].tmdb_id, MediaType.MOVIE) not in _watched(plex, state)

    def test_watching_the_real_copy_still_counts_beside_an_unwatched_trailer(
        self, plex: PlexClient, state: FakePlexState
    ):
        _watch(state, REAL_FILM)
        assert (state.movies[REAL_FILM].tmdb_id, MediaType.MOVIE) in _watched(plex, state)

    def test_playing_a_placeholder_only_film_or_show_counts_for_nothing(self, plex: PlexClient, state: FakePlexState):
        _watch(state, PLACEHOLDER_FILM, PLACEHOLDER_SHOW)
        watched = _watched(plex, state)
        assert (99_001, MediaType.MOVIE) not in watched
        assert (99_002, MediaType.SHOW) not in watched

    def test_a_show_whose_real_episodes_have_landed_still_counts(self, plex: PlexClient, state: FakePlexState):
        _watch(state, ARRIVING_SHOW)
        assert (99_003, MediaType.SHOW) in _watched(plex, state)

    def test_the_cache_sync_read_drops_trailer_plays_too(self, plex: PlexClient, state: FakePlexState):
        """`fetch_section` is what the server's watch cache syncs from; `fetch` is the run's own read."""
        _watch(state, TRAILER, REAL_FILM + 1)
        read = _source(plex, state).fetch_section(SARAH, _section(plex, MediaType.MOVIE), MediaType.MOVIE)
        tmdb_ids = {w.tmdb_id for w in read.items}
        assert state.movies[REAL_FILM].tmdb_id not in tmdb_ids
        assert state.movies[REAL_FILM + 1].tmdb_id in tmdb_ids
        assert read.covers_window, "dropping trailer plays must not cost the read its completeness"

    def test_an_unknown_placeholder_answer_leaves_the_read_as_it_was(self, plex: PlexClient, state: FakePlexState):
        state.ignores_label_filter = True
        _watch(state, TRAILER)
        assert (state.movies[REAL_FILM].tmdb_id, MediaType.MOVIE) in _watched(plex, state)


class TestLibraryIndex:
    @pytest.mark.parametrize("trailer_first", [False, True])
    def test_a_film_with_a_real_copy_resolves_to_it_whichever_plex_lists_last(
        self, plex: PlexClient, state: FakePlexState, trailer_first: bool
    ):
        if trailer_first:
            film = state.movies.pop(REAL_FILM)
            state.movies[REAL_FILM] = film  # now listed after the trailer
        index = plex.build_library_index(_section(plex, MediaType.MOVIE))
        assert index[state.movies[REAL_FILM].tmdb_id] == REAL_FILM

    def test_a_placeholder_only_title_stays_pickable(self, plex: PlexClient):
        assert plex.build_library_index(_section(plex, MediaType.MOVIE))[99_001] == PLACEHOLDER_FILM

    def test_the_cache_signature_moves_when_the_placeholders_do(
        self, plex: PlexClient, server: UvicornThread, state: FakePlexState
    ):
        """The run caches the index for a week under this signature, and Agregarr labels an item AFTER
        Plex has scanned it. A signature blind to the label would keep serving the trailer's key."""
        section = _section(plex, MediaType.MOVIE)
        before = plex.section_signature(section)
        state.movies[REAL_FILM + 1].labels = [PLACEHOLDER_LABEL]
        # A fresh client, as the next run has: a client reads the placeholders once in its lifetime.
        after = PlexClient(server.url, state.owner_token).section_signature(section)
        assert before != after
