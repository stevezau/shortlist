from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.clients.arr import ArrError
from shortlist.engine.clients.seerr import PartialRead, SeerrError
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import row_marker
from shortlist.engine.models import (
    ArrTarget,
    EngineConfig,
    MediaType,
    RequestSources,
    RowSpec,
    SeerrTarget,
    UserProfile,
    UserType,
)
from shortlist.engine.requests_row import (
    RequestedTitle,
    RequestLedger,
    TagMatch,
    build_requests_picks,
    collect_requests,
    parse_requester_tag,
    pattern_matches,
)
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user

FIX = Path(__file__).resolve().parents[1] / "fixtures"
REQS = json.loads((FIX / "overseerr_requests_page.json").read_text())
RADARR = json.loads((FIX / "radarr_request_tags.json").read_text())
SONARR = json.loads((FIX / "sonarr_request_tags.json").read_text())
SEERR = SeerrTarget(url="http://seerr", api_key="k")
ARR = ArrTarget(url="http://arr", api_key="k", quality_profile_id=0, root_folder="", tag="shortlist")


def _person(n: int, **kw) -> UserProfile:
    return UserProfile(
        username=f"person{n}", plex_account_id=100000 + n, user_type=UserType.SHARED, slug=f"person{n}", **kw
    )


def _people() -> list[UserProfile]:
    """The fixture's requesters: Seerr user ids 10-17 remapped to ``person10``..``person17``."""
    return [_person(n) for n in range(10, 18)]


def _seerr(requests=None, users=None, media=None) -> MagicMock:
    c = MagicMock()
    c.requests.return_value = REQS["results"] if requests is None else requests
    c.user_plex_ids.return_value = (
        users if users is not None else {r["requestedBy"]["id"]: r["requestedBy"]["plexId"] for r in REQS["results"]}
    )
    c.arr_settings.return_value = {"radarr": [{"name": "r", "is4k": False, "tagRequests": True}], "sonarr": []}
    c.media_dates.return_value = media or {}
    return c


def _radarr(items=None, tags=None) -> MagicMock:
    c = MagicMock()
    c.app_name = "Radarr"
    c.tags.return_value = {t["id"]: t["label"] for t in (tags or RADARR["tags"])}
    c.movies.return_value = RADARR["tagged_items"] if items is None else items
    return c


def _sonarr(items=None) -> MagicMock:
    c = MagicMock()
    c.app_name = "Sonarr"
    c.tags.return_value = {t["id"]: t["label"] for t in SONARR["tags"]}
    c.series.return_value = SONARR["tagged_items"] if items is None else items
    return c


class TestTagParsing:
    @pytest.mark.parametrize(
        "label,expected", [("12-sarah", 12), ("12 - sarah", 12), ("12-", None), ("sarah", None), ("requested", None)]
    )
    def test_parse_requester_tag(self, label, expected):
        assert parse_requester_tag(label) == expected

    def test_pattern_matches_username_and_name_case_and_dash_insensitively(self):
        sarah = _person(1, nickname="Sarah Jones")
        assert pattern_matches("req-person1", "req-{username}", [sarah]) == [sarah]
        assert pattern_matches("REQ-PERSON1", "req-{username}", [sarah]) == [sarah]
        assert pattern_matches("sarah-jones", "{name}", [sarah]) == [sarah]
        assert pattern_matches("req-nobody", "req-{username}", [sarah]) == []

    def test_a_pattern_without_a_placeholder_matches_nobody(self):
        assert pattern_matches("req-person1", "req-person1", [_person(1)]) == []


