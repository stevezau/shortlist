"""`/api/seasons`: the owner's own seasons for Seasonal rows (issue #137) — list, presets, CRUD, preview and
the editor's searches. TMDB and Plex are faked at the `run_service` boundary; nothing touches the network."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.clients.plex_pms import LibraryCollection, LibraryTitle
from shortlist.engine.models import MediaType
from shortlist.engine.seasons import BUILTIN_SEASONS, DateRule, Season
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


def _row(client: TestClient, name: str, seasons: list[str], *, enabled: bool = True) -> dict:
    r = client.post("/api/collections", json={"name": name, "seasons": seasons, "enabled": enabled})
    assert r.status_code == 201, r.text
    return r.json()


def _jobs(monkeypatch) -> tuple[list[tuple[str, dict]], list[str]]:
    """Record what a handler queues and every background drain it starts; a drain it WAITS on fails the test."""
    from shortlist.server.services import jobs as jobs_mod

    queued: list[tuple[str, dict]] = []
    drains: list[str] = []

    async def waited_on(state, reason: str) -> None:
        raise AssertionError(f"the response waited on the queue ({reason})")

    monkeypatch.setattr(jobs_mod, "enqueue", lambda sessions, kind, payload=None, **kw: queued.append((kind, payload)))
    monkeypatch.setattr(jobs_mod, "drain_in_background", lambda state, reason: drains.append(reason))
    monkeypatch.setattr(jobs_mod, "drain_now", waited_on)
    return queued, drains


def _default_row_follows(client: TestClient, slug: str) -> None:
    """The default row cannot be given a season through the API; written directly, as an older database may hold."""
    from shortlist.server.db.models import DEFAULT_SLUG, Collection

    with client.app.state.sessions() as session:
        session.query(Collection).filter_by(slug=DEFAULT_SLUG).one().seasons = [slug]
        session.commit()


def _rows(client: TestClient) -> dict[int, list[str]]:
    return {c["id"]: c["seasons"] for c in client.get("/api/collections").json()}


def _on(monkeypatch, day: datetime) -> None:
    """The server's clock reads ``day``: whether an edit changes what a row shows TODAY depends on the date."""
    import shortlist.server.services.context_builder as context_builder

    monkeypatch.setattr(context_builder, "local_now", lambda: day)


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

    def test_used_by_names_each_row_as_it_reads_in_that_season(self, client: TestClient):
        """The Seasonal template's own name is `{season_emoji} {season} picks`: shown as its placeholders it
        tells the owner nothing about which row it is."""
        _create(client)
        row = _row(client, "{season_emoji} {season} picks", ["halloween", "thanksgiving"])
        listed = {s["slug"]: s["used_by"] for s in client.get("/api/seasons").json()}
        assert listed["thanksgiving"] == [{"id": row["id"], "name": "🦃 Thanksgiving picks"}]
        assert listed["halloween"] == [{"id": row["id"], "name": "🎃 Halloween picks"}]

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

    def test_moving_the_date_of_a_season_a_row_follows_applies_it_without_waiting(
        self, client: TestClient, monkeypatch
    ):
        """Which days a row is shown on has just changed, as it would had the row's own seasons changed. Each pass
        is a whole privacy sync, so the saved season is answered first and the passes run behind it. On 15 Nov
        a 14-day lead shows Thanksgiving (26 Nov) and a 3-day one does not."""
        _on(monkeypatch, datetime(2026, 11, 15, 12, 0))
        _create(client)
        row = _row(client, "Turkey", ["thanksgiving"])
        queued, drains = _jobs(monkeypatch)

        client.put("/api/seasons/thanksgiving", json=_body(name="Turkey Day"))
        assert (queued, drains) == ([], []), "a rename changes no day the row is shown on"

        assert client.put("/api/seasons/thanksgiving", json=_body(lead_days=3)).status_code == 200
        assert queued == [("rows.visibility", {"row": row["slug"]})]
        assert drains == ["season 'thanksgiving' moved"]

    def test_a_move_that_changes_nothing_today_queues_no_pass(self, client: TestClient, monkeypatch):
        """In October Thanksgiving is hidden with either lead, so the pass would be a whole privacy sync for
        nothing (#137 ARCH-LOW-2)."""
        _on(monkeypatch, datetime(2026, 10, 2, 12, 0))
        _create(client)
        _row(client, "Turkey", ["thanksgiving"])
        queued, drains = _jobs(monkeypatch)

        assert client.put("/api/seasons/thanksgiving", json=_body(lead_days=3)).status_code == 200

        assert (queued, drains) == ([], [])

    def test_a_date_moved_while_the_row_shows_queues_a_pass(self, client: TestClient, monkeypatch):
        """On 20 Nov Thanksgiving (US) shows on the 26th or a day later. The pass re-reads the ledger's own record
        rather than this edit's guess at it, so a second edit before the next run cannot skip one (#137 round 4);
        the guard keeps the collection shown, since its day is within the window's width of the new one."""
        _on(monkeypatch, datetime(2026, 11, 20, 12, 0))
        _create(client)
        row = _row(client, "Turkey", ["thanksgiving"])
        queued, drains = _jobs(monkeypatch)

        r = client.put("/api/seasons/thanksgiving", json=_body(rule={"kind": "fixed", "month": 11, "day": 27}))

        assert r.status_code == 200, r.text
        assert queued == [("rows.visibility", {"row": row["slug"]})]
        assert drains == ["season 'thanksgiving' moved"]

    def test_a_move_that_hands_the_row_to_another_season_queues_a_pass(self, client: TestClient, monkeypatch):
        """Shown before and after, but for another season: the collection built for the first is hidden now
        (#137 C-1), so the pass is owed even though the row is on Home either way."""
        _on(monkeypatch, datetime(2026, 12, 10, 12, 0))
        _create(client, rule={"kind": "fixed", "month": 12, "day": 31}, lead_days=7)
        row = _row(client, "Holidays", ["christmas", "thanksgiving"])
        queued, _drains = _jobs(monkeypatch)

        client.put("/api/seasons/thanksgiving", json=_body(rule={"kind": "fixed", "month": 12, "day": 12}, lead_days=7))

        assert queued == [("rows.visibility", {"row": row["slug"]})]

    def test_a_disabled_row_is_not_given_a_pass(self, client: TestClient, monkeypatch):
        # The day after Thanksgiving: two days after brings it back.
        _on(monkeypatch, datetime(2026, 11, 27, 12, 0))
        _create(client)
        on = _row(client, "On", ["thanksgiving"])
        _row(client, "Off", ["thanksgiving"], enabled=False)
        queued, _drains = _jobs(monkeypatch)

        client.put("/api/seasons/thanksgiving", json=_body(after_days=2))

        assert queued == [("rows.visibility", {"row": on["slug"]})]

    def test_a_rule_is_stored_without_the_fields_its_kind_ignores(self, client: TestClient, monkeypatch):
        """So editing one of them is no edit: nothing stored changes and no row is re-applied."""
        _create(client, rule={"kind": "nth", "month": 11, "nth": 4, "weekday": 3, "day": 20, "offset": 5})
        _row(client, "Turkey", ["thanksgiving"])
        queued, _drains = _jobs(monkeypatch)

        r = client.put(
            "/api/seasons/thanksgiving",
            json=_body(rule={"kind": "nth", "month": 11, "nth": 4, "weekday": 3, "day": 9, "offset": -2}),
        )

        assert r.json()["rule"] == {"kind": "nth", "month": 11, "day": 1, "nth": 4, "weekday": 3, "offset": 0}
        assert queued == []


