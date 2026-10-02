"""`/api/seasons`: the owner's own seasons for Seasonal rows (issue #137) — list, presets, CRUD, preview and
the editor's searches. TMDB and Plex are faked at the `run_service` boundary; nothing touches the network."""

from __future__ import annotations

import json
import time
from datetime import date
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.clients.plex_pms import LibraryCollection, LibraryTitle
from shortlist.engine.models import MediaType
from shortlist.server.db.adapters import DbCache
from shortlist.server.db.models import CacheRow
from shortlist.server.services import library_index as library_index_mod


@pytest.fixture(autouse=True)
def _forget_scanned_libraries():
    """The API's library scans are memoised in the process; one test's library must not answer another's."""
    library_index_mod.forget()
    yield
    library_index_mod.forget()


def _body(**overrides) -> dict:
    body = {
        "name": "Thanksgiving",
        "emoji": "🦃",
        "rule": {"kind": "nth", "month": 11, "nth": 4, "weekday": 3},
        "lead_days": 14,
        "tags": [{"id": 4543, "name": "thanksgiving"}],
    }
    body.update(overrides)
    return body


def _create(client: TestClient, **overrides) -> dict:
    r = client.post("/api/seasons", json=_body(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


def _row(client: TestClient, name: str, seasons: list[str]) -> dict:
    r = client.post("/api/collections", json={"name": name, "seasons": seasons})
    assert r.status_code == 201, r.text
    return r.json()


def _rows(client: TestClient) -> dict[int, list[str]]:
    return {c["id"]: c["seasons"] for c in client.get("/api/collections").json()}


class _Tmdb:
    """Discover lists keyed by (media type, the filter value), details by id, and a tag search answer."""

    def __init__(self, lists: dict[tuple[MediaType, str], list[dict]] | None = None, tags: list[dict] | None = None):
        self.lists = lists or {}
        self.tags = tags or []
        self.workers: list[int] = []
        self.searched: list[tuple[str, int]] = []

    def discover_all(self, media_type: MediaType, params: dict, *, workers: int = 4) -> list[dict]:
        self.workers.append(workers)
        return self.lists.get((media_type, params.get("with_keywords") or params.get("with_genres")), [])

    def list_item(self, tmdb_id: int, media_type: MediaType) -> dict | None:
        return None

    def search_keywords(self, query: str, limit: int = 10) -> list[dict]:
        self.searched.append((query, limit))
        return self.tags


class _Plex:
    """One movie library holding ``held``, with ``collections`` (no members) and ``titles`` for search."""

    SECTION = SimpleNamespace(key="1", type="movie", title="Movies")

    def __init__(
        self,
        held: dict[int, int] | None = None,
        collections: list[LibraryCollection] | None = None,
        titles: list[LibraryTitle] | None = None,
        signature: str | None = "2:1700000000",
    ):
        self.held = held or {}
        self.collections = collections or []
        self.titles = titles or []
        self.signature = signature
        self.scans = 0
        self.searched: list[tuple[str, int]] = []

    def sections(self) -> list:
        return [self.SECTION]

    def section_signature(self, section) -> str | None:
        return self.signature

    def build_library_index(self, section) -> dict[int, int]:
        self.scans += 1
        return dict(self.held)

    def list_collections(self) -> list[LibraryCollection]:
        return list(self.collections)

    def collection_members(self, section_key: str, title: str) -> list[LibraryTitle] | None:
        return None

    def search_titles(self, query: str, limit: int = 10) -> list[LibraryTitle]:
        self.searched.append((query, limit))
        return list(self.titles)


def _connect(monkeypatch, client: TestClient, tmdb: _Tmdb | None, plex: _Plex | None) -> None:
    service = client.app.state.run_service
    monkeypatch.setattr(service, "build_tmdb_only", lambda: tmdb)
    monkeypatch.setattr(service, "build_plex_reader", lambda: plex)


class TestList:
    def test_a_created_season_is_listed_with_its_date_and_no_rows(self, client: TestClient):
        created = _create(client)

        listed = {s["slug"]: s for s in client.get("/api/seasons").json()}

        assert created["slug"] == "thanksgiving"
        season = listed["thanksgiving"]
        assert season["builtin"] is False
        assert season["rule_label"] == "4th Thursday of November"
        assert season["used_by"] == []
        assert (season["lead_days"], season["after_days"]) == (14, 0)
        assert season["tags"] == [{"id": 4543, "name": "thanksgiving"}]
        first, second = (date.fromisoformat(d) for d in season["next_dates"])
        assert first < second and first.year + 1 == second.year
        assert all(d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28 for d in (first, second))

    def test_every_season_is_listed_in_calendar_order_built_ins_without_sources(self, client: TestClient):
        """Moved from `GET /api/collections/seasons`, which this router replaces."""
        _create(client, name="St Patrick's Day", emoji="☘️", rule={"kind": "fixed", "month": 3, "day": 17})

        listed = client.get("/api/seasons").json()

        assert [s["slug"] for s in listed] == ["valentines", "st-patricks-day", "halloween", "christmas"]
        halloween = listed[2]
        assert halloween["description"] == "Halloween films and horror"
        assert halloween["builtin"] is True
        assert halloween["rule"] == {"kind": "fixed", "month": 10, "day": 31, "nth": 1, "weekday": 0, "offset": 0}
        assert halloween["rule_label"] == "31 October"
        # Built-ins follow the row's timing, and their sources live in code, not the editor.
        assert (halloween["lead_days"], halloween["after_days"]) == (None, None)
        assert (halloween["tags"], halloween["collections"], halloween["picks"], halloween["genre"]) == (
            [],
            [],
            [],
            None,
        )

    def test_used_by_names_the_rows_that_follow_the_season(self, client: TestClient):
        row = _row(client, "Spooky", ["halloween"])
        halloween = next(s for s in client.get("/api/seasons").json() if s["slug"] == "halloween")
        assert halloween["used_by"] == [{"id": row["id"], "name": "Spooky"}]

    def test_the_old_seasons_endpoint_is_gone(self, client: TestClient):
        assert client.get("/api/collections/seasons").status_code in (404, 405)


class TestValidation:
    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            (
                {"rule": {"kind": "fixed", "month": 2, "day": 29}},
                "29 February isn't every year — pick 28 February or 1 March.",
            ),
            ({"name": "christmas"}, "There's already a season called “Christmas”."),
            ({"name": "CHRISTMAS"}, "There's already a season called “Christmas”."),
            ({"tags": []}, "Add at least one tag, collection or film."),
        ],
    )
    def test_post_refuses_with_the_message_the_editor_shows(self, client: TestClient, overrides, message):
        r = client.post("/api/seasons", json=_body(**overrides))
        assert r.status_code == 422
        assert r.json()["detail"] == message
        assert [s["slug"] for s in client.get("/api/seasons").json()] == ["valentines", "halloween", "christmas"]

    def test_a_name_is_unique_against_custom_seasons_too(self, client: TestClient):
        _create(client)
        r = client.post("/api/seasons", json=_body(name="thanksGIVING"))
        assert (r.status_code, r.json()["detail"]) == (422, "There's already a season called “Thanksgiving”.")

    def test_a_genre_alone_is_a_source(self, client: TestClient):
        assert client.post("/api/seasons", json=_body(tags=[], genre=10751)).status_code == 201

    def test_an_unknown_field_is_refused_rather_than_ignored(self, client: TestClient):
        assert client.post("/api/seasons", json=_body(colour="orange")).status_code == 422

    def test_an_unknown_preset_is_refused(self, client: TestClient):
        assert client.post("/api/seasons", json=_body(preset="arbor_day")).status_code == 422

    def test_a_season_named_like_a_built_in_slug_gets_a_slug_of_its_own(self, client: TestClient):
        """ "Hallowe'en" is a different name from "Halloween", but makes the same slug — which a row would read
        as the built-in."""
        assert _create(client, name="Hallowe'en")["slug"] == "halloween-2"