class TestCollectFromSeerr:
    def test_requests_map_to_people_by_plex_id_only(self):
        people = _people()
        ledger = collect_requests(RequestSources(overseerr=SEERR), people, seerr=_seerr())
        assert ledger.complete and ledger.problems == []
        by_person = {p.plex_account_id: len(ledger.for_person(p.plex_account_id)) for p in people}
        assert sum(by_person.values()) == 7
        assert ledger.seerr_requests == 7 and ledger.seerr_linked == 6

    def test_a_requester_with_no_plex_id_gets_no_titles(self):
        req = dict(REQS["results"][0])
        req["requestedBy"] = {**req["requestedBy"], "plexId": None}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR),
            [_person(10)],
            seerr=_seerr(requests=[req], users={req["requestedBy"]["id"]: None}),
        )
        assert ledger.titles == [] and ledger.complete

    def test_pending_and_declined_requests_are_ignored(self):
        rows = [dict(REQS["results"][0], status=1), dict(REQS["results"][1], status=3)]
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=_seerr(requests=rows))
        assert ledger.titles == []

    def test_a_tv_request_is_landed_only_when_the_request_is_completed(self):
        tv = next(r for r in REQS["results"] if r["type"] == "tv")
        # A second show for the same person: the same tmdbId would merge into one title.
        waiting = dict(
            tv,
            status=2,
            seasons=[dict(tv["seasons"][0], status=2)],
            media={**tv["media"], "tmdbId": tv["media"]["tmdbId"] + 1},
        )
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=_seerr(requests=[tv, waiting]))
        landed = {t.tmdb_id: t.seasons_landed for t in ledger.titles}
        assert landed == {tv["media"]["tmdbId"]: True, waiting["media"]["tmdbId"]: False}

    def test_the_request_as_account_is_left_out(self):
        req = REQS["results"][0]
        src = RequestSources(overseerr=SEERR, exclude_seerr_user_id=req["requestedBy"]["id"])
        ledger = collect_requests(src, _people(), seerr=_seerr(requests=[req]))
        assert ledger.titles == []

    def test_a_4k_request_lands_on_status4k_not_status(self):
        req = dict(REQS["results"][0], is4k=True, status=2)
        req["media"] = {**req["media"], "status": 5, "status4k": 3}
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=_seerr(requests=[req]))
        assert [t.on_disk for t in ledger.titles] == [False]

    def test_two_seerr_accounts_with_one_plex_id_both_belong_to_that_person(self):
        a, b = REQS["results"][0], dict(REQS["results"][1])
        b["requestedBy"] = {**b["requestedBy"], "id": 999, "plexId": a["requestedBy"]["plexId"]}
        users = {a["requestedBy"]["id"]: a["requestedBy"]["plexId"], 999: a["requestedBy"]["plexId"]}
        person = UserProfile(username="x", plex_account_id=a["requestedBy"]["plexId"], user_type=UserType.SHARED)
        ledger = collect_requests(RequestSources(overseerr=SEERR), [person], seerr=_seerr(requests=[a, b], users=users))
        assert len(ledger.for_person(person.plex_account_id)) == 2

    def test_zero_users_and_zero_requests_reads_as_a_failed_read(self):
        ledger = collect_requests(RequestSources(overseerr=SEERR), [_person(10)], seerr=_seerr(requests=[], users={}))
        assert ledger.complete is False and any("Overseerr" in p for p in ledger.problems)

    def test_a_seerr_error_marks_the_ledger_incomplete_and_keeps_going(self):
        c = _seerr()
        c.requests.side_effect = SeerrError("down")
        ledger = collect_requests(RequestSources(overseerr=SEERR, radarr=ARR), _people(), seerr=c, radarr=_radarr())
        assert ledger.complete is False
        assert any(t.found_in == ("tag",) for t in ledger.titles)  # Radarr still read

    def test_a_partial_request_page_marks_the_ledger_incomplete(self):
        """A page that stops short of what `pageInfo` promised is a failed read, not a shorter list: the
        titles it dropped would read as "nothing requested" and take rows down."""
        c = _seerr()
        c.requests.side_effect = PartialRead("read 100 of the 300 rows /request says it has")
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=c)
        assert ledger.complete is False
        assert "Overseerr" in ledger.unreadable

    def test_a_plex_id_serialised_as_a_string_still_maps(self):
        """`requestedBy.plexId` is read like `user_plex_ids` reads it — through `_int_or_none` — so a
        server that serialises it as a string does not silently drop every request."""
        req = dict(REQS["results"][0])
        req["requestedBy"] = {**req["requestedBy"], "plexId": str(req["requestedBy"]["plexId"])}
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=_seerr(requests=[req], users={}))
        assert [t.plex_account_id for t in ledger.titles] == [int(req["requestedBy"]["plexId"])]

    def test_unreadable_names_only_the_source_whose_read_failed(self):
        """Advice about Overseerr is not an Overseerr outage: a healthy Radarr next to a missing
        Overseerr reports the tags it cannot trace, and nothing is unreadable."""
        advised = collect_requests(RequestSources(radarr=ARR), [_person(10)], radarr=_radarr())
        assert advised.unreadable == set() and any("Overseerr" in p for p in advised.problems)

        seerr = _seerr()
        seerr.requests.side_effect = SeerrError("down")
        down = collect_requests(RequestSources(overseerr=SEERR, radarr=ARR), _people(), seerr=seerr, radarr=_radarr())
        assert down.unreadable == {"Overseerr"}

        empty = collect_requests(RequestSources(overseerr=SEERR), [_person(10)], seerr=_seerr(requests=[], users={}))
        assert empty.unreadable == {"Overseerr"}

        radarr = _radarr()
        radarr.movies.side_effect = ArrError("down")
        arr_down = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), _people(), seerr=_seerr(), radarr=radarr
        )
        assert arr_down.unreadable == {"Radarr"}

    def test_seerr_plex_ids_is_every_linked_account_not_every_requester(self):
        users = {10: 100010, 99: 100099, 5: None}  # 99 asked for nothing; 5 is a local account
        ledger = collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=_seerr(users=users))
        assert ledger.seerr_plex_ids == {100010, 100099}