class TestWhetherASeasonEditOwesAPass:
    """`_today` and `_pass_owed`, the season-edit gate, called directly. A pass is owed for a row shown before or
    after the edit whenever the day or window of the season it shows changed (#137 round 4). Guessing whether the
    guard's answer changes, from the season's day before the edit, missed a second edit made before the next run:
    the ledger's record is the day the row was BUILT for, which the pass reads and the gate does not."""

    ROW = SimpleNamespace(show_days=[], season_lead_days=30, season_after_days=0)

    @staticmethod
    def _catalogue(month: int, day: int, lead: int, after: int = 0) -> dict:
        moved = Season(
            slug="moved",
            name="Moved",
            emoji="🗓️",
            rule=DateRule("fixed", month=month, day=day),
            description="",
            keywords=(1,),
            lead_days=lead,
            after_days=after,
        )
        return {**BUILTIN_SEASONS, "moved": moved}

    def _owed(self, now: datetime, before: tuple, after: tuple, seasons: tuple[str, ...] = ("moved",)) -> bool:
        from shortlist.server.api import seasons as seasons_api

        answers = [seasons_api._today(self.ROW, list(seasons), now, self._catalogue(*rule)) for rule in (before, after)]
        return seasons_api._pass_owed(*answers)

    @pytest.mark.parametrize(
        ("now", "before", "after"),
        [
            (datetime(2026, 10, 30, 12), (3, 1, 14, 3), (11, 8, 14, 3)),
            (datetime(2026, 12, 20, 12), (1, 1, 14, 0), (12, 31, 14, 0)),
            (datetime(2027, 3, 12, 12), (3, 14, 7, 0), (3, 17, 7, 0)),
            (datetime(2026, 12, 30, 12), (12, 28, 7, 3), (1, 5, 7, 3)),
        ],
        ids=["march_to_november", "january_to_december", "14_to_17_march", "28_december_to_5_january"],
    )
    def test_a_pass_is_owed_when_the_day_of_a_shown_season_moves(self, now, before, after):
        assert self._owed(now, before, after) is True

    def test_moving_it_back_after_a_pass_hid_it_is_owed_a_pass(self):
        """17 to 16 March hid the row under the old rule; moving it back to 17 asked the gate about 16, which the
        new day's window holds, so no pass ran and the hidden row stayed hidden until the next run."""
        now = datetime(2027, 3, 12, 12)
        assert self._owed(now, (3, 16, 7, 0), (3, 17, 7, 0)) is True

    def test_each_of_two_moves_before_a_run_is_owed_a_pass(self):
        """10 to 17 to 24 June with no run between: the second move was judged against 17, a day the row was never
        built for, while the ledger still says 10."""
        now = datetime(2026, 6, 18, 12)
        assert self._owed(now, (6, 10, 7, 10), (6, 17, 7, 10)) is True
        assert self._owed(now, (6, 17, 7, 10), (6, 24, 7, 10)) is True

    def test_a_row_hidden_before_and_after_is_owed_none(self):
        assert self._owed(datetime(2026, 10, 2, 12), (11, 26, 14, 0), (11, 26, 3, 0)) is False

    def test_a_row_showing_another_season_is_owed_none(self):
        """It shows Halloween today either way: the edit moves nothing it shows."""
        now = datetime(2026, 10, 15, 12)
        assert self._owed(now, (3, 1, 7, 0), (4, 1, 7, 0), seasons=("halloween", "moved")) is False


