"""Trace capture: the per-user pipeline trace (``GatherStats.trace`` / ``UserRunReport.trace``) that
feeds the run-detail "View trace" dialog. These assert what each stage RECORDS, not just that it ran
— the trace is the operator's only window into "what did the AI search, and what did it propose?"."""

from datetime import UTC, datetime
from types import SimpleNamespace

from shortlist.engine import rows as rows_mod
from shortlist.engine.candidates import GatherStats, gather_candidates
from shortlist.engine.clients.search import SearchResult
from shortlist.engine.models import Candidate, MediaType, RowSpec, Seed, UserRunReport, WatchedItem


def make_result(title: str, text: str = "") -> SearchResult:
    return SearchResult(title=title, url="https://example.com", text=text)


def seed(tmdb_id: int, title: str = "Seed", media: MediaType = MediaType.MOVIE, weight: float = 1.0) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=title, media_type=media, weight=weight)


def _row_spec(slug: str = "picked") -> RowSpec:
    return RowSpec(slug=slug, name_template="Picked for {user}", size=10, media="movie")


def _report() -> UserRunReport:
    return UserRunReport(username="sam", slug="sam")


def _ranked(items: list[dict]) -> list[tuple[dict, float]]:
    return [(item, 1.0) for item in items]


class _FakeSearch:
    name = "exa"

    def __init__(self, results):
        self._results = results
        self.queries: list[str] = []

    def search(self, query, *, num_results=8):
        self.queries.append(query)
        return self._results


class _NonNativeCurator:
    """Ollama-shaped: no native web search, `complete` proposes from the Exa results."""

    supports_native_web_search = False

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply


