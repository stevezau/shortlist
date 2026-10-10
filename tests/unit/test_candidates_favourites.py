"""Favourite-title searches and the taste text through the llm_web source (#152)."""

from types import SimpleNamespace

from shortlist.engine.candidates import GatherStats, gather_candidates
from shortlist.engine.clients.search import SearchResult, TitleCandidate
from shortlist.engine.models import MediaType, Seed
from shortlist.engine.taste import TastePrompt

TASTE = TastePrompt("What this person has watched. TASTE-MARKER", True)
RECENT = TastePrompt("Recently watched (most recent first):\n- Pinned (2020)", False)


def seed(tmdb_id: int, title: str, media: MediaType = MediaType.MOVIE) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=title, media_type=media, weight=1.0)


class _Cache:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class _Search:
    """Exa-shaped: every query returns the same extracted titles, and the queries are recorded."""

    name = "exa"
    results_per_query = 10

    def __init__(self, titles):
        self._titles = titles
        self.queries: list[str] = []

    def search(self, query, *, num_results=8):
        self.queries.append(query)
        return [SearchResult(title="An article", url="https://example.com", text="body")]

    def search_detailed(self, query, *, num_results=8):
        return self.search(query, num_results=num_results), list(self._titles)


class _Curator:
    supports_native_web_search = False

    def __init__(self, reply="[]"):
        self.reply = reply
        self.user = ""
        self.system = ""

    def complete(self, system, user):
        self.system, self.user = system, user
        return self.reply


class _NativeCurator:
    supports_native_web_search = True

    def __init__(self):
        self.kwargs: dict = {}

    def recommend_web(self, profile, seeds, k, **kwargs):
        self.kwargs = kwargs
        return []


def _tmdb(mock_tmdb):
    mock_tmdb.suggestions.side_effect = lambda tid, mt: []
    mock_tmdb.genre_names.return_value = {}
    mock_tmdb.search.side_effect = lambda title, mt, year=None: None
    return mock_tmdb


def _gather(mock_tmdb, search, curator, seeds, stats, *, cache=None, **kwargs):
    return gather_candidates(
        _tmdb(mock_tmdb),
        seeds,
        sources=["llm_web"],
        curator=curator,
        profile=SimpleNamespace(history=[]),
        search=search,
        web_search_mode="exa",
        web_search_cache=cache or _Cache(),
        stats=stats,
        **kwargs,
    )


def _titles(*names):
    return [TitleCandidate(title=n, year=2023, media="movie") for n in names]


def test_favourites_are_searched_after_the_recent_seeds_and_marked_in_the_trace(mock_tmdb):
    search = _Search(_titles("Silo"))
    stats = GatherStats()
    recent = [seed(1, "R1"), seed(2, "R2"), seed(3, "R3")]
    favourites = [seed(10, "F1"), seed(11, "F2")]
    _gather(mock_tmdb, search, _Curator(), recent, stats, recent_count=2, favourite_seeds=favourites, favourite_count=2)
    queries = stats.trace["web"]["searches"]
    assert [(q["seed"], q["kind"]) for q in queries] == [
        ("R1", "recent"),
        ("R2", "recent"),
        ("F1", "favourite"),
        ("F2", "favourite"),
    ]


def test_favourites_are_capped_and_a_recent_seed_is_not_searched_twice(mock_tmdb):
    search = _Search(_titles("Silo"))
    stats = GatherStats()
    recent = [seed(1, "R1"), seed(2, "R2")]
    favourites = [seed(2, "R2"), seed(10, "F1"), seed(11, "F2"), seed(12, "F3")]
    _gather(mock_tmdb, search, _Curator(), recent, stats, recent_count=2, favourite_seeds=favourites, favourite_count=2)
    assert [q["seed"] for q in stats.trace["web"]["searches"]] == ["R1", "R2", "F1", "F2"]


