"""The history mix (#152): its three settings, the row and person fields, and the on-demand mix endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


class TestSettings:
    @pytest.mark.parametrize("key", ["recommendations.favourite_count", "recommendations.older_count"])
    def test_a_count_is_zero_to_ten(self, client: TestClient, key):
        for ok in (0, 5, 10):
            assert client.put("/api/settings", json={"values": {key: ok}}).status_code == 200
        for bad in (-1, 11, "x"):
            assert client.put("/api/settings", json={"values": {key: bad}}).status_code == 422

    def test_the_look_back_is_any_time_or_one_three_or_five_years(self, client: TestClient):
        key = "recommendations.older_lookback_years"
        for ok in (0, 1, 3, 5):
            assert client.put("/api/settings", json={"values": {key: ok}}).status_code == 200
        for bad in (2, 4, 10, -1, True, "3"):
            assert client.put("/api/settings", json={"values": {key: bad}}).status_code == 422

    def test_everything_defaults_to_zero(self, client: TestClient):
        values = client.get("/api/settings").json()
        assert [values[f"recommendations.{k}"] for k in ("favourite_count", "older_count", "older_lookback_years")] == [
            0,
            0,
            0,
        ]


class TestRowFields:
    def test_a_row_round_trips_both_counts_and_none_inherits(self, client: TestClient):
        created = client.post("/api/collections", json={"name": "Mix", "size": 10, "favourite_count": 3})
        assert created.status_code == 201, created.text
        row = created.json()
        assert (row["favourite_count"], row["older_count"]) == (3, None)
        patched = client.patch(f"/api/collections/{row['id']}", json={"name": "Mix", "older_count": 0})
        assert patched.status_code == 200, patched.text
        assert (patched.json()["favourite_count"], patched.json()["older_count"]) == (3, 0)
        cleared = client.patch(
            f"/api/collections/{row['id']}", json={"name": "Mix", "favourite_count": None, "older_count": None}
        )
        assert (cleared.json()["favourite_count"], cleared.json()["older_count"]) == (None, None)

    @pytest.mark.parametrize("field", ["favourite_count", "older_count"])
    def test_a_row_count_is_bounded(self, client: TestClient, field):
        assert client.post("/api/collections", json={"name": "Mix", field: 11}).status_code == 422
        assert client.post("/api/collections", json={"name": "Mix", field: -1}).status_code == 422


class TestPersonOverride:
    def _ids(self, client: TestClient) -> tuple[int, int]:
        uid = next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")
        return uid, client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]

    def test_the_rows_list_shows_effective_counts_and_the_override(self, client: TestClient):
        client.put("/api/settings", json={"values": {"recommendations.favourite_count": 4}})
        uid, cid = self._ids(client)
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert (row["favourite_count"], row["older_count"]) == (4, 0)
        assert (row["override"]["favourite_count"], row["override"]["older_count"]) == (None, None)

        saved = client.put(f"/api/users/{uid}/rows/{cid}", json={"older_count": 6, "favourite_count": 0})
        assert saved.status_code == 200, saved.text
        assert (saved.json()["favourite_count"], saved.json()["older_count"]) == (0, 6)
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert (row["override"]["favourite_count"], row["override"]["older_count"]) == (0, 6)

        client.put(f"/api/users/{uid}/rows/{cid}", json={"favourite_count": None, "older_count": None})
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert (row["override"]["favourite_count"], row["override"]["older_count"]) == (None, None)

    def test_an_override_count_is_bounded(self, client: TestClient):
        uid, cid = self._ids(client)
        assert client.put(f"/api/users/{uid}/rows/{cid}", json={"older_count": 11}).status_code == 422


def _watch(title, days_ago, tmdb_id, *, plays=1, media=MediaType.MOVIE) -> WatchedItem:
    return WatchedItem(
        title=title, media_type=media, watched_at=NOW - timedelta(days=days_ago), tmdb_id=tmdb_id, watch_count=plays
    )


class TestHistoryMixEndpoint:
    @pytest.fixture
    def history(self, client: TestClient, monkeypatch):
        items = [_watch(f"Recent {n}", n, 100 + n) for n in range(12)]
        items += [_watch("Favourite", 200, 300, plays=4), _watch("Blocked", 210, 301, plays=4)]
        items += [_watch(f"Old {n}", 400 + n, 400 + n) for n in range(20)]

        def profile_for(session, user_id):
            return UserProfile(
                username="sarah", plex_account_id=1, user_type=UserType.SHARED, history=items, blocked_seeds={301}
            )

        monkeypatch.setattr(client.app.state.run_service, "profile_with_history", profile_for)

    def _ids(self, client: TestClient) -> tuple[int, int]:
        uid = next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")
        return uid, client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]

    def _web(self, client: TestClient) -> None:
        client.put("/api/settings", json={"values": {"candidates.sources": ["tmdb_similar", "llm_web"]}})

    def test_it_lists_the_three_groups_with_the_rows_counts(self, client: TestClient, history):
        self._web(client)
        client.put("/api/settings", json={"values": {"recommendations.favourite_count": 2}})
        client.put("/api/settings", json={"values": {"recommendations.older_count": 3}})
        uid, cid = self._ids(client)
        response = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": cid})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["favourite_count"], body["older_count"]) == (2, 3)
        assert [t["title"] for t in body["recent"]][:2] == ["Recent 0", "Recent 1"]
        assert [t["title"] for t in body["favourites"]] == ["Favourite"]
        assert len(body["older"]) == 3 and all(t["title"].startswith("Old ") for t in body["older"])
        assert set(body["older"][0]) == {"title", "year", "media_type", "tmdb_id"}
        everything = [t["title"] for group in ("recent", "favourites", "older") for t in body[group]]
        assert "Blocked" not in everything and len(everything) == len(set(everything))

    def test_a_persons_override_beats_the_server(self, client: TestClient, history):
        self._web(client)
        client.put("/api/settings", json={"values": {"recommendations.older_count": 3}})
        uid, cid = self._ids(client)
        client.put(f"/api/users/{uid}/rows/{cid}", json={"older_count": 0})
        body = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": cid}).json()
        assert body["older_count"] == 0 and body["older"] == []

    def test_an_unknown_person_or_row_is_404(self, client: TestClient, history):
        uid, cid = self._ids(client)
        assert client.get("/api/users/9999/history-mix", params={"collection_id": cid}).status_code == 404
        assert client.get(f"/api/users/{uid}/history-mix", params={"collection_id": 9999}).status_code == 404

    def test_a_plex_error_is_502_with_the_secret_redacted(self, client: TestClient, monkeypatch):
        def boom(session, user_id):
            raise RuntimeError("GET http://pms:32400/x?X-Plex-Token=SECRET123 failed")

        monkeypatch.setattr(client.app.state.run_service, "profile_with_history", boom)
        uid, cid = self._ids(client)
        response = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": cid})
        assert response.status_code == 502
        assert "SECRET123" not in response.text

    def test_the_mix_needs_the_owner(self, client: TestClient):
        client.cookies.clear()
        assert client.get("/api/users/1/history-mix", params={"collection_id": 1}).status_code in (401, 403)

    def test_it_matches_what_a_run_searches(self, client: TestClient, history):
        from shortlist.engine.history import derive_seeds, ratings_policy
        from shortlist.engine.taste import history_mix

        self._web(client)
        client.put("/api/settings", json={"values": {"recommendations.favourite_count": 2}})
        client.put("/api/settings", json={"values": {"recommendations.older_count": 4}})
        client.put("/api/settings", json={"values": {"recommendations.recent_count": 5}})
        uid, cid = self._ids(client)
        body = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": cid}).json()
        profile = client.app.state.run_service.profile_with_history(None, uid)
        ratings = ratings_policy(profile.history, None)
        max_seeds = client.get("/api/settings").json()["recommendations.max_seeds"]
        searched = derive_seeds(
            profile.history, lambda w: w.tmdb_id, max_seeds=max_seeds, blocked=profile.blocked_seeds
        )[:5]
        mix = history_mix(
            profile.history,
            blocked=profile.blocked_seeds,
            ratings=ratings,
            resolve=lambda w: w.tmdb_id,
            favourites=2,
            older=4,
            now=NOW,
            searched=frozenset((s.tmdb_id, s.media_type) for s in searched),
        )
        assert [t["tmdb_id"] for t in body["recent"]] == [s.tmdb_id for s in searched]
        assert [t["tmdb_id"] for t in body["favourites"]] == [s.tmdb_id for s in mix.favourite_seeds[:2]]
        assert [t["tmdb_id"] for t in body["older"]] == [s.tmdb_id for s in mix.older_seeds[:4]]
        assert not {t["tmdb_id"] for t in body["older"]} & {s.tmdb_id for s in searched}

    def _no_widening(self, client: TestClient, **row) -> dict:
        client.put("/api/settings", json={"values": {"recommendations.favourite_count": 2}})
        client.put("/api/settings", json={"values": {"recommendations.older_count": 3}})
        uid, _ = self._ids(client)
        created = client.post("/api/collections", json={"name": "Other", **row})
        assert created.status_code == 201, created.text
        response = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": created.json()["id"]})
        assert response.status_code == 200, response.text
        return response.json()

    def _assert_flat(self, body: dict) -> None:
        assert (body["favourite_count"], body["older_count"]) == (0, 0)
        assert body["favourites"] == [] and body["older"] == []

    def test_a_row_without_web_search_does_not_widen(self, client: TestClient, history):
        self._assert_flat(self._no_widening(client, candidate_sources=["tmdb_similar"]))

    def test_a_single_seed_row_does_not_widen(self, client: TestClient, history):
        self._assert_flat(self._no_widening(client, candidate_sources=["llm_web"], max_seeds=1))

    def test_a_shared_row_does_not_widen(self, client: TestClient, history):
        self._assert_flat(self._no_widening(client, candidate_sources=["llm_web"], build="shared"))

    def test_an_internal_lookup_error_is_502_not_404(self, client: TestClient, monkeypatch):
        def boom(session, user_id):
            raise KeyError("secret-internal-key")

        monkeypatch.setattr(client.app.state.run_service, "profile_with_history", boom)
        uid, cid = self._ids(client)
        response = client.get(f"/api/users/{uid}/history-mix", params={"collection_id": cid})
        assert response.status_code == 502