class TestGatherTraceSources:
    """The per-source summary: every source attempted, whether it worked, how many it contributed."""

    def test_records_ok_and_failed_sources_with_contribution_counts(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 10, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )

        class _Boom:
            def related(self, tmdb_id, media_type):
                raise RuntimeError("trakt 503")

        stats = GatherStats()
        gather_candidates(
            mock_tmdb, [seed(1, "Arrival")], sources=["tmdb_similar", "trakt"], trakt=_Boom(), stats=stats
        )
        by_source = {s["source"]: s for s in stats.trace["sources"]}
        assert by_source["tmdb_similar"]["status"] == "ok"
        assert by_source["tmdb_similar"]["contributed"] == 1
        assert by_source["trakt"]["status"] == "failed"
        assert by_source["trakt"]["contributed"] == 0
        assert "trakt 503" in by_source["trakt"]["detail"]  # the real reason, for the operator
        # The seeded source records what each seed searched for and what came back, so the operator can
        # follow a TMDB query the way they can an AI web search. Each return carries its tmdb_id so the
        # disposition pass can mark it kept/dropped precisely.
        assert by_source["tmdb_similar"]["queries"][0]["seed"] == "Arrival"
        assert by_source["tmdb_similar"]["queries"][0]["returned"] == [{"tmdb_id": 10, "title": "S"}]

    def test_per_seed_query_sample_is_bounded(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 10, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        from shortlist.engine.candidates import _TRACE_SEEDS_SAMPLE

        many = [seed(i, f"Seed {i}") for i in range(_TRACE_SEEDS_SAMPLE + 10)]
        stats = GatherStats()
        gather_candidates(mock_tmdb, many, sources=["tmdb_similar"], stats=stats)
        queries = {s["source"]: s for s in stats.trace["sources"]}["tmdb_similar"]["queries"]
        assert len(queries) == _TRACE_SEEDS_SAMPLE  # sampled, not one row per seed

    def test_discover_records_the_genres_it_widened_into(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_ids_for.side_effect = lambda tid, mt: [18, 28]
        mock_tmdb.genre_names.return_value = {18: "Drama", 28: "Action"}
        mock_tmdb.discover.side_effect = lambda mt, gids, **kw: []

        stats = GatherStats()
        gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_discover"], stats=stats)
        assert stats.trace["discover_genres"]["movie"] == ["Drama", "Action"]


class TestGatherTraceWeb:
    """The web-search detail: the queries sent, the RAG prompt, and resolved vs. hallucinated titles."""

    def test_records_exa_queries_rag_prompt_and_resolved_split(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        # "Real Film" resolves to a TMDB id; "Made Up" does not (a hallucination -> unresolved).
        resolved = {"Real Film": {"id": 800, "title": "Found", "genre_ids": [], "vote_average": 7.5}}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: resolved.get(title)
        search = _FakeSearch([make_result("Best of 2021", "great films")])
        reply = (
            '[{"title": "Real Film", "year": 2022, "media": "movie"}, '
            '{"title": "Made Up", "year": null, "media": "movie"}]'
        )
        curator = _NonNativeCurator(reply)

        stats = GatherStats()
        gather_candidates(
            mock_tmdb,
            [seed(1, "Arrival")],
            sources=["llm_web"],
            curator=curator,
            profile=SimpleNamespace(history=[]),
            search=search,
            web_search_mode="exa",
            stats=stats,
        )
        web = stats.trace["web"]
        assert web["mode"] == "exa"
        # One search per seed title, with the query and what it returned.
        assert len(web["searches"]) == 1
        assert web["searches"][0]["seed"] == "Arrival"
        assert web["searches"][0]["returned"] == ["Best of 2021"]
        assert web["searches"][0]["cached"] is False
        # The exact prompt the model ranked from is captured for inspection.
        assert web["rag_user"] and web["rag_system"]
        # The resolved/unresolved split makes hallucinations visible in the UI.
        assert any("Real Film" in t for t in web["resolved"])
        assert any("Made Up" in t for t in web["unresolved"])


class TestRecordHistoryTrace:
    """rows._record_history_trace: the history/seeds stage. Display only — bounded, sorted, summarised."""

    def _watch(self, title: str, day: int, media: MediaType = MediaType.MOVIE) -> WatchedItem:
        return WatchedItem(title=title, media_type=media, watched_at=datetime(2026, 7, day, tzinfo=UTC))

    def test_records_recent_watches_seeds_and_counts(self):
        report = _report()
        history = [self._watch("Old", 1), self._watch("Newer", 10), self._watch("Newest", 20)]
        spec = _row_spec()
        seeds = [seed(1, "A", weight=0.4), seed(2, "B", weight=0.9)]

        rows_mod._record_history_trace(
            report,
            history,
            [spec],
            seeds_for=lambda _spec: seeds,
            watched_movies={1, 2, 3},
            watched_shows={7: (4, 10)},
        )

        hist = report.trace["history"]
        assert hist["total"] == 3
        assert hist["watched_movies"] == 3
        assert hist["watched_shows"] == 1
        # Most-recent first.
        assert [w["title"] for w in hist["recent"]] == ["Newest", "Newer", "Old"]
        # Seeds strongest-first, with the weight rounded for display.
        assert [s["title"] for s in report.trace["seeds"]] == ["B", "A"]
        assert report.trace["seeds"][0]["weight"] == 0.9

    def test_counts_true_distinct_watched_titles_per_library(self):
        # Two movie libraries + a heavy-TV recent history: the per-library total must be exact per
        # library (not a shared per-media number), and count DISTINCT titles (a binge counts once).
        report = _report()
        history = [
            WatchedItem(title="Dune", media_type=MediaType.MOVIE, watched_at=datetime(2026, 7, 1, tzinfo=UTC)),
            WatchedItem(title="Akira", media_type=MediaType.SHOW, watched_at=datetime(2026, 7, 2, tzinfo=UTC)),
            # Same show watched twice (a binge) -> one distinct title.
            WatchedItem(title="Akira", media_type=MediaType.SHOW, watched_at=datetime(2026, 7, 3, tzinfo=UTC)),
            WatchedItem(title="Tron", media_type=MediaType.MOVIE, watched_at=datetime(2026, 7, 4, tzinfo=UTC)),
        ]
        library = {"Dune": "Movies", "Akira": "TV Shows", "Tron": "4K Movies"}

        rows_mod._record_history_trace(
            report,
            history,
            [_row_spec()],
            seeds_for=lambda _spec: [],
            watched_movies=set(),
            watched_shows={},
            library_of_watch=lambda item: library[item.title],
        )

        by_lib = report.trace["history"]["watched_by_library"]
        assert by_lib["Movies"] == {"movie": 1, "show": 0}
        assert by_lib["4K Movies"] == {"movie": 1, "show": 0}  # NOT merged with "Movies"
        assert by_lib["TV Shows"] == {"movie": 0, "show": 1}  # binge counts once

    def test_records_the_real_library_name_for_each_watch_and_seed(self):
        # A server with two movie libraries: grouping by media type alone would wrongly merge them,
        # so the trace must carry each item's real library display name.
        report = _report()
        history = [self._watch("Dune", 20), self._watch("Akira", 19)]
        seeds = [seed(1, "Dune"), seed(2, "Akira")]
        libraries = {"Dune": "Movies", "Akira": "4K Movies"}

        rows_mod._record_history_trace(
            report,
            history,
            [_row_spec()],
            seeds_for=lambda _spec: seeds,
            watched_movies=set(),
            watched_shows={},
            library_of_watch=lambda item: libraries[item.title],
            library_of_seed=lambda s: libraries[s.title],
        )
        assert {w["title"]: w["library"] for w in report.trace["history"]["recent"]} == libraries
        assert {s["title"]: s["library"] for s in report.trace["seeds"]} == libraries

    def test_recent_watches_are_capped(self):
        report = _report()
        history = [self._watch(f"W{i}", (i % 27) + 1) for i in range(rows_mod._TRACE_HISTORY_SAMPLE + 15)]
        spec = _row_spec()

        rows_mod._record_history_trace(
            report, history, [spec], seeds_for=lambda _spec: [], watched_movies=set(), watched_shows={}
        )
        assert report.trace["history"]["total"] == len(history)  # full count preserved
        assert len(report.trace["history"]["recent"]) == rows_mod._TRACE_HISTORY_SAMPLE  # sample bounded


class TestRecordGatherTrace:
    """rows._record_gather: folds a pool's GatherStats.trace under report.trace['gathers'] with a label."""

    def test_labels_the_pool_and_appends_its_trace(self):
        report = _report()
        stats = GatherStats()
        stats.trace["sources"] = [{"source": "tmdb_similar", "status": "ok", "contributed": 5, "detail": ""}]

        rows_mod._record_gather(report, stats, pool_label="movie · Movies")
        assert len(report.trace["gathers"]) == 1
        assert report.trace["gathers"][0]["pool"] == "movie · Movies"
        assert report.trace["gathers"][0]["sources"][0]["source"] == "tmdb_similar"

    def test_empty_trace_records_no_gather(self):
        report = _report()
        rows_mod._record_gather(report, GatherStats(), pool_label="movie · Movies")
        assert "gathers" not in report.trace  # nothing to show -> nothing recorded


class TestStampDisposition:
    """rows._stamp_disposition: stamps each recorded return with its FATE and tallies per source.

    Covers every fate cell in one pass (bug shape 8 — the key-derivation branches, especially the
    movie/show media hint and the ranked-vs-in_library relationship, must each be exercised)."""

    def test_stamps_every_fate_including_the_show_media_branch(self):
        def cand(tmdb_id: int, media: MediaType = MediaType.MOVIE) -> Candidate:
            return Candidate(tmdb_id=tmdb_id, title=f"t{tmdb_id}", media_type=media)

        kept_movie = cand(10)
        cutoff_movie = cand(11)  # in library, but lost the pre-rank cut
        kept_show = cand(50, MediaType.SHOW)
        # Delivery-side lists: ranked is the pre-rank survivors, in_library ⊇ ranked.
        ranked = [kept_movie, kept_show]
        in_library = [kept_movie, cutoff_movie, kept_show]
        dropped = [
            (cand(20), "already_watched"),
            (cand(21), "not_in_your_libraries"),
            (cand(22), "excluded_genre"),
        ]

        stats = GatherStats()
        stats.trace["sources"] = [
            {
                "source": "tmdb_similar",
                "status": "ok",
                "contributed": 3,
                "detail": "",
                "queries": [
                    {
                        "seed": "Toy Story",
                        "media": "movie",
                        "total": 6,
                        "returned": [
                            {"tmdb_id": 10, "title": "kept"},
                            {"tmdb_id": 11, "title": "cutoff"},
                            {"tmdb_id": 20, "title": "watched"},
                            {"tmdb_id": 21, "title": "not-in-lib"},
                            {"tmdb_id": 22, "title": "excluded"},
                            {"tmdb_id": 99, "title": "phantom"},  # never pooled -> not_returned
                        ],
                    },
                    {
                        "seed": "Breaking Bad",
                        "media": "show",
                        "total": 1,
                        "returned": [{"tmdb_id": 50, "title": "kept-show"}],
                    },
                ],
            }
        ]

        rows_mod._stamp_disposition(stats, dropped=dropped, in_library=in_library, ranked=ranked)

        movie_q, show_q = stats.trace["sources"][0]["queries"]
        fates = {r["title"]: r["fate"] for r in movie_q["returned"]}
        assert fates == {
            "kept": "kept",
            "cutoff": "lost_ranking_cutoff",
            "watched": "already_watched",
            "not-in-lib": "not_in_your_libraries",
            "excluded": "excluded_genre",
            "phantom": "not_returned",
        }
        # The show-media branch must key on MediaType.SHOW, not fall through to a movie mismatch.
        assert show_q["returned"][0]["fate"] == "kept"
        assert stats.trace["sources"][0]["disposition"] == {
            "kept": 2,
            "lost_ranking_cutoff": 1,
            "already_watched": 1,
            "not_in_your_libraries": 1,
            "excluded_genre": 1,
            "not_returned": 1,
        }