class TestDelete:
    def test_a_season_that_is_a_rows_only_season_is_not_deleted(self, client: TestClient, monkeypatch):
        # 20 Nov: both rows show Thanksgiving today, and neither shows anything once it is gone.
        _on(monkeypatch, datetime(2026, 11, 20, 12, 0))
        _create(client)
        a = _row(client, "Row A", ["halloween", "thanksgiving"])
        b = _row(client, "Row B", ["thanksgiving"])
        before = _rows(client)

        r = client.delete("/api/seasons/thanksgiving")

        assert r.status_code == 409
        assert r.json()["detail"] == (
            "“Thanksgiving” is the only season in “Row B”. Give that row another season, or delete it, first."
        )
        assert _rows(client) == before
        assert "thanksgiving" in [s["slug"] for s in client.get("/api/seasons").json()]

        assert client.patch(
            f"/api/collections/{b['id']}", json={"name": "Row B", "seasons": ["christmas", "thanksgiving"]}
        ).is_success
        queued, drains = _jobs(monkeypatch)

        assert client.delete("/api/seasons/thanksgiving").status_code == 204

        after = _rows(client)
        assert (after[a["id"]], after[b["id"]]) == (["halloween"], ["christmas"])
        assert "thanksgiving" not in [s["slug"] for s in client.get("/api/seasons").json()]
        # Those rows no longer follow it, so whether they are on Home today is re-applied now.
        assert sorted(payload["row"] for _kind, payload in queued) == sorted([a["slug"], b["slug"]])
        assert {kind for kind, _payload in queued} == {"rows.visibility"}
        assert drains == ["season 'thanksgiving' was deleted"], "queued behind the response, never waited on"

    def test_a_row_whose_day_is_unchanged_by_the_delete_gets_no_pass(self, client: TestClient, monkeypatch):
        """On 2 October the row shows Halloween with or without Thanksgiving (#137 ARCH-LOW-2)."""
        _on(monkeypatch, datetime(2026, 10, 2, 12, 0))
        _create(client)
        _row(client, "Row A", ["halloween", "thanksgiving"])
        queued, drains = _jobs(monkeypatch)

        assert client.delete("/api/seasons/thanksgiving").status_code == 204

        assert (queued, drains) == ([], [])

    def test_the_default_row_is_named_as_the_rows_page_names_it(self, client: TestClient):
        """Its title is the global `row.name_template`; its own `name` column is stale seed data."""
        from shortlist.server.db.models import DEFAULT_SLUG
        from shortlist.server.settings_store import SettingsStore

        _create(client)
        _default_row_follows(client, "thanksgiving")
        with client.app.state.sessions() as session:
            SettingsStore(session).set("row.name_template", "✨ Picked for You")
            session.commit()
        listed = next(c for c in client.get("/api/collections").json() if c["slug"] == DEFAULT_SLUG)

        season = next(s for s in client.get("/api/seasons").json() if s["slug"] == "thanksgiving")
        detail = client.delete("/api/seasons/thanksgiving").json()["detail"]

        assert (
            season["used_by"]
            == [{"id": listed["id"], "name": "✨ Picked for You"}]
            == [{"id": listed["id"], "name": listed["name"]}]
        )
        assert detail.startswith("“Thanksgiving” is the only season in “✨ Picked for You”.")

    def test_every_row_it_is_the_only_season_of_is_named(self, client: TestClient):
        _create(client)
        _row(client, "Row B", ["thanksgiving"])
        _row(client, "Row C", ["thanksgiving"])
        detail = client.delete("/api/seasons/thanksgiving").json()["detail"]
        assert detail == (
            "“Thanksgiving” is the only season in “Row B” and “Row C”. "
            "Give those rows another season, or delete them, first."
        )

    def test_a_refusal_names_a_row_as_it_reads_in_this_season(self, client: TestClient):
        _create(client)
        _row(client, "{season_emoji} {season} picks", ["thanksgiving"])
        detail = client.delete("/api/seasons/thanksgiving").json()["detail"]
        assert detail.startswith("“Thanksgiving” is the only season in “🦃 Thanksgiving picks”.")


