"""Explore and over-time controls on an AI row (#138): the fields, their validation, and the rotation routes."""

# ruff: noqa: F811 -- a test requests the imported `client` fixture by name, which reads as a redefinition
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import ThemeSpec
from shortlist.server.db.models import Collection, Event, Theme, ThemeHistory, User
from shortlist.server.services import theme_rotation
from shortlist.server.services.theme_author import ThemeAuthorError, ThemeDraft, ThemeStats
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import client  # noqa: F401  (the shared app + owner-session fixture)

CONTROLS = {
    "theme_mode": "explore",
    "explore_brief": "cosy nights in",
    "theme_days": 5,
    "refresh_share": 0.5,
    "repeat_cooldown_days": 30,
}


def make_theme(client: TestClient, name: str = "Twist endings") -> int:
    body = {
        "name": name,
        "media": ["movie"],
        "genres": ["thriller"],
        "origin": "ai",
        "picks": [{"tmdb_id": 1, "media": "movie", "origin": "ai", "title": "Se7en"}],
    }
    r = client.post("/api/themes", json={"draft": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ai_row(client: TestClient, theme_id: int | None, name: str = "Twist row", **extra) -> dict:
    r = client.post("/api/collections", json={"name": name, "theme_id": theme_id, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def plain_row(client: TestClient, name: str = "Plain row", **extra) -> dict:
    r = client.post("/api/collections", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def add_people(client: TestClient, *names: str) -> list[int]:
    with client.app.state.sessions() as session:
        users = [
            User(plex_account_id=1000 + i, username=n, slug=n, enabled=True, nickname=n.title())
            for i, n in enumerate(names)
        ]
        session.add_all(users)
        session.commit()
        return [u.id for u in users]


def history(client: TestClient, row_id: int, user_id: int, state: str, name: str, *, theme_id=None, days_ago=0):
    with client.app.state.sessions() as session:
        session.add(
            ThemeHistory(
                collection_id=row_id,
                user_id=user_id,
                theme_id=theme_id,
                theme_name=name,
                state=state,
                started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago),
            )
        )
        session.commit()


def patch(client: TestClient, row: dict, **fields):
    """PATCH sends only ``fields`` (plus the name every body needs), so the rest of the row must hold."""
    return client.patch(f"/api/collections/{row['id']}", json={"name": row["name"], **fields})


def get_row(client: TestClient, row_id: int) -> dict:
    return next(r for r in client.get("/api/collections").json() if r["id"] == row_id)


class TestDefaults:
    def test_a_new_row_carries_todays_behaviour(self, client: TestClient):
        row = ai_row(client, make_theme(client))

        assert (row["theme_mode"], row["explore_brief"]) == ("fixed", "")
        assert [row[k] for k in ("theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows")] == [None] * 4

    def test_an_ordinary_row_reports_the_same_defaults(self, client: TestClient):
        row = plain_row(client)

        assert (row["theme_mode"], row["avoid_rows"], row["refresh_share"]) == ("fixed", None, None)


class TestRoundTrip:
    def test_every_control_survives_create_get_and_patch(self, client: TestClient):
        plain_row(client, "Quiet")
        theme_id = make_theme(client)

        row = ai_row(client, theme_id, avoid_rows=["quiet"], **CONTROLS)

        shown = get_row(client, row["id"])
        for key, value in {**CONTROLS, "avoid_rows": ["quiet"]}.items():
            assert row[key] == shown[key] == value, key
        patched = patch(client, row, theme_days=9, refresh_share=1, avoid_rows=[])
        assert patched.status_code == 200, patched.text
        assert (patched.json()["theme_days"], patched.json()["refresh_share"], patched.json()["avoid_rows"]) == (
            9,
            1,
            None,
        )

    def test_a_patch_that_sends_one_control_leaves_the_others(self, client: TestClient):
        row = ai_row(client, make_theme(client), **CONTROLS)

        patched = patch(client, row, refresh_share=0.25).json()

        assert patched["refresh_share"] == 0.25
        assert {k: patched[k] for k in CONTROLS if k != "refresh_share"} == {
            k: v for k, v in CONTROLS.items() if k != "refresh_share"
        }

    def test_clearing_the_theme_resets_explore_and_the_controls(self, client: TestClient):
        plain_row(client, "Quiet")
        row = ai_row(client, make_theme(client), avoid_rows=["quiet"], **CONTROLS)

        patched = patch(client, row, theme_id=None)

        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert (body["theme_id"], body["theme_mode"], body["explore_brief"]) == (None, "fixed", "")
        assert [body[k] for k in ("theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows")] == [None] * 4


class TestValidation:
    @pytest.mark.parametrize(
        "extra",
        [{"theme_mode": "explore"}, {"refresh_share": 0.5}, {"repeat_cooldown_days": 7}, {"avoid_rows": ["picked"]}],
        ids=["explore", "share", "cooldown", "avoid"],
    )
    def test_a_row_with_no_theme_refuses_them(self, client: TestClient, extra):
        r = client.post("/api/collections", json={"name": "Plain", **extra})

        assert r.status_code == 422, r.text

    def test_a_patch_cannot_add_a_control_to_an_ordinary_row(self, client: TestClient):
        row = plain_row(client)

        r = patch(client, row, refresh_share=0.5)

        assert r.status_code == 422, r.text

    def test_explore_is_refused_on_a_shared_row(self, client: TestClient):
        r = client.post(
            "/api/collections",
            json={"name": "Shared", "build": "shared", "theme_id": make_theme(client), "theme_mode": "explore"},
        )

        assert r.status_code == 422, r.text

    @pytest.mark.parametrize("share", [0, 1.5, -0.1])
    def test_refresh_share_must_be_above_zero_and_at_most_one(self, client: TestClient, share):
        r = client.post("/api/collections", json={"name": "x", "theme_id": make_theme(client), "refresh_share": share})

        assert r.status_code == 422

    def test_avoid_rows_must_exist(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "x", "theme_id": make_theme(client), "avoid_rows": ["ghost"]})

        assert r.status_code == 422 and "ghost" in r.text

    def test_avoid_rows_cannot_name_the_row_itself(self, client: TestClient):
        row = ai_row(client, make_theme(client))

        r = patch(client, row, avoid_rows=[row["slug"]])

        assert r.status_code == 422 and row["slug"] in r.text

    def test_avoid_rows_cannot_name_a_shared_row(self, client: TestClient):
        shared = plain_row(client, "Together", build="shared")

        r = client.post(
            "/api/collections", json={"name": "x", "theme_id": make_theme(client), "avoid_rows": [shared["slug"]]}
        )

        assert r.status_code == 422 and shared["slug"] in r.text

    def test_duplicate_avoid_slugs_are_stored_once(self, client: TestClient):
        quiet = plain_row(client, "Quiet")

        row = ai_row(client, make_theme(client), avoid_rows=[quiet["slug"], quiet["slug"]])

        assert row["avoid_rows"] == [quiet["slug"]]


@pytest.fixture
def explore(client: TestClient) -> dict:
    """An explore AI row with two people in its audience."""
    ann, bob = add_people(client, "ann", "bob")
    row = ai_row(client, make_theme(client), theme_mode="explore", theme_days=7)
    return {"row": row, "ann": ann, "bob": bob}


class TestTheRotationView:
    def test_one_target_per_person_in_the_audience(self, client: TestClient, explore):
        row, ann, bob = explore["row"], explore["ann"], explore["bob"]
        cosy = make_theme(client, "Cosy")
        history(client, row["id"], ann, "current", "Cosy", theme_id=cosy, days_ago=2)
        history(client, row["id"], ann, "next", "Queued")
        history(client, row["id"], ann, "past", "Bleak", days_ago=9)

        r = client.get(f"/api/collections/{row['id']}/theme-rotation")

        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["mode"], body["days"]) == ("explore", 7)
        by_user = {t["user_id"]: t for t in body["targets"]}
        assert {ann, bob} <= set(by_user)
        assert by_user[ann]["current"]["name"] == "Cosy" and by_user[ann]["current"]["theme_id"] == cosy
        assert by_user[ann]["next"]["name"] == "Queued"
        assert [h["name"] for h in by_user[ann]["history"]] == ["Bleak"]
        assert by_user[ann]["name"] == "Ann"
        assert by_user[ann]["next_due_at"] is not None
        assert by_user[bob]["current"] is None and by_user[bob]["next"] is None and by_user[bob]["history"] == []

    def test_a_subset_row_lists_only_its_audience(self, client: TestClient):
        ann, _ = add_people(client, "ann", "bob")
        row = ai_row(client, make_theme(client), theme_mode="explore", audience="subset", audience_user_ids=[ann])

        body = client.get(f"/api/collections/{row['id']}/theme-rotation").json()

        assert [t["user_id"] for t in body["targets"]] == [ann]

    def test_a_row_that_is_not_an_ai_row_is_a_404(self, client: TestClient):
        row = plain_row(client)

        assert client.get(f"/api/collections/{row['id']}/theme-rotation").status_code == 404


class TestUpNext:
    def test_it_points_next_at_a_saved_theme_replacing_the_old_one(self, client: TestClient, explore):
        row, ann = explore["row"], explore["ann"]
        history(client, row["id"], ann, "next", "Old queue")
        fresh = make_theme(client, "Fresh")

        r = client.put(f"/api/collections/{row['id']}/up-next", json={"user_id": ann, "theme_id": fresh})

        assert r.status_code == 200, r.text
        assert r.json()["theme_id"] == fresh
        target = next(
            t
            for t in client.get(f"/api/collections/{row['id']}/theme-rotation").json()["targets"]
            if t["user_id"] == ann
        )
        assert target["next"]["name"] == "Fresh"
        with client.app.state.sessions() as session:
            assert session.query(ThemeHistory).filter_by(state="next", user_id=ann).count() == 1

    def test_a_missing_theme_is_a_404(self, client: TestClient, explore):
        r = client.put(
            f"/api/collections/{explore['row']['id']}/up-next", json={"user_id": explore["ann"], "theme_id": 9999}
        )

        assert r.status_code == 404

    def test_a_person_outside_the_audience_is_a_404(self, client: TestClient, explore):
        r = client.put(
            f"/api/collections/{explore['row']['id']}/up-next",
            json={"user_id": 9999, "theme_id": make_theme(client, "Fresh")},
        )

        assert r.status_code == 404

    def test_it_does_not_change_the_other_persons_rotation(self, client: TestClient, explore):
        row, ann, bob = explore["row"], explore["ann"], explore["bob"]
        history(client, row["id"], bob, "next", "Bob queue")

        client.put(
            f"/api/collections/{row['id']}/up-next", json={"user_id": ann, "theme_id": make_theme(client, "Fresh")}
        )

        with client.app.state.sessions() as session:
            assert [h.theme_name for h in session.query(ThemeHistory).filter_by(user_id=bob)] == ["Bob queue"]


class _Author:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, **kwargs) -> ThemeDraft:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        spec = ThemeSpec(
            slug="fresh-take",
            name="Fresh take",
            emoji=None,
            media=(MediaType.MOVIE,),
            tags=(),
            genres=("Thriller",),
            excluded_genres=(),
            collections=(),
            picks=(),
            rules=RowLimits(),
            min_votes=None,
        )
        return ThemeDraft(
            spec=spec,
            brief=kwargs["brief"],
            stats=ThemeStats(named=1, resolved=1, in_library=1, after_rules=1, unwatched_median=None),
            tokens=88,
            ai_reasons={},
        )


@pytest.fixture
def author(client: TestClient, monkeypatch) -> _Author:
    recorder = _Author()
    service = client.app.state.run_service
    monkeypatch.setattr(service, "build_tmdb_only", lambda: SimpleNamespace(search_keywords=lambda q, limit=10: []))
    monkeypatch.setattr(service, "build_plex_reader", lambda: object())
    monkeypatch.setattr(
        service, "profile_with_history", lambda session, user_id: SimpleNamespace(username=f"profile-{user_id}")
    )
    monkeypatch.setattr(theme_rotation, "library_index", lambda plex, sessions, **kw: {})
    monkeypatch.setattr(theme_rotation, "author_theme", recorder)
    monkeypatch.setattr("shortlist.engine.curator.make_curator", lambda provider, **kw: SimpleNamespace(name=provider))
    with client.app.state.sessions() as session:
        SettingsStore(session, client.app.state.secrets).set("curator.provider", "anthropic")
        session.commit()
    return recorder


class TestRegenerate:
    def test_it_authors_a_new_next_with_the_recent_names_in_the_brief(self, client: TestClient, explore, author):
        row, ann = explore["row"], explore["ann"]
        history(client, row["id"], ann, "current", "Cosy", days_ago=2)
        patch(client, row, explore_brief="cosy nights in")

        r = client.post(f"/api/collections/{row['id']}/up-next/regenerate", json={"user_id": ann})

        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Fresh take"
        call = author.calls[0]
        assert "cosy nights in" in call["brief"] and "Avoid these recent theme names: Cosy." in call["brief"]
        assert call["profile"].username == f"profile-{ann}"
        with client.app.state.sessions() as session:
            assert session.query(ThemeHistory).filter_by(user_id=ann, state="next").count() == 1
            assert session.get(Collection, row["id"]).ai_tokens == 88

    def test_a_paused_row_is_a_409_and_never_calls_the_ai(self, client: TestClient, explore, author):
        row = explore["row"]
        client.post(f"/api/collections/{row['id']}/ai-pause", json={"paused": True})

        r = client.post(f"/api/collections/{row['id']}/up-next/regenerate", json={"user_id": explore["ann"]})

        assert r.status_code == 409
        assert author.calls == []

    def test_no_provider_is_a_422(self, client: TestClient, explore, author):
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("curator.provider", "none")
            session.commit()

        r = client.post(f"/api/collections/{explore['row']['id']}/up-next/regenerate", json={"user_id": explore["ann"]})

        assert r.status_code == 422
        assert author.calls == []

    def test_a_fixed_row_is_a_422(self, client: TestClient, author):
        (ann,) = add_people(client, "ann")
        row = ai_row(client, make_theme(client))

        r = client.post(f"/api/collections/{row['id']}/up-next/regenerate", json={"user_id": ann})

        assert r.status_code == 422
        assert author.calls == []

    def test_an_author_failure_changes_nothing(self, client: TestClient, explore, author):
        author.error = ThemeAuthorError("The AI's answer was not a theme I could read.")
        row, ann = explore["row"], explore["ann"]
        history(client, row["id"], ann, "next", "Queued")

        r = client.post(f"/api/collections/{row['id']}/up-next/regenerate", json={"user_id": ann})

        assert r.status_code == 422
        with client.app.state.sessions() as session:
            assert [h.theme_name for h in session.query(ThemeHistory).filter_by(user_id=ann)] == ["Queued"]
            assert session.query(Theme).filter(Theme.name == "Fresh take").count() == 0
            assert session.query(Event).filter(Event.scope == "theme.build").count() == 1  # the setup theme only


class TestRegenerateErrorsArePlain:
    @pytest.mark.parametrize(("error", "status"), [(RuntimeError("no plex"), 502), (LookupError("gone"), 422)])
    def test_a_history_read_failure_is_a_plain_error_not_a_500(
        self, client, explore, author, monkeypatch, error, status
    ):
        def boom(session, user_id):
            raise error

        monkeypatch.setattr(client.app.state.run_service, "profile_with_history", boom)

        r = client.post(f"/api/collections/{explore['row']['id']}/up-next/regenerate", json={"user_id": explore["ann"]})

        assert r.status_code == status
        assert "no plex" not in r.text and "gone" not in r.text

    def test_an_unusable_theme_is_a_422(self, client, explore, author, monkeypatch):
        from dataclasses import replace

        good = author.__call__

        def backwards(**kw):
            draft = good(**kw)
            return replace(draft, spec=replace(draft.spec, rules=RowLimits(min_year=2020, max_year=1990)))

        monkeypatch.setattr(theme_rotation, "author_theme", backwards)

        r = client.post(f"/api/collections/{explore['row']['id']}/up-next/regenerate", json={"user_id": explore["ann"]})

        assert r.status_code == 422
        assert "earliest year" in r.text