class TestCollectFromTags:
    def test_overseerr_tags_resolve_through_seerr_users(self):
        people = _people()
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), people, seerr=_seerr(requests=[]), radarr=_radarr()
        )
        tagged = [t for t in ledger.titles if "tag" in t.found_in]
        assert len(tagged) == len(RADARR["tagged_items"])
        assert all(t.plex_account_id in {p.plex_account_id for p in people} for t in tagged)

    def test_a_tag_in_both_arrs_is_listed_once_with_its_titles_summed(self):
        """Radarr and Sonarr each carry `14-person14` (one tagged item apiece). The editor's tag preview
        showed the tag twice, once per arr; the owner wants one line per tag."""
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR, sonarr=ARR),
            _people(),
            seerr=_seerr(requests=[]),
            radarr=_radarr(),
            sonarr=_sonarr(),
        )
        matches = [m for m in ledger.tag_matches if m.label == "14-person14"]
        assert matches == [
            TagMatch(label="14-person14", source="overseerr", plex_account_id=100014, titles=2, ambiguous=False)
        ]
        assert len({(m.label, m.source) for m in ledger.tag_matches}) == len(ledger.tag_matches)

    def test_overseerr_tags_without_overseerr_connected_are_reported_not_guessed(self):
        ledger = collect_requests(RequestSources(radarr=ARR), [_person(10)], radarr=_radarr())
        assert ledger.titles == []
        assert any("Overseerr" in p and "tag" in p.lower() for p in ledger.problems)
        assert ledger.complete  # a read that WORKED, with nothing we may use

    def test_radarr_on_disk_follows_has_file_and_lands_on_the_file_date(self):
        item = dict(RADARR["tagged_items"][0], hasFile=True, movieFile={"dateAdded": "2026-09-16T03:00:00Z"})
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            _people(),
            seerr=_seerr(requests=[]),
            radarr=_radarr(items=[item]),
        )
        (t,) = ledger.titles
        assert t.on_disk and t.landed_at == datetime(2026, 9, 16, 3, tzinfo=UTC)

    def test_sonarr_on_disk_needs_an_episode_file(self):
        item = dict(SONARR["tagged_items"][0])
        item["statistics"] = {**item["statistics"], "episodeFileCount": 0}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, sonarr=ARR),
            _people(),
            seerr=_seerr(requests=[]),
            sonarr=_sonarr(items=[item]),
        )
        assert [t.on_disk for t in ledger.titles] == [False]

    def test_a_series_without_tmdb_id_is_skipped_and_reported(self):
        item = dict(SONARR["tagged_items"][0])
        item.pop("tmdbId", None)
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, sonarr=ARR),
            _people(),
            seerr=_seerr(requests=[]),
            sonarr=_sonarr(items=[item]),
        )
        assert ledger.titles == [] and any("TMDB" in p for p in ledger.problems)

    def test_own_pattern_tags_match_people_and_skip_shortlists_own_items(self):
        sarah = _person(1)
        tags = [{"id": 1, "label": "req-person1"}, {"id": 2, "label": "shortlist"}]
        asked = {
            "tmdbId": 501,
            "tags": [1],
            "hasFile": True,
            "movieFile": {"dateAdded": "2026-09-01T00:00:00Z"},
            "title": "A",
        }
        ours = {
            "tmdbId": 502,
            "tags": [1, 2],
            "hasFile": True,
            "movieFile": {"dateAdded": "2026-09-01T00:00:00Z"},
            "title": "B",
        }
        src = RequestSources(radarr=ARR, shortlist_tag="shortlist")
        ledger = collect_requests(
            src, [sarah], radarr=_radarr(items=[asked, ours], tags=tags), patterns=frozenset({"req-{username}"})
        )
        assert [t.tmdb_id for t in ledger.for_person(sarah.plex_account_id)] == [501]
        assert [t.pattern for t in ledger.titles] == ["req-{username}"]

    def test_an_ambiguous_pattern_tag_is_ignored_and_listed(self):
        a = _person(1, nickname="Sam")
        b = _person(2, nickname="Sam")
        tags = [{"id": 1, "label": "sam"}]
        item = {"tmdbId": 501, "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(
            RequestSources(radarr=ARR), [a, b], radarr=_radarr(items=[item], tags=tags), patterns=frozenset({"{name}"})
        )
        assert ledger.titles == []
        assert [(m.label, m.ambiguous) for m in ledger.tag_matches] == [("sam", True)]

    def test_a_per_person_override_tag_wins(self):
        kids = _person(3, requested_by_tag="children")
        tags = [{"id": 7, "label": "children"}]
        item = {"tmdbId": 501, "tags": [7], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(RequestSources(radarr=ARR), [kids], radarr=_radarr(items=[item], tags=tags))
        assert [t.plex_account_id for t in ledger.titles] == [kids.plex_account_id]

    def test_a_title_in_seerr_and_tagged_counts_once_and_keeps_seerr_dates(self):
        # A movie request, so the Radarr item lands on the same (movie, tmdbId, person) key.
        req = next(r for r in REQS["results"] if r["type"] == "movie")
        tag_label = next(t["label"] for t in RADARR["tags"] if t["label"].startswith(f"{req['requestedBy']['id']}-"))
        tag_id = next(t["id"] for t in RADARR["tags"] if t["label"] == tag_label)
        item = {
            "tmdbId": req["media"]["tmdbId"],
            "tags": [tag_id],
            "hasFile": True,
            "movieFile": {"dateAdded": "2020-01-01T00:00:00Z"},
            "title": "A",
        }
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            _people(),
            seerr=_seerr(requests=[req]),
            radarr=_radarr(items=[item]),
        )
        (t,) = [t for t in ledger.titles if t.tmdb_id == req["media"]["tmdbId"]]
        assert t.found_in == ("overseerr", "tag")
        assert t.requested_at == datetime.fromisoformat(req["createdAt"].replace("Z", "+00:00"))

    def test_a_title_in_seerr_and_own_pattern_tagged_is_not_bound_to_the_pattern(self):
        # Overseerr proof stands on its own: every requests row for the person keeps the title,
        # whatever tag pattern that row uses.
        req = next(r for r in REQS["results"] if r["type"] == "movie")
        who = _person(req["requestedBy"]["id"])
        tags = [{"id": 1, "label": f"req-{who.username}"}]
        item = {"tmdbId": req["media"]["tmdbId"], "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            [who],
            seerr=_seerr(requests=[req]),
            radarr=_radarr(items=[item], tags=tags),
            patterns=frozenset({"req-{username}"}),
        )
        (t,) = ledger.titles
        assert t.found_in == ("overseerr", "tag") and t.pattern == ""

    def test_a_tagged_title_whose_request_is_gone_dates_from_seerr_media(self):
        item = dict(RADARR["tagged_items"][0], hasFile=True, movieFile={"dateAdded": "2026-09-16T03:00:00Z"})
        media = {
            ("movie", item["tmdbId"]): {
                "mediaAddedAt": "2026-09-15T00:00:00.000Z",
                "lastSeasonChange": None,
                "tvdbId": None,
                "status": 5,
                "status4k": 1,
            }
        }
        seerr = _seerr(requests=[], media=media)
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR), _people(), seerr=seerr, radarr=_radarr(items=[item])
        )
        (t,) = ledger.titles
        assert t.landed_at == datetime(2026, 9, 15, tzinfo=UTC)
        seerr.media_dates.assert_called_once()

    def test_seerr_media_dates_are_not_read_when_every_title_has_a_live_request(self):
        seerr = _seerr()
        collect_requests(RequestSources(overseerr=SEERR), _people(), seerr=seerr)
        seerr.media_dates.assert_not_called()

    def test_an_override_tag_shared_by_two_people_is_ambiguous_and_listed(self):
        a = _person(1, requested_by_tag="fam")
        b = _person(2, requested_by_tag="fam")
        tags = [{"id": 1, "label": "fam"}]
        item = {"tmdbId": 501, "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(RequestSources(radarr=ARR), [a, b], radarr=_radarr(items=[item], tags=tags))
        assert ledger.titles == []
        assert ledger.tag_matches == [
            TagMatch(label="fam", source="override", plex_account_id=None, titles=1, ambiguous=True)
        ]

    def test_a_tag_shared_with_someone_outside_tonights_run_still_names_nobody(self):
        """`people` is the ROSTER — everyone who could own a tag — not the run's scope. The person who
        shares the tag may be disabled, paused or simply not in tonight's run; the one who IS gets
        nothing from it either way, or a disabled person's requests would land on their row."""
        in_run = _person(1, requested_by_tag="fam")
        outside = _person(2, requested_by_tag="fam")
        tags = [{"id": 1, "label": "fam"}]
        item = {"tmdbId": 501, "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(
            RequestSources(radarr=ARR), [in_run, outside], radarr=_radarr(items=[item], tags=tags)
        )
        assert ledger.for_person(in_run.plex_account_id) == []
        assert ledger.titles == []
        (match,) = ledger.tag_matches
        assert match.ambiguous and match.plex_account_id is None

    @pytest.mark.parametrize("tag_order", [[1, 2], [2, 1]])
    def test_a_title_tagged_by_override_and_pattern_keeps_the_override(self, tag_order):
        who = _person(1, requested_by_tag="fam")
        tags = [{"id": 1, "label": "fam"}, {"id": 2, "label": "req-person1"}]
        item = {"tmdbId": 501, "tags": tag_order, "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(
            RequestSources(radarr=ARR),
            [who],
            radarr=_radarr(items=[item], tags=tags),
            patterns=frozenset({"req-{username}"}),
        )
        assert [(t.tmdb_id, t.pattern) for t in ledger.titles] == [(501, "")]

    def test_a_tag_only_show_reads_seerr_media_dates_under_the_tv_key(self):
        item = SONARR["tagged_items"][0]  # tag 85 -> person14, episodes on disk
        tv = {"mediaAddedAt": "2026-09-10T00:00:00.000Z", "lastSeasonChange": "2026-09-20T00:00:00.000Z"}
        decoy = {"mediaAddedAt": "2000-01-01T00:00:00.000Z", "lastSeasonChange": "2000-01-01T00:00:00.000Z"}
        media = {("tv", item["tmdbId"]): tv, ("show", item["tmdbId"]): decoy}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, sonarr=ARR),
            _people(),
            seerr=_seerr(requests=[], media=media),
            sonarr=_sonarr(items=[item]),
        )
        (t,) = ledger.titles
        assert t.landed_at == datetime(2026, 9, 20, tzinfo=UTC)

    def test_a_radarr_error_marks_the_ledger_incomplete_and_overseerr_is_still_read(self):
        radarr = _radarr()
        radarr.tags.side_effect = ArrError("down")
        ledger = collect_requests(RequestSources(overseerr=SEERR, radarr=ARR), _people(), seerr=_seerr(), radarr=radarr)
        assert ledger.complete is False
        assert any("Radarr" in p for p in ledger.problems)
        assert len(ledger.titles) == 7 and all(t.found_in == ("overseerr",) for t in ledger.titles)

    def test_an_overseerr_tag_for_an_unknown_seerr_user_names_nobody(self):
        tags = [{"id": 1, "label": "77-ghost"}]
        item = {"tmdbId": 501, "tags": [1], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR),
            _people(),
            seerr=_seerr(requests=[]),
            radarr=_radarr(items=[item], tags=tags),
        )
        assert ledger.titles == []
        assert ledger.tag_matches == [
            TagMatch(label="77-ghost", source="overseerr", plex_account_id=None, titles=1, ambiguous=False)
        ]

    def test_the_request_as_accounts_tag_names_nobody(self):
        """Its Overseerr requests are left out (`exclude_seerr_user_id`), so its `<uid>-<name>` tag on a
        Radarr item must be too — that tag rides on every title Shortlist itself files."""
        item = {"tmdbId": 501, "tags": [50], "hasFile": True, "movieFile": {}, "title": "A"}  # 50 = "10-person10"
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR, exclude_seerr_user_id=10),
            _people(),
            seerr=_seerr(requests=[]),
            radarr=_radarr(items=[item]),
        )
        assert ledger.titles == []
        (match,) = [m for m in ledger.tag_matches if m.label == "10-person10"]
        assert match == TagMatch(
            label="10-person10", source="overseerr", plex_account_id=None, titles=1, ambiguous=False
        )

    def test_an_override_tag_in_overseerrs_own_shape_wins_even_with_overseerr_off(self):
        """An owner who typed `12-sarah` on a person meant it: the override is checked before the tag is
        read as an Overseerr requester tag, so it resolves with Overseerr off and raises no advice."""
        sarah = _person(1, requested_by_tag="12-sarah")
        tags = [{"id": 7, "label": "12-sarah"}]
        item = {"tmdbId": 501, "tags": [7], "hasFile": True, "movieFile": {}, "title": "A"}
        ledger = collect_requests(RequestSources(radarr=ARR), [sarah], radarr=_radarr(items=[item], tags=tags))
        assert [t.plex_account_id for t in ledger.titles] == [sarah.plex_account_id]
        assert [m.source for m in ledger.tag_matches] == ["override"]
        assert ledger.problems == []

    def test_shortlists_own_item_never_matches_an_override_tag_but_an_overseerr_tag_still_counts(self):
        kids = _person(3, requested_by_tag="children")
        tags = [{"id": 7, "label": "children"}, {"id": 2, "label": "shortlist"}, {"id": 50, "label": "10-person10"}]
        ours_for_kids = {"tmdbId": 501, "tags": [7, 2], "hasFile": True, "movieFile": {}, "title": "A"}
        ours_asked = {"tmdbId": 502, "tags": [50, 2], "hasFile": True, "movieFile": {}, "title": "B"}
        ledger = collect_requests(
            RequestSources(overseerr=SEERR, radarr=ARR, shortlist_tag="shortlist"),
            [kids, _person(10)],
            seerr=_seerr(requests=[]),
            radarr=_radarr(items=[ours_for_kids, ours_asked], tags=tags),
        )
        assert [(t.tmdb_id, t.plex_account_id) for t in ledger.titles] == [(502, _person(10).plex_account_id)]