def test_without_favourites_every_search_is_recent(mock_tmdb):
    stats = GatherStats()
    _gather(mock_tmdb, _Search(_titles("Silo")), _Curator(), [seed(1, "R1")], stats)
    assert [q["kind"] for q in stats.trace["web"]["searches"]] == ["recent"]


def test_a_favourite_search_shares_the_cache_with_a_recent_one(mock_tmdb):
    cache = _Cache()
    search = _Search(_titles("Silo"))
    _gather(mock_tmdb, search, _Curator(), [seed(1, "R1")], GatherStats(), cache=cache)
    stats = GatherStats()
    _gather(
        mock_tmdb,
        search,
        _Curator(),
        [seed(5, "Other")],
        stats,
        cache=cache,
        favourite_seeds=[seed(1, "R1")],
        favourite_count=1,
    )
    assert stats.exa_cache_hits == 1


def test_a_favourite_title_is_not_suggested_back(mock_tmdb):
    search = _Search(_titles("F1", "Silo"))
    stats = GatherStats()
    _gather(mock_tmdb, search, _Curator(), [seed(1, "R1")], stats, favourite_seeds=[seed(10, "F1")], favourite_count=1)
    assert [t.title for t in stats.web_found] == ["Silo"]


def test_web_found_is_the_full_deduped_extraction(mock_tmdb):
    search = _Search(_titles("Silo", "Silo", "Counterpart"))
    stats = GatherStats()
    _gather(mock_tmdb, search, _Curator(), [seed(1, "R1"), seed(2, "R2")], stats)
    assert [t.title for t in stats.web_found] == ["Silo", "Counterpart"]


def test_the_taste_text_reaches_the_pick_prompt(mock_tmdb):
    curator = _Curator()
    _gather(mock_tmdb, _Search(_titles("Silo")), curator, [seed(1, "R1")], GatherStats(), taste=TASTE)
    assert curator.user.startswith(TASTE.text)
    assert "viewing history" in curator.system


def test_no_taste_leaves_the_pick_prompt_as_it_was(mock_tmdb):
    curator = _Curator()
    _gather(mock_tmdb, _Search(_titles("Silo")), curator, [seed(1, "R1")], GatherStats())
    assert curator.user.startswith("Recently watched (most recent first):")


def test_the_native_path_gets_the_taste_and_ignores_favourites(mock_tmdb):
    curator = _NativeCurator()
    gather_candidates(
        _tmdb(mock_tmdb),
        [seed(1, "R1")],
        sources=["llm_web"],
        curator=curator,
        profile=SimpleNamespace(history=[]),
        web_search_mode="native",
        taste=TASTE,
        favourite_seeds=[seed(10, "F1")],
        favourite_count=1,
    )
    assert curator.kwargs["taste"] == TASTE


def test_the_native_path_passes_no_taste_kwarg_when_there_is_none(mock_tmdb):
    curator = _NativeCurator()
    gather_candidates(
        _tmdb(mock_tmdb),
        [seed(1, "R1")],
        sources=["llm_web"],
        curator=curator,
        profile=SimpleNamespace(history=[]),
        web_search_mode="native",
    )
    assert "taste" not in curator.kwargs


def test_a_recent_taste_reaches_the_pick_prompt_without_the_wide_guide(mock_tmdb):
    curator = _Curator()
    _gather(mock_tmdb, _Search(_titles("Silo")), curator, [seed(1, "R1")], GatherStats(), taste=RECENT)
    assert curator.user.startswith(RECENT.text)
    assert "viewing history" not in curator.system


def test_the_native_path_is_not_handed_a_recent_taste(mock_tmdb):
    curator = _NativeCurator()
    gather_candidates(
        _tmdb(mock_tmdb),
        [seed(1, "R1")],
        sources=["llm_web"],
        curator=curator,
        profile=SimpleNamespace(history=[]),
        web_search_mode="native",
        taste=RECENT,
    )
    assert "taste" not in curator.kwargs