class TestASeasonCannotGiveTwoRowsOneTitle:
    """#137 I-2. A row named after its season wears the season's name, so a season can give it the title of a plain
    row beside it, and delivery would write both rows into one Plex collection. The row editor refuses that name;
    a season's name and a newly ticked season are checked the same way."""

    def _seasonal_row(self, client: TestClient, seasons: list[str] | None = None, **fields) -> dict:
        body = {"name": "{season} picks", "seasons": seasons or ["christmas"], **fields}
        r = client.post("/api/collections", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def _plain_row(self, client: TestClient, name: str, **fields) -> dict:
        r = client.post("/api/collections", json={"name": name, **fields})
        assert r.status_code == 201, r.text
        return r.json()

    def test_a_new_season_that_would_title_a_row_like_another_is_refused(self, client: TestClient):
        self._seasonal_row(client)
        self._plain_row(client, "Diwali picks")

        r = client.post("/api/seasons", json=_body(name="Diwali", emoji="🪔"))

        assert r.status_code == 422
        assert r.json()["detail"].startswith(
            "This season would title “{season} picks” “Diwali picks”, the title “Diwali picks” already has"
        )
        assert "diwali" not in [s["slug"] for s in client.get("/api/seasons").json()], "nothing was saved"

    def test_every_row_named_after_its_season_is_checked_not_only_its_followers(self, client: TestClient):
        """Any of them may tick the season later; the row editor would then be the first to find the clash."""
        self._seasonal_row(client, ["halloween"])
        self._plain_row(client, "Thanksgiving picks")

        assert client.post("/api/seasons", json=_body()).status_code == 422

    def test_renaming_a_season_is_checked_too(self, client: TestClient):
        _create(client)
        self._seasonal_row(client, ["thanksgiving"])
        self._plain_row(client, "Turkey Day picks")

        r = client.put("/api/seasons/thanksgiving", json=_body(name="Turkey Day"))

        assert r.status_code == 422 and "“Turkey Day picks”" in r.json()["detail"]
        assert [s["name"] for s in client.get("/api/seasons").json() if s["slug"] == "thanksgiving"] == ["Thanksgiving"]

    def test_rows_that_never_build_in_one_library_may_share_a_title(self, client: TestClient):
        self._seasonal_row(client, media="movie")
        self._plain_row(client, "Diwali picks", media="show")

        assert client.post("/api/seasons", json=_body(name="Diwali", emoji="🪔")).status_code == 201

    def test_ticking_a_season_that_titles_the_row_like_another_is_refused(self, client: TestClient):
        """The PATCH checked a row's title only when its name, libraries or build moved, so ticking a season was
        the one door left open. The clash is set up as an older database can hold it: the plain row renamed
        after the season existed."""
        from shortlist.server.db.models import Collection

        _create(client)
        seasonal = self._seasonal_row(client, ["christmas"])
        plain = self._plain_row(client, "Turkey picks")
        with client.app.state.sessions() as session:
            row = session.get(Collection, plain["id"])
            row.name = "Thanksgiving picks"
            session.commit()

        r = client.patch(
            f"/api/collections/{seasonal['id']}",
            json={"name": "{season} picks", "seasons": ["christmas", "thanksgiving"]},
        )

        assert r.status_code == 422
        assert r.json()["detail"].startswith(
            "'Thanksgiving picks' is already the title of the row 'Thanksgiving picks'"
        )
        assert _rows(client)[seasonal["id"]] == ["christmas"]

    def test_ticking_a_season_with_no_clash_is_allowed_beside_one_that_has(self, client: TestClient):
        """Only the seasons ticked are rendered, so an existing clash in a season the row does not tick does not
        block an unrelated edit."""
        from shortlist.server.db.models import Collection

        _create(client)
        seasonal = self._seasonal_row(client, ["christmas"])
        plain = self._plain_row(client, "Turkey picks")
        with client.app.state.sessions() as session:
            session.get(Collection, plain["id"]).name = "Thanksgiving picks"
            session.commit()

        r = client.patch(
            f"/api/collections/{seasonal['id']}",
            json={"name": "{season} picks", "seasons": ["valentines", "christmas"]},
        )

        assert r.status_code == 200, r.text


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
        assert (thanksgiving["label"], thanksgiving["name"]) == ("Thanksgiving (US)", "Thanksgiving")
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
        body = {k: v for k, v in preset.items() if k not in ("key", "label", "note")}
        r = client.post("/api/seasons", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["excluded_genres"] == [27]

    def test_two_regions_of_one_holiday_cannot_both_be_added_under_one_name(self, client: TestClient):
        """Names are unique (D13): the second asks the owner for another name rather than titling two rows alike."""
        offered = {p["key"]: p for p in client.get("/api/seasons/presets").json()}
        bodies = [
            {k: v for k, v in offered[key].items() if k not in ("key", "label", "note")}
            for key in ("fathers_day", "fathers_day_au_nz")
        ]
        bodies = [{**body, "picks": [{"tmdb_id": 1, "media_type": "movie", "title": "Big Fish"}]} for body in bodies]

        assert client.post("/api/seasons", json=bodies[0]).status_code == 201
        second = client.post("/api/seasons", json=bodies[1])
        assert (second.status_code, second.json()["detail"]) == (422, "There's already a season called “Father's Day”.")


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

    class _Libraries(_Plex):
        """Movies (1) and 4K (5), both films, and TV (2). Each holds its own titles; one TV collection."""

        SECTIONS: ClassVar[list] = [
            SimpleNamespace(key="1", type="movie", title="Movies"),
            SimpleNamespace(key="5", type="movie", title="4K"),
            SimpleNamespace(key="2", type="show", title="TV"),
        ]
        HELD: ClassVar[dict] = {"1": {1: 101}, "5": {2: 502}, "2": {10: 210, 11: 211}}

        def sections(self) -> list:
            return list(self.SECTIONS)

        def build_library_index(self, section) -> dict[int, int]:
            self.scans += 1
            return dict(self.HELD[section.key])

        def collection_members(self, section_key: str, title: str) -> list[LibraryTitle] | None:
            return [LibraryTitle(11, MediaType.SHOW, "A Thanksgiving Special", 2001)] if section_key == "2" else None

    #: Films 1 and 2 and shows 10 and 11 carry the tag; the TV library's collection holds show 11.
    BOTH_KINDS: ClassVar[dict] = {
        (MediaType.MOVIE, "4543"): [
            {"id": 1, "title": "Planes, Trains and Automobiles", "vote_count": 1500},
            {"id": 2, "title": "Pieces of April", "vote_count": 90},
        ],
        (MediaType.SHOW, "4543"): [
            {"id": 10, "name": "Thanksgiving Reunion", "vote_count": 40},
            {"id": 11, "name": "A Thanksgiving Special", "vote_count": 30},
        ],
    }
    TV_COLLECTION: ClassVar[dict] = {"section_key": "2", "section_title": "TV", "title": "Thanksgiving TV"}

    @pytest.mark.parametrize(
        ("media", "library_keys", "total", "collection"),
        [
            ("movie", [], 2, 0),
            ("show", [], 2, 1),
            ("both", [], 4, 1),
            ("movie", ["1"], 1, 0),
            ("both", ["5", "2"], 3, 1),
        ],
        ids=["films_row", "shows_row", "both_row", "films_row_in_one_library", "both_row_in_two_libraries"],
    )
    def test_it_counts_only_what_the_row_it_was_opened_from_can_draw(
        self, client: TestClient, monkeypatch, media, library_keys, total, collection
    ):
        """#137 I-1: St Patrick's read "72 films" on a real server that held 56 of them — the other 16 were
        shows, which a films row never draws, and a library the row does not build in counted too."""
        _connect(monkeypatch, client, _Tmdb(self.BOTH_KINDS), self._Libraries())

        result = self._preview(
            client, tags=[self.TAG], collections=[self.TV_COLLECTION], media=media, library_keys=library_keys
        )

        assert result["total"] == total
        assert result["per_tag"] == {"4543": total}
        assert result["per_collection"][0]["in_library"] == collection

    @pytest.mark.parametrize(
        ("media", "library_keys", "movies", "shows"),
        [
            ("both", [], 2, 2),
            ("movie", [], 2, None),
            ("show", [], None, 2),
            ("both", ["1", "5"], 2, None),
            ("both", ["2"], None, 2),
        ],
        ids=["both_row", "films_row", "shows_row", "both_row_in_film_libraries", "both_row_in_its_tv_library"],
    )
    def test_it_splits_the_count_by_type_for_each_type_the_row_builds_in(
        self, client: TestClient, monkeypatch, media, library_keys, movies, shows
    ):
        """A row of both builds one collection per library, each filled from its own type: "Enough titles" for 40
        films and 0 shows left the TV library empty (#137 round 2). A type the row builds in no library of is
        null, not 0, so the editor never warns about a library the row does not have."""
        _connect(monkeypatch, client, _Tmdb(self.BOTH_KINDS), self._Libraries())

        result = self._preview(client, tags=[self.TAG], media=media, library_keys=library_keys)

        assert (result["movies"], result["shows"]) == (movies, shows)

    def test_without_a_row_it_counts_every_library_of_both_kinds(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, _Tmdb(self.BOTH_KINDS), self._Libraries())
        assert self._preview(client, tags=[self.TAG])["total"] == 4

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


class TestNextDate:
    """The editor's "Next: …" line, from the rule alone: no TMDB key or Plex needed, so a failed count never
    takes the date with it."""

    def test_a_rule_says_when_it_next_falls_without_tmdb_or_plex(self, client: TestClient, monkeypatch):
        _connect(monkeypatch, client, None, None)
        _on(monkeypatch, datetime(2026, 10, 3, 12, 0))
        r = client.post("/api/seasons/next-date", json={"kind": "nth", "month": 11, "nth": 4, "weekday": 3})
        assert (r.status_code, r.json()) == (200, {"next_date": "2026-11-26", "rule_error": None})

    def test_a_day_already_past_this_year_falls_next_year(self, client: TestClient, monkeypatch):
        _on(monkeypatch, datetime(2026, 10, 3, 12, 0))
        r = client.post("/api/seasons/next-date", json={"kind": "fixed", "month": 3, "day": 17})
        assert r.json() == {"next_date": "2027-03-17", "rule_error": None}

    def test_a_rule_that_cant_be_used_says_why(self, client: TestClient):
        r = client.post("/api/seasons/next-date", json={"kind": "fixed", "month": 2, "day": 29})
        assert r.json() == {
            "next_date": None,
            "rule_error": "29 February isn't every year — pick 28 February or 1 March.",
        }


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
                "media_type": "movie",
            },
            {
                "section_key": "1",
                "section_title": "Movies",
                "title": "Thanksgiving Movies",
                "count": 12,
                "smart": False,
                "media_type": "movie",
            },
        ]

    def test_a_tv_collection_says_it_holds_shows(self, client: TestClient, monkeypatch):
        tv = LibraryCollection("2", "TV", "Thanksgiving Episodes", 4, False, MediaType.SHOW)
        _connect(monkeypatch, client, None, _Plex(collections=[tv]))

        assert client.get("/api/seasons/plex-collections?q=thanks").json()[0]["media_type"] == "show"

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