def _after(barrier: threading.Barrier, value: object):
    def read() -> object:
        barrier.wait()
        return value

    return read


class TestTheSourcesAreReadAtOnce:
    """An Arr library is one whole-library dump its server takes seconds to build (live, 2026-10-04: 5.3s
    to first byte for 10,211 movies, 4.1s for 4,992 series). Read one after another, the Users page's
    Requests column waited for their sum — 13s."""

    def test_overseerr_radarr_and_sonarr_reads_overlap_when_all_three_are_configured(self):
        # Each read blocks until all three are in flight. One after another, the first waits out the
        # timeout, the barrier breaks, and every source reads as failed.
        barrier = threading.Barrier(3, timeout=5)
        seerr, radarr, sonarr = _seerr(), _radarr(), _sonarr()
        seerr.requests.side_effect = _after(barrier, REQS["results"])
        radarr.movies.side_effect = _after(barrier, RADARR["tagged_items"])
        sonarr.series.side_effect = _after(barrier, SONARR["tagged_items"])
        sources = RequestSources(overseerr=SEERR, radarr=ARR, sonarr=ARR)

        ledger = collect_requests(sources, _people(), seerr=seerr, radarr=radarr, sonarr=sonarr)

        assert ledger.complete is True, ledger.problems
        # Same ledger as reading them in turn: Arr tags still resolve through Overseerr's user list,
        # and titles, problems and tag matches keep their order whichever read finished first.
        assert ledger == collect_requests(sources, _people(), seerr=_seerr(), radarr=_radarr(), sonarr=_sonarr())

    def test_a_warning_logged_in_an_arr_read_carries_the_callers_log_context(self):
        from loguru import logger

        radarr = _radarr()

        def tags_that_warn():
            logger.warning("retrying")
            return {}

        radarr.tags.side_effect = tags_that_warn
        records = []
        sink_id = logger.add(lambda m: records.append(m.record), level="WARNING")
        try:
            with logger.contextualize(shortlist_run_log=42):
                collect_requests(RequestSources(radarr=ARR), _people(), radarr=radarr)
        finally:
            logger.remove(sink_id)

        assert [r["extra"].get("shortlist_run_log") for r in records if r["message"] == "retrying"] == [42]