class TestUpdate:
    def test_put_renames_the_season_and_keeps_its_slug(self, client: TestClient):
        _create(client, preset="thanksgiving_us")

        r = client.put("/api/seasons/thanksgiving", json=_body(name="Turkey Day", emoji="🍗", lead_days=3))

        assert r.status_code == 200, r.text
        assert (r.json()["slug"], r.json()["name"], r.json()["emoji"], r.json()["lead_days"]) == (
            "thanksgiving",
            "Turkey Day",
            "🍗",
            3,
        )
        assert r.json()["preset"] == "thanksgiving_us", "where it came from is not the editor's to change"
        assert [s["name"] for s in client.get("/api/seasons").json() if s["slug"] == "thanksgiving"] == ["Turkey Day"]

    def test_put_may_keep_its_own_name_in_another_case(self, client: TestClient):
        _create(client)
        assert client.put("/api/seasons/thanksgiving", json=_body(name="THANKSGIVING")).status_code == 200

    def test_put_runs_the_same_checks_as_post(self, client: TestClient):
        _create(client)
        r = client.put("/api/seasons/thanksgiving", json=_body(name="Valentine's Day"))
        assert (r.status_code, r.json()["detail"]) == (422, "There's already a season called “Valentine's Day”.")

    def test_a_built_in_cannot_be_edited_or_deleted(self, client: TestClient):
        r = client.put("/api/seasons/halloween", json=_body(name="Spooky"))
        assert (r.status_code, r.json()["detail"]) == (403, "Built-in seasons can't be edited.")
        assert client.delete("/api/seasons/halloween").status_code == 403

    def test_an_unknown_season_is_404(self, client: TestClient):
        assert client.put("/api/seasons/arbor-day", json=_body()).status_code == 404
        assert client.delete("/api/seasons/arbor-day").status_code == 404

    def test_moving_the_date_of_a_season_a_row_follows_applies_it_now(self, client: TestClient, monkeypatch):
        """Which days a row is shown on has just changed, as it would had the row's own seasons changed."""
        from shortlist.server.services import jobs as jobs_mod

        _create(client)
        row = _row(client, "Turkey", ["thanksgiving"])
        queued: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            jobs_mod, "enqueue", lambda sessions, kind, payload=None, **kw: queued.append((kind, payload))
        )

        client.put("/api/seasons/thanksgiving", json=_body(name="Turkey Day"))
        assert queued == [], "a rename changes no day the row is shown on"

        client.put("/api/seasons/thanksgiving", json=_body(lead_days=3))
        assert queued == [("rows.visibility", {"row": row["slug"]})]


