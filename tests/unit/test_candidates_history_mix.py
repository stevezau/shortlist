"""Older-watch searches and the fair share between the three kinds of search (#152)."""

from types import SimpleNamespace

from shortlist.engine.candidates import GatherStats
from shortlist.engine.clients.search import SearchResult, TitleCandidate
from tests.unit.test_candidates_favourites import TASTE, _Curator, _gather, seed


class _PerQuerySearch:
    """Exa-shaped; the extracted titles depend on which watched title the query is about."""

    name = "exa"
    results_per_query = 10

    def __init__(self, by_title):
        self._by_title = by_title

    def search(self, query, *, num_results=8):
        return [SearchResult(title="An article", url="https://example.com", text="body")]

    def search_detailed(self, query, *, num_results=8):
        for watched, titles in self._by_title.items():
            if watched in query:
                return self.search(query), [TitleCandidate(title=t, year=2020, media="movie") for t in titles]
        return self.search(query), []


def _mix_search():
    return _PerQuerySearch(
        {
            "R1": ["Recent A", "Shared"],
            "F1": ["Fav A", "Shared", "Fav B"],
            "O1": ["Old A", "Fav A"],
            "O2": ["Old B"],
        }
    )


def _run(mock_tmdb, search, curator, stats, *, favourites=1, older=2, recent=None, **kw):
    return _gather(
        mock_tmdb,
        search,
        curator,
        recent or [seed(1, "R1")],
        stats,
        favourite_seeds=[seed(10, "F1")],
        favourite_count=favourites,
        older_seeds=[seed(20, "O1"), seed(21, "O2")],
        older_count=older,
        **kw,
    )


def test_older_searches_follow_the_favourites_and_are_marked_in_the_trace(mock_tmdb):
    stats = GatherStats()
    _run(mock_tmdb, _mix_search(), _Curator(), stats)
    assert [(q["seed"], q["kind"]) for q in stats.trace["web"]["searches"]] == [
        ("R1", "recent"),
        ("F1", "favourite"),
        ("O1", "older"),
        ("O2", "older"),
    ]


def test_older_is_capped_and_never_repeats_a_title_already_searched(mock_tmdb):
    stats = GatherStats()
    _gather(
        mock_tmdb,
        _mix_search(),
        _Curator(),
        [seed(1, "R1")],
        stats,
        favourite_seeds=[seed(10, "F1")],
        favourite_count=1,
        older_seeds=[seed(10, "F1"), seed(1, "R1"), seed(20, "O1"), seed(21, "O2")],
        older_count=1,
    )
    assert [q["seed"] for q in stats.trace["web"]["searches"]] == ["R1", "F1", "O1"]


def test_an_older_title_is_not_suggested_back(mock_tmdb):
    stats = GatherStats()
    _run(mock_tmdb, _PerQuerySearch({"R1": ["O1", "Silo"]}), _Curator(), stats)
    assert [t.title for t in stats.web_found] == ["Silo"]


def test_the_pick_prompt_lists_each_kind_under_its_heading_and_says_how_many_to_take(mock_tmdb):
    curator = _Curator()
    stats = GatherStats()
    _run(mock_tmdb, _mix_search(), curator, stats, taste=TASTE)
    user = curator.user
    recent = user.index("Titles recommended for their recent watches:")
    favourite = user.index("Titles recommended for their long-time favourites:")
    older = user.index("Titles recommended for things they watched further back:")
    assert recent < favourite < older
    # A title any recent search found is "recent", and the first kind to find a title keeps it.
    assert user.index("Shared") < favourite and user.index("Fav A") < older and user.index("Fav B") < older
    assert user.index("Old A") > older and user.count("Fav A") == 1
    # 4 searches, k = _LLM_WEB_K: one favourite, two older, recent takes the rest.
    shares = stats.trace["web"]["shares"]
    assert shares["favourite"] > 0 and shares["older"] > shares["favourite"]
    assert shares["recent"] == sum(shares.values()) - shares["favourite"] - shares["older"]
    assert (
        f"Take roughly {shares['recent']} from the recent list, {shares['favourite']} from the favourites list, "
        f"{shares['older']} from the further-back list, unless a list has fewer that suit them."
    ) in user


def test_a_kind_with_no_titles_gets_no_list_and_its_share_goes_to_recent(mock_tmdb):
    curator = _Curator()
    stats = GatherStats()
    _run(mock_tmdb, _PerQuerySearch({"R1": ["Recent A"], "O1": ["Old A"]}), curator, stats, older=1)
    assert "long-time favourites" not in curator.user
    shares = stats.trace["web"]["shares"]
    assert shares["favourite"] == 0 and shares["older"] > 0
    assert "from the favourites list" not in curator.user


def test_each_kind_keeps_its_share_of_the_pick_cap(mock_tmdb):
    many = [f"Recent {n}" for n in range(400)]
    curator = _Curator()
    _run(mock_tmdb, _PerQuerySearch({"R1": many, "F1": ["Fav A"], "O1": ["Old A"]}), curator, GatherStats(), older=1)
    assert "Fav A" in curator.user and "Old A" in curator.user
    assert curator.user.count("- Recent ") == 100


def test_without_the_new_searches_the_prompt_is_the_single_list_it_always_was(mock_tmdb):
    curator = _Curator()
    stats = GatherStats()
    _gather(mock_tmdb, _PerQuerySearch({"R1": ["Silo"]}), curator, [seed(1, "R1")], stats)
    assert "Titles recommended by recent articles:\n- Silo (2020) [movie]" in curator.user
    assert "Take roughly" not in curator.user and "shares" not in stats.trace["web"]


def test_the_native_path_ignores_older_seeds(mock_tmdb):
    from shortlist.engine.candidates import gather_candidates
    from tests.unit.test_candidates_favourites import _NativeCurator, _tmdb

    curator = _NativeCurator()
    gather_candidates(
        _tmdb(mock_tmdb),
        [seed(1, "R1")],
        sources=["llm_web"],
        curator=curator,
        profile=SimpleNamespace(history=[]),
        web_search_mode="native",
        older_seeds=[seed(20, "O1")],
        older_count=1,
    )
    assert "older_seeds" not in curator.kwargs