def _title(tmdb_id, kind=MediaType.MOVIE, *, days_ago=1, person=100001, **kw):
    landed = datetime(2026, 9, 28, tzinfo=UTC) - timedelta(days=days_ago)
    base = dict(
        tmdb_id=tmdb_id,
        media_type=kind,
        plex_account_id=person,
        requested_at=landed - timedelta(days=2),
        landed_at=landed,
        on_disk=True,
        seasons_landed=True,
        found_in=("overseerr",),
    )
    return RequestedTitle(**{**base, **kw})


def _policy(section_index, *, watched_movies=(), watched_shows=None, visible=None):
    policy = MagicMock()
    policy.user = _person(1)
    policy.ctx.section_index = section_index
    policy.ctx.plex.fetch_items.side_effect = lambda keys: (
        [MagicMock(ratingKey=k, title=f"t{k}", year=2020) for k in keys],
        [],
    )
    policy.watched_movies = set(watched_movies)
    policy.watched_shows = watched_shows or {}
    policy.visible.side_effect = lambda keys: visible if visible is not None else None
    policy.report.trace = {}
    return policy


def _section(key="1", kind="movie"):
    s = MagicMock()
    s.key = key
    s.type = kind
    s.title = "Movies" if kind == "movie" else "TV Shows"
    return s


NOW = datetime(2026, 9, 28, tzinfo=UTC)
SPEC = RowSpec(slug="asked", name_template="📬 {library_name} you asked for", size=20, requests_row=True)