class TestDelete:
    def test_a_season_that_is_a_rows_only_season_is_not_deleted(self, client: TestClient, monkeypatch):
        from shortlist.server.services import jobs as jobs_mod

        _create(client)
        a = _row(client, "Row A", ["halloween", "thanksgiving"])
        b = _row(client, "Row B", ["thanksgiving"])
        before = _rows(client)

        r = client.delete("/api/seasons/thanksgiving")

        assert r.status_code == 409
        assert r.json()["detail"] == (
            "“Thanksgiving” is the only season in “Row B”. Give those rows another season, or delete them, first."
        )
        assert _rows(client) == before
        assert "thanksgiving" in [s["slug"] for s in client.get("/api/seasons").json()]

        assert client.patch(
            f"/api/collections/{b['id']}", json={"name": "Row B", "seasons": ["christmas", "thanksgiving"]}
        ).is_success
        queued: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            jobs_mod, "enqueue", lambda sessions, kind, payload=None, **kw: queued.append((kind, payload))
        )

        assert client.delete("/api/seasons/thanksgiving").status_code == 204

        after = _rows(client)
        assert (after[a["id"]], after[b["id"]]) == (["halloween"], ["christmas"])
        assert "thanksgiving" not in [s["slug"] for s in client.get("/api/seasons").json()]
        # Those rows no longer follow it, so whether they are on Home today is re-applied now.
        assert sorted(payload["row"] for _kind, payload in queued) == sorted([a["slug"], b["slug"]])
        assert {kind for kind, _payload in queued} == {"rows.visibility"}

    def test_every_row_it_is_the_only_season_of_is_named(self, client: TestClient):
        _create(client)
        _row(client, "Row B", ["thanksgiving"])
        _row(client, "Row C", ["thanksgiving"])
        detail = client.delete("/api/seasons/thanksgiving").json()["detail"]
        assert detail.startswith("“Thanksgiving” is the only season in “Row B” and “Row C”.")