class TestBuildRequestsPicks:
    def test_newest_landed_first_and_only_titles_on_plex(self):
        titles = [_title(1, days_ago=5), _title(2, days_ago=1), _title(3, days_ago=2)]
        ledger = RequestLedger(titles=titles, complete=True)
        policy = _policy({"1": {1: 11, 2: 22}})
        picks = build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2, 1]
        assert [p.rank for p in picks["1"]] == [1, 2]
        results = {r["tmdb_id"]: r["result"] for r in policy.report.trace["selection"][0]["requests"]}
        assert results == {2: "in_row", 1: "in_row", 3: "not_on_plex"}

    def test_a_watched_movie_and_a_finished_show_drop_but_an_unfinished_show_stays(self):
        ledger = RequestLedger(titles=[_title(1), _title(2, MediaType.SHOW), _title(3, MediaType.SHOW)], complete=True)
        policy = _policy(
            {"1": {1: 11}, "2": {2: 22, 3: 33}}, watched_movies={1}, watched_shows={2: (10, 10), 3: (4, 10)}
        )
        picks = build_requests_picks(policy, SPEC, [_section(), _section("2", "show")], 20, ledger, now=NOW)
        assert picks["1"] == [] and [p.tmdb_id for p in picks["2"]] == [3]

    def test_a_show_plex_holds_no_episodes_of_is_not_finished(self):
        """`(0, 0)` reads as "nothing to watch", never as "watched everything"."""
        ledger = RequestLedger(titles=[_title(2, MediaType.SHOW)], complete=True)
        policy = _policy({"2": {2: 22}}, watched_shows={2: (0, 0)})
        picks = build_requests_picks(policy, SPEC, [_section("2", "show")], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["2"]] == [2]

    def test_a_season_that_has_not_landed_waits(self):
        ledger = RequestLedger(titles=[_title(2, MediaType.SHOW, seasons_landed=False)], complete=True)
        policy = _policy({"2": {2: 22}})
        picks = build_requests_picks(policy, SPEC, [_section("2", "show")], 20, ledger, now=NOW)
        assert picks["2"] == []
        assert policy.report.trace["selection"][0]["requests"][0]["result"] == "season_not_landed"

    def test_the_window_drops_old_arrivals_and_zero_keeps_them(self):
        ledger = RequestLedger(titles=[_title(1, days_ago=91), _title(2, days_ago=89)], complete=True)
        policy = _policy({"1": {1: 11, 2: 22}})
        assert [p.tmdb_id for p in build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)["1"]] == [2]
        # The window is recorded on the trace entry, so a `too_old` verdict can name the run's own setting.
        assert policy.report.trace["selection"][0]["requests_window_days"] == SPEC.requests_window_days == 90
        forever = RowSpec(slug="asked", name_template="n", size=20, requests_row=True, requests_window_days=0)
        policy = _policy({"1": {1: 11, 2: 22}})
        picks = build_requests_picks(policy, forever, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2, 1]
        assert policy.report.trace["selection"][0]["requests_window_days"] == 0

    def test_an_undated_title_is_kept_and_sorted_last(self):
        ledger = RequestLedger(titles=[_title(1, landed_at=None), _title(2, days_ago=1)], complete=True)
        picks = build_requests_picks(_policy({"1": {1: 11, 2: 22}}), SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2, 1]

    def test_hidden_titles_are_dropped_when_visibility_is_known(self):
        ledger = RequestLedger(titles=[_title(1), _title(2)], complete=True)
        policy = _policy({"1": {1: 11, 2: 22}}, visible={22})
        picks = build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2]
        results = {r["tmdb_id"]: r["result"] for r in policy.report.trace["selection"][0]["requests"]}
        assert results == {1: "hidden", 2: "in_row"}

    def test_the_row_size_caps_and_marks_the_rest(self):
        ledger = RequestLedger(titles=[_title(i, days_ago=i) for i in range(1, 5)], complete=True)
        policy = _policy({"1": {i: i * 11 for i in range(1, 5)}})
        picks = build_requests_picks(policy, SPEC, [_section()], 2, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [1, 2]
        assert [r["result"] for r in policy.report.trace["selection"][0]["requests"]][2:] == ["over_size", "over_size"]

    def test_only_this_persons_titles_and_this_rows_pattern(self):
        ledger = RequestLedger(
            titles=[
                _title(1, person=999),
                _title(2, found_in=("tag",), pattern="other-{username}"),
                _title(3, found_in=("tag",), pattern="req-{username}"),
            ],
            complete=True,
        )
        spec = RowSpec(
            slug="asked", name_template="n", size=20, requests_row=True, requests_tag_pattern="req-{username}"
        )
        picks = build_requests_picks(_policy({"1": {1: 11, 2: 22, 3: 33}}), spec, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [3]

    def test_a_pick_says_when_they_asked(self):
        ledger = RequestLedger(titles=[_title(1)], complete=True)
        (pick,) = build_requests_picks(_policy({"1": {1: 11}}), SPEC, [_section()], 20, ledger, now=NOW)["1"]
        assert pick.reason == "You asked for this on 25 Sep" and pick.sources == ["requests"] and pick.title == "t11"

    def test_a_title_plex_dropped_since_the_index_was_built_is_not_on_plex(self):
        ledger = RequestLedger(titles=[_title(1), _title(2)], complete=True)
        policy = _policy({"1": {1: 11, 2: 22}})
        policy.ctx.plex.fetch_items.side_effect = lambda keys: (
            [MagicMock(ratingKey=k, title=f"t{k}", year=2020) for k in keys if k != 11],
            [11],
        )
        picks = build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2] and picks["1"][0].rank == 1
        results = {r["tmdb_id"]: r["result"] for r in policy.report.trace["selection"][0]["requests"]}
        assert results == {1: "not_on_plex", 2: "in_row"}

    def test_a_trailer_standing_in_for_the_title_is_not_on_plex(self):
        """An Agregarr trailer (#151) carries the film's tmdb id, but it is not the film they asked for."""
        ledger = RequestLedger(titles=[_title(1), _title(2)], complete=True)
        policy = _policy({"1": {1: 11, 2: 22}})
        policy.ctx.plex.placeholder_keys.return_value = frozenset({11})
        picks = build_requests_picks(policy, SPEC, [_section()], 20, ledger, now=NOW)
        assert [p.tmdb_id for p in picks["1"]] == [2]
        policy.ctx.plex.placeholder_keys.assert_called_with("1", MediaType.MOVIE)
        results = {r["tmdb_id"]: r["result"] for r in policy.report.trace["selection"][0]["requests"]}
        assert results == {1: "not_on_plex", 2: "in_row"}


BOB_TAG = [{"id": 1, "label": "req-bob"}]
BOB_ITEM = {
    "tmdbId": 42,
    "tags": [1],
    "hasFile": True,
    "movieFile": {"dateAdded": "2026-09-27T00:00:00Z"},
    "title": "A",
}
BY_USERNAME, BY_NAME = "req-{username}", "req-{name}"


def _rows_holding(ledger: RequestLedger, people: list[UserProfile], patterns: frozenset[str]) -> dict:
    """``{(plex account id, row pattern): picked tmdb ids}`` — whose requests row would show what."""
    out = {}
    for person in people:
        for pattern in sorted(patterns):
            policy = _policy({"1": {BOB_ITEM["tmdbId"]: 4242}})
            policy.user = person
            spec = RowSpec(slug="asked", name_template="n", size=20, requests_row=True, requests_tag_pattern=pattern)
            picks = build_requests_picks(policy, spec, [_section()], 20, ledger, now=NOW)
            out[(person.plex_account_id, pattern)] = [p.tmdb_id for p in picks["1"]]
    return out


def _collect_bob_tag(people: list[UserProfile], patterns: frozenset[str]) -> RequestLedger:
    radarr = _radarr(items=[BOB_ITEM], tags=BOB_TAG)
    return collect_requests(RequestSources(radarr=ARR), people, radarr=radarr, patterns=patterns)


class TestOneTagAcrossSeveralPatterns:
    """A tag resolves against EVERY pattern the run's requests rows use, not the first that fits.

    Two rows using `req-{username}` and `req-{name}` render `req-bob` for whoever is called bob either way;
    stopping at the first pattern credited one of two people with the other's request and called it
    unambiguous. `_add_tagged` never reads `user_type` (a pattern renders username and nickname for every
    type), so the matrix below must give the same answer for owner, shared and managed alike.
    """

    @pytest.mark.parametrize("user_type", list(UserType))
    def test_a_tag_two_patterns_render_for_two_people_goes_to_nobody_and_is_reported(self, user_type):
        alice = UserProfile(username="alice", nickname="bob", plex_account_id=1, user_type=user_type, slug="alice")
        bob = UserProfile(username="bob", nickname="Robert", plex_account_id=2, user_type=user_type, slug="bob")
        both = frozenset({BY_USERNAME, BY_NAME})

        ledger = _collect_bob_tag([alice, bob], both)

        assert _rows_holding(ledger, [alice, bob], both) == {
            (1, BY_NAME): [],
            (1, BY_USERNAME): [],
            (2, BY_NAME): [],
            (2, BY_USERNAME): [],
        }
        assert ledger.titles == []
        assert ledger.tag_matches == [
            TagMatch(label="req-bob", source="pattern", plex_account_id=None, titles=1, ambiguous=True)
        ]

    @pytest.mark.parametrize("user_type", list(UserType))
    def test_one_person_two_patterns_render_it_for_gets_the_title_in_each_of_those_rows(self, user_type):
        bob = UserProfile(username="bob", nickname="bob", plex_account_id=2, user_type=user_type, slug="bob")
        patterns = frozenset({BY_USERNAME, BY_NAME, "other-{username}"})

        ledger = _collect_bob_tag([bob], patterns)

        assert _rows_holding(ledger, [bob], patterns) == {
            (2, "other-{username}"): [],
            (2, BY_NAME): [42],
            (2, BY_USERNAME): [42],
        }
        assert ledger.tag_matches == [
            TagMatch(label="req-bob", source="pattern", plex_account_id=2, titles=1, ambiguous=False)
        ]

    @pytest.mark.parametrize(
        "alice_nickname,patterns",
        [
            # The audit's baseline: Alice is nicknamed bob, but no row in the run renders a nickname.
            pytest.param("bob", frozenset({BY_USERNAME}), id="the-colliding-pattern-is-not-in-play"),
            pytest.param("Alice", frozenset({BY_USERNAME, BY_NAME}), id="a-second-pattern-that-names-nobody"),
        ],
    )
    def test_a_tag_only_one_person_renders_is_theirs_in_the_matching_row_only(self, alice_nickname, patterns):
        alice = UserProfile(
            username="alice", nickname=alice_nickname, plex_account_id=1, user_type=UserType.SHARED, slug="alice"
        )
        bob = UserProfile(username="bob", nickname="Robert", plex_account_id=2, user_type=UserType.SHARED, slug="bob")

        ledger = _collect_bob_tag([alice, bob], patterns)

        holding = _rows_holding(ledger, [alice, bob], patterns)
        assert {row: ids for row, ids in holding.items() if ids} == {(2, BY_USERNAME): [42]}
        assert ledger.tag_matches == [
            TagMatch(label="req-bob", source="pattern", plex_account_id=2, titles=1, ambiguous=False)
        ]


RADARR_AT = ArrTarget(url="http://radarr.test", api_key="RADARR-KEY-CANARY", quality_profile_id=0, root_folder="")
SONARR_AT = ArrTarget(url="http://sonarr.test", api_key="SONARR-KEY-CANARY", quality_profile_id=0, root_folder="")
#: 200 answers that are not the list of records Radarr/Sonarr serve. CANARY must never reach a message.
MALFORMED_ARR = [
    pytest.param(b'{"message": "CANARY"}', id="object"),
    pytest.param(b"null", id="null"),
    pytest.param(b'"CANARY"', id="string"),
    pytest.param(b'[{"id": 1, "label": "req-sarah", "tmdbId": 10, "tags": [1]}, "CANARY"]', id="list-holding-a-string"),
]
ARR_READS = [
    pytest.param(RADARR_AT, "/api/v3/tag", "Radarr", id="radarr-tags"),
    pytest.param(RADARR_AT, "/api/v3/movie", "Radarr", id="radarr-movies"),
    pytest.param(SONARR_AT, "/api/v3/tag", "Sonarr", id="sonarr-tags"),
    pytest.param(SONARR_AT, "/api/v3/series", "Sonarr", id="sonarr-series"),
]
EXISTING_ROW = fake_media_item(4242, "Movies you asked for" + row_marker(100))
SIBLING_ROW = fake_media_item(4343, "✨ Movies Picked for You" + row_marker(100))


@pytest.fixture
def requests_ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    """Sarah's requests row already on Plex beside a sibling row under her label, both Arrs as sources.

    The real pipeline, ledger read and `remove_row`; only Plex, plex.tv and the Arrs' HTTP are fakes.
    """
    plex = MagicMock()
    movies = MagicMock()
    movies.type, movies.title, movies.key = "movie", "Movies", "1"
    movies.collections.return_value = []
    plex.sections.return_value = [movies]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
    plex.build_library_index.return_value = {900: 999}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = [EXISTING_ROW, SIBLING_ROW]
    plex.stored_label.side_effect = lambda collection, label, *, extra=None: label.replace("shortlist", "Shortlist", 1)
    plex.fetch_items.side_effect = lambda keys: ([fake_media_item(k, f"item{k}") for k in keys], [])
    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]
    mock_plextv.users = [plextv_user(100, "sarah")]
    config = replace(
        engine_config,
        rows=[RowSpec(slug="asked", name_template="{library_name} you asked for", size=5, requests_row=True)],
        rows_defined=True,
        request_sources=RequestSources(radarr=RADARR_AT, sonarr=SONARR_AT),
    )
    return EngineContext(
        config=config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


def _run_with_arrs(ctx: EngineContext, answers: dict[str, httpx.Response]):
    """Sarah through the real pipeline, every Arr read answering an empty list unless ``answers`` overrides it."""
    with respx.mock(assert_all_called=False) as arrs:
        for target, path in (
            (RADARR_AT, "/api/v3/tag"),
            (RADARR_AT, "/api/v3/movie"),
            (SONARR_AT, "/api/v3/tag"),
            (SONARR_AT, "/api/v3/series"),
        ):
            url = f"{target.url}{path}"
            arrs.get(url).mock(return_value=answers.get(url, httpx.Response(200, json=[])))
        return pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]


class TestAMalformedArrAnswerNeverRemovesARow:
    """A 200 whose body is not a list of records is a FAILED read, not an empty one.

    Coerced to an empty list it made the ledger complete and empty, and the pipeline deleted the
    person's requests row and forgot its delivery on the strength of a read that did not happen.
    """

    @pytest.mark.parametrize("body", MALFORMED_ARR)
    @pytest.mark.parametrize("target,path,app", ARR_READS)
    def test_the_existing_row_survives_and_the_ledger_is_incomplete(
        self, requests_ctx: EngineContext, target, path, app, body
    ):
        report = _run_with_arrs(requests_ctx, {f"{target.url}{path}": httpx.Response(200, content=body)})

        requests_ctx.plex.delete_owned_collection.assert_not_called()
        assert report.removed_deliveries == []
        assert not (report.diff and report.diff.deleted)
        ledger = requests_ctx.request_ledger
        assert ledger is not None and ledger.complete is False
        assert ledger.unreadable == {app}
        assert any(path in p for p in ledger.problems)
        assert not any("CANARY" in p for p in ledger.problems)

    def test_an_empty_list_is_a_complete_read_and_the_empty_row_is_still_removed(self, requests_ctx: EngineContext):
        report = _run_with_arrs(requests_ctx, {})

        assert requests_ctx.request_ledger is not None and requests_ctx.request_ledger.complete is True
        requests_ctx.plex.delete_owned_collection.assert_called_once()
        assert requests_ctx.plex.delete_owned_collection.call_args.args[0] is EXISTING_ROW
        assert report.removed_deliveries == [{"row_slug": "asked", "library_key": "1"}]
        assert report.diff.deleted == ["Movies you asked for"]