class TestRowsFollowCustomSeasons:
    def test_a_row_may_follow_a_custom_season_but_not_an_unknown_one(self, client: TestClient):
        _create(client)
        row = _row(client, "Turkey", ["halloween"])

        ok = client.patch(
            f"/api/collections/{row['id']}", json={"name": "Turkey", "seasons": ["thanksgiving", "halloween"]}
        )
        refused = client.patch(f"/api/collections/{row['id']}", json={"name": "Turkey", "seasons": ["arbor-day"]})

        assert ok.status_code == 200, ok.text
        assert ok.json()["seasons"] == ["halloween", "thanksgiving"]
        assert refused.status_code == 422
        assert _rows(client)[row["id"]] == ["halloween", "thanksgiving"]


class TestPresets:
    def test_a_preset_already_added_is_no_longer_offered(self, client: TestClient):
        offered = {p["key"]: p for p in client.get("/api/seasons/presets").json()}
        assert {"thanksgiving_us", "thanksgiving_ca"} <= set(offered)
        thanksgiving = offered["thanksgiving_us"]
        assert thanksgiving["tags"] == [{"id": 4543, "name": "thanksgiving"}]
        assert (thanksgiving["preset"], thanksgiving["lead_days"]) == ("thanksgiving_us", 14)
        assert thanksgiving["rule"] == {"kind": "nth", "month": 11, "day": 1, "nth": 4, "weekday": 3, "offset": 0}

        _create(client, preset="thanksgiving_us")

        keys = [p["key"] for p in client.get("/api/seasons/presets").json()]
        assert "thanksgiving_us" not in keys
        assert "thanksgiving_ca" in keys

    def test_a_preset_posts_back_as_a_season(self, client: TestClient):
        """What the editor does with one: open it pre-filled, then save it."""
        preset = next(p for p in client.get("/api/seasons/presets").json() if p["key"] == "st_patricks_day")
        body = {k: v for k, v in preset.items() if k not in ("key", "note")}
        r = client.post("/api/seasons", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["excluded_genres"] == [27]


class TestPreview:
    TAG: ClassVar[dict] = {"id": 4543, "name": "thanksgiving"}
    LISTS: ClassVar[dict] = {
        (MediaType.MOVIE, "4543"): [
            {"id": 1, "title": "Planes, Trains and Automobiles", "vote_count": 1500, "genre_ids": []},
            {"id": 2, "title": "Pieces of April", "vote_count": 90, "genre_ids": []},
            {"id": 3, "title": "Not On This Server", "vote_count": 9000, "genre_ids": []},
        ]
    }

    def _preview(self, client: TestClient, **body) -> dict:
        r = client.post(
            "/api/seasons/preview", json={"rule": {"kind": "nth", "month": 11, "nth": 4, "weekday": 3}, **body}
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_counts_only_what_the_libraries_hold_most_voted_first(self, client: TestClient, monkeypatch):
        tmdb = _Tmdb(self.LISTS)
        _connect(monkeypatch, client, tmdb, _Plex(held={1: 101, 2: 102}))

        result = self._preview(
            client,
            tags=[self.TAG],
            collections=[{"section_key": "1", "section_title": "Movies", "title": "Thanksgiving Movies"}],
        )

        assert result["total"] == 2
        assert result["per_tag"] == {"4543": 2}
        assert result["sample"] == ["Planes, Trains and Automobiles", "Pieces of April"]
        assert (result["from_tags"], result["from_genre"], result["from_collections"], result["from_picks"]) == (
            2,
            0,
            0,
            0,
        )
        assert result["per_collection"] == [
            {"title": "Thanksgiving Movies", "section_key": "1", "found": False, "in_library": 0}
        ]
        assert date.fromisoformat(result["next_date"]).month == 11 and result["rule_error"] is None
        assert tmdb.workers and set(tmdb.workers) == {6}, "the editor reads a list's pages six at a time"

    def test_an_invalid_rule_still_counts(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, _Tmdb(self.LISTS), _Plex(held={1: 101}))
        result = self._preview(client, rule={"kind": "nth", "month": 11, "nth": 9, "weekday": 3}, tags=[self.TAG])
        assert (result["next_date"], result["total"]) == (None, 1)
        assert result["rule_error"] == "Pick the 1st to 4th, or last, weekday of the month."

    def test_without_a_tmdb_key_it_says_what_to_do(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, None, _Plex())
        r = client.post("/api/seasons/preview", json={"rule": {"kind": "easter"}, "tags": [self.TAG]})
        assert (r.status_code, r.json()["detail"]) == (503, "Add a TMDB API key in Settings first.")

    def test_a_tmdb_failure_is_a_502_without_the_key(self, client: TestClient, monkeypatch):
        class _Down(_Tmdb):
            def discover_all(self, media_type, params, *, workers=4):
                raise RuntimeError("GET https://api.themoviedb.org/3/discover/movie?api_key=SECRETKEY failed")

        _connect(monkeypatch, client, _Down(), _Plex())
        r = client.post("/api/seasons/preview", json={"rule": {"kind": "easter"}, "tags": [self.TAG]})
        assert r.status_code == 502
        assert "SECRETKEY" not in r.text and "RuntimeError" in r.json()["detail"]

    def test_a_plex_failure_is_a_502(self, client: TestClient, monkeypatch):
        def unreachable():
            raise ConnectionError("pms:32400 refused X-Plex-Token=PLEXSECRET")

        _connect(monkeypatch, client, _Tmdb(), None)
        monkeypatch.setattr(client.app.state.run_service, "build_plex_reader", unreachable)
        r = client.post("/api/seasons/preview", json={"rule": {"kind": "easter"}, "tags": [self.TAG]})
        assert r.status_code == 502 and "PLEXSECRET" not in r.text


class TestSearches:
    def test_a_query_under_two_characters_returns_nothing_and_asks_nobody(self, client: TestClient, monkeypatch):
        tmdb, plex = _Tmdb(tags=[{"id": 1, "name": "t", "movies": 1}]), _Plex()
        _connect(monkeypatch, client, tmdb, plex)

        assert client.get("/api/seasons/tmdb-tags?q=t").json() == []
        assert client.get("/api/seasons/library-search?q=a").json() == []
        assert client.get("/api/seasons/plex-collections?q=%20x%20").json() == []
        assert (tmdb.searched, plex.searched) == ([], [])

    def test_tmdb_tags_are_tmdbs_answer(self, client: TestClient, monkeypatch):
        tmdb = _Tmdb(tags=[{"id": 4543, "name": "thanksgiving", "movies": 120}])
        _connect(monkeypatch, client, tmdb, None)

        assert client.get("/api/seasons/tmdb-tags?q=%20thanks%20").json() == [
            {"id": 4543, "name": "thanksgiving", "movies": 120}
        ]
        assert tmdb.searched == [("thanks", 10)]

    def test_tmdb_tags_without_a_key_say_what_to_do(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, None, None)
        r = client.get("/api/seasons/tmdb-tags?q=thanks")
        assert (r.status_code, r.json()["detail"]) == (503, "Add a TMDB API key in Settings first.")

    def test_plex_collections_match_any_part_of_the_title_in_any_case_sorted(self, client: TestClient, monkeypatch):
        def coll(title: str, count: int = 3) -> LibraryCollection:
            return LibraryCollection("1", "Movies", title, count, False, MediaType.MOVIE)

        plex = _Plex(collections=[coll("Thanksgiving Movies", 12), coll("Christmas"), coll("A THANKSGIVING Feast")])
        _connect(monkeypatch, client, None, plex)

        assert client.get("/api/seasons/plex-collections?q=thanks").json() == [
            {
                "section_key": "1",
                "section_title": "Movies",
                "title": "A THANKSGIVING Feast",
                "count": 3,
                "smart": False,
            },
            {
                "section_key": "1",
                "section_title": "Movies",
                "title": "Thanksgiving Movies",
                "count": 12,
                "smart": False,
            },
        ]

    def test_library_search_asks_plex_for_ten(self, client: TestClient, monkeypatch):
        plex = _Plex(titles=[LibraryTitle(2, MediaType.MOVIE, "Pieces of April", 2003)])
        _connect(monkeypatch, client, None, plex)

        assert client.get("/api/seasons/library-search?q=april").json() == [
            {"tmdb_id": 2, "media_type": "movie", "title": "Pieces of April", "year": 2003}
        ]
        assert plex.searched == [("april", 10)]

    def test_without_plex_it_says_what_to_do(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, None, None)
        assert client.get("/api/seasons/library-search?q=april").status_code == 503


class TestLibraryIndex:
    """What the editor counts against: the engine's cached index when it is current, else one scan held for
    ten minutes (#137 — a 10,000-film library takes 7.5s to scan)."""

    def test_the_engines_cached_index_is_read_without_a_scan(self, client: TestClient):
        sessions = client.app.state.sessions
        DbCache(sessions, kind="library_index").set(
            "index3:1:2:1700000000", json.dumps({"index": {"7": 107}, "genres": {}, "tallied": False}), 3600
        )
        plex = _Plex(held={1: 101})

        index = library_index_mod.library_index(plex, sessions)

        assert index == {MediaType.MOVIE: {7: 107}, MediaType.SHOW: {}}
        assert plex.scans == 0

    def test_a_scan_is_held_for_ten_minutes_and_never_written_to_the_engines_cache(
        self, client: TestClient, monkeypatch
    ):
        sessions = client.app.state.sessions
        plex = _Plex(held={1: 101})
        clock = [time.monotonic()]
        monkeypatch.setattr(library_index_mod.time, "monotonic", lambda: clock[0])

        assert library_index_mod.library_index(plex, sessions) == {MediaType.MOVIE: {1: 101}, MediaType.SHOW: {}}
        clock[0] += 599
        library_index_mod.library_index(plex, sessions)
        assert plex.scans == 1

        clock[0] += 2
        library_index_mod.library_index(plex, sessions)
        assert plex.scans == 2

        with sessions() as session:
            assert session.query(CacheRow).filter_by(kind="library_index").count() == 0

    def test_a_changed_library_is_scanned_again(self, client: TestClient):
        sessions = client.app.state.sessions
        plex = _Plex(held={1: 101})
        library_index_mod.library_index(plex, sessions)
        plex.signature, plex.held = "3:1700000500", {1: 101, 2: 102}

        assert library_index_mod.library_index(plex, sessions)[MediaType.MOVIE] == {1: 101, 2: 102}
        assert plex.scans == 2