SEERR_AT = SeerrTarget(url="http://overseerr.test", api_key="SEERR-KEY-CANARY")
SEERR_BASE = f"{SEERR_AT.url}/api/v1"


def _users_page() -> dict:
    """The Overseerr accounts behind the recorded request page, as a `/user` page."""
    users = list({r["requestedBy"]["id"]: r["requestedBy"] for r in REQS["results"]}.values())
    return {"pageInfo": {"pages": 1, "results": len(users)}, "results": users}


def _collect_from_seerr(answers: dict[str, httpx.Response]) -> RequestLedger:
    plex_ids = {r["requestedBy"]["plexId"] for r in REQS["results"]}
    people = [UserProfile(username=f"p{i}", plex_account_id=i, user_type=UserType.SHARED) for i in sorted(plex_ids)]
    with respx.mock(assert_all_called=False) as seerr:
        seerr.get(f"{SEERR_BASE}/request").mock(return_value=answers.get("/request", httpx.Response(200, json=REQS)))
        seerr.get(f"{SEERR_BASE}/user").mock(return_value=answers.get("/user", httpx.Response(200, json=_users_page())))
        seerr.get(url__startswith=f"{SEERR_BASE}/settings/").mock(return_value=httpx.Response(200, json=[]))
        return collect_requests(RequestSources(overseerr=SEERR_AT), people)


class TestAMalformedOverseerrPageFailsTheRead:
    """`/request` and `/user` are the Overseerr reads that can take a row down; a 200 that is not a
    `{results: [objects]}` page is a failed read of either, never "nobody asked for anything"."""

    @pytest.mark.parametrize("path", ["/request", "/user"])
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b'{"message": "CANARY"}', id="object-without-results"),
            pytest.param(b"null", id="null"),
            pytest.param(b'"CANARY"', id="string"),
            pytest.param(b"[]", id="bare-list"),
            pytest.param(b'{"pageInfo": {"results": 1}, "results": ["CANARY"]}', id="results-holding-a-string"),
        ],
    )
    def test_the_ledger_is_incomplete(self, path, body):
        ledger = _collect_from_seerr({path: httpx.Response(200, content=body)})

        assert ledger.complete is False
        assert ledger.unreadable == {"Overseerr"}
        assert any(path in p for p in ledger.problems)
        assert not any("CANARY" in p for p in ledger.problems)

    def test_a_genuinely_empty_request_page_is_a_complete_read(self):
        empty = {"pageInfo": {"pages": 0, "results": 0}, "results": []}
        ledger = _collect_from_seerr({"/request": httpx.Response(200, json=empty)})

        assert ledger.complete is True and ledger.titles == []

    def test_the_recorded_pages_still_read_in_full(self):
        ledger = _collect_from_seerr({})

        assert ledger.complete is True and len(ledger.titles) == 7
