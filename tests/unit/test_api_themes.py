"""`/api/themes` and the AI-row fields on `/api/collections` (#138): preview, save, pause, capability.

The AI call, TMDB and Plex are faked at the module boundary; nothing touches the network.
"""

# ruff: noqa: F811 -- a test requests the imported `client` fixture by name, which reads as a redefinition
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.placeholders import refusal
from shortlist.engine.themes import ThemePick, ThemeSpec, theme_content_hash
from shortlist.server.api import themes as themes_api
from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Collection, Event, Theme
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.services.sse import EventBus
from shortlist.server.services.theme_author import (
    BUILD_SYSTEM_GUIDANCE,
    BUILD_SYSTEM_MECHANICS,
    ThemeAuthorError,
    ThemeDraft,
    ThemeStats,
)
from shortlist.server.services.theme_store import spec_from_row
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import client  # noqa: F401  (the shared app + owner-session fixture)

SECRET = "sk-never-in-an-event"


def _spec(**overrides) -> ThemeSpec:
    base = dict(
        slug="twist-endings",
        name="Twist endings",
        emoji="🌀",
        media=(MediaType.MOVIE,),
        tags=(111,),
        genres=("thriller",),
        excluded_genres=(),
        collections=(),
        picks=(
            ThemePick(tmdb_id=1, media=MediaType.MOVIE, origin="ai", reason="The twist"),
            ThemePick(tmdb_id=2, media=MediaType.MOVIE, origin="ai", reason=None),
        ),
        rules=RowLimits(max_runtime=140),
        min_votes=None,
    )
    base.update(overrides)
    return ThemeSpec(**base)


def _draft(spec: ThemeSpec | None = None, tokens: int = 321) -> ThemeDraft:
    spec = spec or _spec()
    return ThemeDraft(
        spec=spec,
        brief="films with a twist",
        stats=ThemeStats(named=60, resolved=2, in_library=2, after_rules=2, unwatched_median=None),
        tokens=tokens,
        ai_reasons={(MediaType.MOVIE, 1): "The twist"},
        titles={(MediaType.MOVIE, 1): "Se7en", (MediaType.MOVIE, 2): "The Prestige"},
    )


class _Author:
    """Records what the API hands `author_theme`, and answers a canned draft or raises."""

    def __init__(self, draft: ThemeDraft | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.draft = draft or _draft()
        self.error = error

    def __call__(self, **kwargs) -> ThemeDraft:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.draft


@pytest.fixture
def author(client: TestClient, monkeypatch) -> _Author:
    """A configured AI provider, TMDB and Plex, with `author_theme` replaced by a recorder."""
    recorder = _Author()
    service = client.app.state.run_service
    monkeypatch.setattr(service, "build_tmdb_only", lambda: SimpleNamespace(search_keywords=lambda q, limit=10: []))
    monkeypatch.setattr(service, "build_plex_reader", lambda: object())
    monkeypatch.setattr(themes_api, "library_index", lambda plex, sessions, **kw: {})
    monkeypatch.setattr(themes_api, "make_curator", lambda provider, **kw: SimpleNamespace(name=provider))
    monkeypatch.setattr(themes_api, "author_theme", recorder)
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("curator.provider", "anthropic")
        store.set("curator.api_key", SECRET)
        session.commit()
    return recorder


def _body(**overrides) -> dict:
    draft = {
        "name": "Twist endings",
        "emoji": "🌀",
        "brief": "films with a twist",
        "origin": "ai",
        "media": ["movie"],
        "tags": [{"id": 111, "name": "twist ending"}],
        "genres": ["thriller"],
        "picks": [
            {"tmdb_id": 1, "media": "movie", "origin": "ai", "reason": "The twist", "title": "Se7en", "year": 1995},
            {"tmdb_id": 2, "media": "movie", "origin": "owner", "reason": None, "title": "The Prestige", "year": 2006},
        ],
        "rules": {"max_runtime": 140},
    }
    draft.update(overrides)
    return draft


def _save(client: TestClient, tokens: int = 0, collection_id: int | None = None, **draft) -> dict:
    r = client.post("/api/themes", json={"draft": _body(**draft), "tokens": tokens, "collection_id": collection_id})
    assert r.status_code == 201, r.text
    return r.json()


def _ai_row(client: TestClient, theme_id: int, name: str = "Twist endings", **extra) -> dict:
    r = client.post("/api/collections", json={"name": name, "theme_id": theme_id, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _themes(client: TestClient) -> list[Theme]:
    with client.app.state.sessions() as session:
        rows = session.query(Theme).all()
        session.expunge_all()
        return rows


class TestPreview:
    def test_returns_a_draft_and_persists_nothing(self, client: TestClient, author: _Author):
        r = client.post("/api/themes/preview", json={"brief": "films with a twist", "media": "movie"})

        assert r.status_code == 200, r.text
        out = r.json()
        assert out["draft"]["name"] == "Twist endings" and out["draft"]["id"] is None
        assert out["draft"]["tags"] == [{"id": 111, "name": "111"}], "a tag the AI call named but TMDB never gave"
        assert [p["title"] for p in out["draft"]["picks"]] == ["Se7en", "The Prestige"]
        assert out["tokens"] == 321 and out["diff"] is None
        assert out["stats"]["resolved"] == 2
        assert _themes(client) == []
        with client.app.state.sessions() as session:
            assert session.query(Event).filter(Event.scope == "theme.build").count() == 0
        call = author.calls[0]
        assert call["brief"] == "films with a twist"
        assert call["media"] == (MediaType.MOVIE,)
        assert call["current"] is None and tuple(call["current_tag_names"]) == ()
        assert call["profile"] is None

    def test_a_refinement_sends_the_stored_theme_and_its_tag_names_and_diffs_by_title(
        self, client: TestClient, author: _Author
    ):
        saved = _save(client)
        author.draft = _draft(
            _spec(picks=(ThemePick(tmdb_id=2, media=MediaType.MOVIE, origin="ai", reason=None),)),
        )

        r = client.post(
            "/api/themes/preview",
            json={"brief": "darker", "media": "movie", "current_theme_id": saved["id"]},
        )

        assert r.status_code == 200, r.text
        call = author.calls[0]
        assert call["current"].slug == saved["slug"]
        assert [p.tmdb_id for p in call["current"].picks] == [1, 2]
        assert list(call["current_tag_names"]) == ["twist ending"]
        diff = r.json()["diff"]
        assert diff["removed"] == ["Se7en"] and diff["unchanged"] == ["The Prestige"] and diff["added"] == []

    def test_media_both_asks_for_both_kinds(self, client: TestClient, author: _Author):
        client.post("/api/themes/preview", json={"brief": "x", "media": "both"})

        assert author.calls[0]["media"] == (MediaType.MOVIE, MediaType.SHOW)

    def test_a_paused_row_answers_409_in_plain_words_and_never_calls_the_ai(self, client: TestClient, author: _Author):
        row = _ai_row(client, _save(client)["id"])
        assert client.post(f"/api/collections/{row['id']}/ai-pause", json={"paused": True}).status_code == 200

        r = client.post("/api/themes/preview", json={"brief": "x", "media": "movie", "collection_id": row["id"]})

        assert r.status_code == 409
        assert "paused" in r.json()["detail"].lower()
        assert author.calls == []

    def test_no_provider_answers_422_and_never_calls_the_ai(self, client: TestClient, author: _Author):
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("curator.provider", "none")
            session.commit()

        r = client.post("/api/themes/preview", json={"brief": "x", "media": "movie"})

        assert r.status_code == 422
        assert "AI provider" in r.json()["detail"]
        assert author.calls == []

    def test_an_authoring_failure_is_422_with_its_plain_message_and_no_secret(
        self, client: TestClient, author: _Author
    ):
        author.error = ThemeAuthorError("The AI did not answer. Try again in a moment.")

        r = client.post("/api/themes/preview", json={"brief": "x", "media": "movie"})

        assert (r.status_code, r.json()["detail"]) == (422, "The AI did not answer. Try again in a moment.")
        assert SECRET not in r.text


class TestSave:
    def test_the_hash_is_recomputed_from_content_never_taken_from_the_client(self, client: TestClient):
        r = client.post("/api/themes", json={"draft": {**_body(), "content_hash": "forged"}, "tokens": 0})

        assert r.status_code == 201, r.text
        row = _themes(client)[0]
        assert row.content_hash == theme_content_hash(spec_from_row(row)) != "forged"
        assert r.json()["content_hash"] == row.content_hash

    def test_an_edit_that_changes_contents_changes_the_hash_and_a_rename_does_not(self, client: TestClient):
        saved = _save(client)

        def put(**draft):
            r = client.put(f"/api/themes/{saved['id']}", json={"draft": _body(**draft), "tokens": 0})
            assert r.status_code == 200, r.text
            return r.json()["content_hash"]

        assert put(name="Renamed", emoji="🎭") == saved["content_hash"]
        fewer = put(picks=_body()["picks"][:1])
        assert fewer != saved["content_hash"]
        assert put(picks=_body()["picks"][:1], rules={"max_runtime": 100}) != fewer

    def test_slugs_are_deduplicated_with_a_numeric_suffix(self, client: TestClient):
        slugs = [_save(client)["slug"] for _ in range(3)]

        assert slugs == ["twist-endings", "twist-endings-2", "twist-endings-3"]

    def test_get_returns_what_was_saved(self, client: TestClient):
        saved = _save(client)

        got = client.get(f"/api/themes/{saved['id']}").json()

        assert got["picks"][1]["origin"] == "owner" and got["tags"] == [{"id": 111, "name": "twist ending"}]
        assert client.get("/api/themes/9999").status_code == 404

    def test_a_theme_that_selects_nothing_is_refused(self, client: TestClient):
        r = client.post("/api/themes", json={"draft": _body(tags=[], genres=[], picks=[]), "tokens": 0})

        assert r.status_code == 422

    def test_an_unknown_genre_is_refused(self, client: TestClient):
        r = client.post("/api/themes", json={"draft": _body(genres=["thrillerz"]), "tokens": 0})

        assert r.status_code == 422 and "thrillerz" in r.json()["detail"]

    def test_tokens_accumulate_on_the_theme_and_the_row_and_an_event_records_them_without_keys(
        self, client: TestClient
    ):
        row = _ai_row(client, _save(client)["id"])
        saved = _save(client, tokens=300, collection_id=row["id"], name="Second")
        again = client.put(
            f"/api/themes/{saved['id']}",
            json={"draft": _body(name="Second"), "tokens": 200, "collection_id": row["id"]},
        )
        assert again.status_code == 200, again.text

        assert [t.ai_tokens for t in _themes(client) if t.id == saved["id"]] == [500]
        with client.app.state.sessions() as session:
            assert session.get(Collection, row["id"]).ai_tokens == 500
            events = session.query(Event).filter(Event.scope == "theme.build").order_by(Event.id).all()
        assert [e.message["tokens"] for e in events if e.message["theme"] == saved["slug"]] == [300, 200]
        edit = events[-1].message
        assert edit["diff"]["added"] == [] and edit["diff"]["removed"] == [] and edit["diff"]["unchanged"]
        assert SECRET not in str([e.message for e in events])

    def test_a_save_that_spends_tokens_on_a_paused_row_is_409(self, client: TestClient):
        row = _ai_row(client, _save(client)["id"])
        client.post(f"/api/collections/{row['id']}/ai-pause", json={"paused": True})

        spent = client.post("/api/themes", json={"draft": _body(name="B"), "tokens": 50, "collection_id": row["id"]})
        by_hand = client.post("/api/themes", json={"draft": _body(name="C"), "tokens": 0, "collection_id": row["id"]})

        assert spent.status_code == 409 and by_hand.status_code == 201


class TestGuidanceAndPrompts:
    def test_the_owners_guidance_reaches_the_authoring_call(self, client: TestClient, author: _Author):
        r = client.post(
            "/api/themes/preview", json={"brief": "films with a twist", "guidance": "Favour films before 2000."}
        )

        assert r.status_code == 200, r.text
        assert author.calls[0]["guidance"] == "Favour films before 2000."

    def test_no_guidance_means_the_built_in_wording(self, client: TestClient, author: _Author):
        client.post("/api/themes/preview", json={"brief": "films with a twist"})

        assert author.calls[0]["guidance"] == ""

    def test_the_prompts_endpoint_returns_the_default_guidance_and_the_locked_mechanics(self, client: TestClient):
        r = client.get("/api/themes/prompts")

        assert r.status_code == 200, r.text
        assert r.json() == {
            "guidance": BUILD_SYSTEM_GUIDANCE.strip(),
            "mechanics": BUILD_SYSTEM_MECHANICS,
        }


class TestCapabilities:
    def test_ai_is_false_without_a_provider_and_true_with_one(self, client: TestClient, author: _Author):
        assert client.get("/api/themes/capabilities").json() == {"ai": True}
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("curator.provider", "none")
            session.commit()

        assert client.get("/api/themes/capabilities").json() == {"ai": False}


class TestOwnerOnly:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("get", "/api/themes/capabilities", None),
            ("get", "/api/themes/1", None),
            ("post", "/api/themes/preview", {"brief": "x", "media": "movie"}),
            ("post", "/api/themes", {"draft": _body(), "tokens": 0}),
            ("put", "/api/themes/1", {"draft": _body(), "tokens": 0}),
            ("post", "/api/collections/1/ai-pause", {"paused": True}),
        ],
    )
    def test_someone_who_is_not_the_owner_gets_403(self, client: TestClient, method: str, path: str, body):
        stranger = session_serializer(client.app.state.session_secret).dumps({"account_id": 42, "username": "x"})
        client.cookies.set(SESSION_COOKIE, stranger)

        r = getattr(client, method)(path, **({"json": body} if body is not None else {}))

        assert r.status_code == 403


class TestAiRows:
    def test_a_new_ai_row_is_created_disabled_whatever_was_asked(self, client: TestClient):
        theme = _save(client)

        row = _ai_row(client, theme["id"], enabled=True)

        assert (row["theme_id"], row["enabled"], row["ai_paused"], row["ai_tokens"]) == (theme["id"], False, False, 0)

    def test_a_row_without_a_theme_keeps_the_enabled_it_was_given(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Plain", "enabled": True})

        assert r.json()["enabled"] is True and r.json()["theme_id"] is None

    def test_a_theme_that_does_not_exist_is_refused(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Ghost", "theme_id": 999})

        assert r.status_code == 422

    def test_a_shared_ai_row_is_refused(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "S", "theme_id": _save(client)["id"], "build": "shared"})

        assert r.status_code == 422 and "per person" in r.json()["detail"]

    def test_an_ai_row_with_a_season_is_refused(self, client: TestClient):
        r = client.post(
            "/api/collections", json={"name": "S", "theme_id": _save(client)["id"], "seasons": ["halloween"]}
        )

        assert r.status_code == 422 and "season" in r.json()["detail"].lower()

    def test_the_theme_placeholder_is_refused_on_a_row_with_no_theme(self, client: TestClient):
        r = client.post("/api/collections", json={"name": "Picks", "name_template": "{theme_emoji} {theme} picks"})

        assert r.status_code == 422 and "{theme}" in r.json()["detail"]

    def test_the_theme_placeholder_fills_on_an_ai_row(self, client: TestClient):
        row = _ai_row(client, _save(client)["id"], name="Row", name_template="{theme_emoji} {theme}")

        assert row["name_template"] == "{theme_emoji} {theme}"

    def test_a_row_title_that_would_clash_with_an_existing_row_is_refused_with_the_usual_message(
        self, client: TestClient
    ):
        client.post("/api/collections", json={"name": "Twist endings"})

        r = client.post(
            "/api/collections", json={"name": "Twist", "name_template": "{theme}", "theme_id": _save(client)["id"]}
        )

        assert r.status_code == 422
        assert "is already the title of the row" in r.json()["detail"]

    def test_patching_a_theme_onto_a_shared_row_is_refused(self, client: TestClient):
        shared = client.post("/api/collections", json={"name": "Shared", "build": "shared"}).json()

        r = client.patch(f"/api/collections/{shared['id']}", json={"name": "Shared", "theme_id": _save(client)["id"]})

        assert r.status_code == 422

    def test_pause_toggles_and_a_plain_row_cannot_be_paused(self, client: TestClient):
        row = _ai_row(client, _save(client)["id"])
        plain = client.post("/api/collections", json={"name": "Plain"}).json()

        paused = client.post(f"/api/collections/{row['id']}/ai-pause", json={"paused": True})
        listed = {c["id"]: c for c in client.get("/api/collections").json()}
        resumed = client.post(f"/api/collections/{row['id']}/ai-pause", json={"paused": False})

        assert paused.json()["ai_paused"] is True and listed[row["id"]]["ai_paused"] is True
        assert resumed.json()["ai_paused"] is False
        assert client.post(f"/api/collections/{plain['id']}/ai-pause", json={"paused": True}).status_code == 422
        assert client.post("/api/collections/9999/ai-pause", json={"paused": True}).status_code == 404

    def test_the_run_gets_the_theme_on_the_rows_spec_with_real_section_keys(self, client: TestClient):
        theme = _save(
            client,
            collections=[{"section_key": "7", "section_title": "Movies", "title": "Twisty"}],
        )
        row = _ai_row(client, theme["id"])
        enabled = client.patch(f"/api/collections/{row['id']}", json={"name": row["name"], "enabled": True})
        assert enabled.status_code == 200, enabled.text
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())

        with client.app.state.sessions() as session:
            specs = builder._build_rows(
                session, SettingsStore(session, client.app.state.secrets), catalogue=load_catalogue(session)
            )

        spec = next(s for s in specs if s.slug == row["slug"])
        assert spec.theme is not None
        assert [(c.section_key, c.title) for c in spec.theme.collections] == [("7", "Twisty")]
        assert spec.theme.slug == "twist-endings" and spec.theme.rules.max_runtime == 140
        assert [(p.tmdb_id, p.origin) for p in spec.theme.picks] == [(1, "ai"), (2, "owner")]
        assert next(s for s in specs if s.slug == "picked").theme is None


class TestTokensReachTheRowThatFollowsTheTheme:
    """A new row's list is saved before the row exists, so its tokens are charged to the row when it is made."""

    def _usage(self, client: TestClient, row_id: int) -> int:
        return next(r for r in client.get("/api/collections").json() if r["id"] == row_id)["ai_tokens"]

    def test_a_new_row_starts_with_what_its_theme_cost(self, client: TestClient):
        theme = _save(client, tokens=321)

        row = _ai_row(client, theme["id"])

        assert row["ai_tokens"] == 321
        assert self._usage(client, row["id"]) == 321

    def test_a_second_row_on_the_same_theme_is_not_charged_again(self, client: TestClient):
        theme = _save(client, tokens=321)
        _ai_row(client, theme["id"])

        second = _ai_row(client, theme["id"], name="Twist endings again")

        assert second["ai_tokens"] == 0

    def test_a_row_moved_onto_a_theme_no_row_follows_is_charged_for_it_once(self, client: TestClient):
        first = _ai_row(client, _save(client)["id"])
        other = _save(client, tokens=250, name="Heists")

        moved = client.patch(f"/api/collections/{first['id']}", json={"name": first["name"], "theme_id": other["id"]})

        assert moved.status_code == 200, moved.text
        assert moved.json()["ai_tokens"] == 250
        again = client.patch(f"/api/collections/{first['id']}", json={"name": first["name"], "theme_id": other["id"]})
        assert again.json()["ai_tokens"] == 250

    def test_a_theme_saved_for_a_row_is_not_counted_twice_when_the_row_is_made_after(self, client: TestClient):
        row = _ai_row(client, _save(client)["id"])
        _save(client, tokens=300, collection_id=row["id"], name="Second")

        assert self._usage(client, row["id"]) == 300


class TestTryItOnASwitchedOffRow:
    """A new AI row is made disabled, so its "Try it" has to reach it: a DRY run that names it builds it."""

    def _config(self, client: TestClient, row_id: int, *, dry_run: bool, collection_ids: list[int] | None):
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            return builder._engine_config(
                session,
                SettingsStore(session, client.app.state.secrets),
                dry_run=dry_run,
                collection_ids=collection_ids,
            )

    def _disabled_row(self, client: TestClient) -> dict:
        row = _ai_row(client, _save(client)["id"])
        assert row["enabled"] is False
        return row

    def test_a_dry_run_that_names_the_row_builds_it_and_does_not_retire_it(self, client: TestClient):
        row = self._disabled_row(client)

        config = self._config(client, row["id"], dry_run=True, collection_ids=[row["id"]])

        assert row["slug"] in [spec.slug for spec in config.rows]
        assert config.build_only == frozenset({row["slug"]})
        assert row["slug"] not in [spec.slug for spec in config.retired_rows]
        assert config.dry_run is True

    def test_a_real_run_that_names_the_same_row_still_skips_it_and_retires_it(self, client: TestClient):
        row = self._disabled_row(client)

        config = self._config(client, row["id"], dry_run=False, collection_ids=[row["id"]])

        assert row["slug"] not in [spec.slug for spec in config.rows]
        assert config.build_only == frozenset()
        assert row["slug"] in [spec.slug for spec in config.retired_rows]

    def test_an_unscoped_dry_run_skips_a_switched_off_row(self, client: TestClient):
        row = self._disabled_row(client)

        config = self._config(client, row["id"], dry_run=True, collection_ids=None)

        assert row["slug"] not in [spec.slug for spec in config.rows]
        assert config.build_only is None
        assert row["slug"] in [spec.slug for spec in config.retired_rows]

    def test_a_dry_run_naming_another_row_leaves_this_one_out(self, client: TestClient):
        row = self._disabled_row(client)

        config = self._config(client, row["id"], dry_run=True, collection_ids=[999])

        assert row["slug"] not in [spec.slug for spec in config.rows]


class TestThemeTitleClashes:
    """The generic title check fills `{theme}` from each row's own theme, as it fills `{season}`."""

    def _themed(self, client: TestClient, name: str = "Twist endings") -> tuple[dict, dict]:
        theme = _save(client, name=name)
        return theme, _ai_row(client, theme["id"], name=f"Row {name}", name_template="{theme}")

    def test_a_plain_row_named_after_an_ai_rows_theme_is_refused_on_create(self, client: TestClient):
        self._themed(client)

        r = client.post("/api/collections", json={"name": "Twist endings"})

        assert r.status_code == 422 and "is already the title of the row" in r.json()["detail"]

    def test_a_plain_row_renamed_after_an_ai_rows_theme_is_refused(self, client: TestClient):
        self._themed(client)
        plain = client.post("/api/collections", json={"name": "Plain"}).json()

        r = client.patch(f"/api/collections/{plain['id']}", json={"name": "Twist endings"})

        assert r.status_code == 422 and "is already the title of the row" in r.json()["detail"]

    def test_two_ai_rows_on_one_theme_with_one_template_are_refused(self, client: TestClient):
        theme, _ = self._themed(client)

        r = client.post(
            "/api/collections", json={"name": "Second", "theme_id": theme["id"], "name_template": "{theme}"}
        )

        assert r.status_code == 422 and "is already the title of the row" in r.json()["detail"]

    def test_moving_an_ai_row_to_a_different_theme_is_not_a_clash(self, client: TestClient):
        self._themed(client)
        other = _save(client, name="Heist films")
        second = _ai_row(client, other["id"], name="Second", name_template="{theme} too")

        r = client.patch(
            f"/api/collections/{second['id']}",
            json={"name": "Second", "name_template": "{theme}", "theme_id": other["id"]},
        )

        assert r.status_code == 200, r.text

    def test_dropping_the_suffix_of_an_ai_rows_template_onto_a_plain_rows_title_is_refused(self, client: TestClient):
        theme = _save(client, name="Twist endings")
        ai = _ai_row(client, theme["id"], name="Row", name_template="{theme} too")
        client.post("/api/collections", json={"name": "Twist endings"})

        r = client.patch(f"/api/collections/{ai['id']}", json={"name": "Row", "name_template": "{theme}"})

        assert r.status_code == 422 and "is already the title of the row" in r.json()["detail"]

    def test_renaming_a_theme_onto_a_plain_rows_title_is_refused(self, client: TestClient):
        theme, _ = self._themed(client)
        client.post("/api/collections", json={"name": "Heist films"})

        r = client.put(f"/api/themes/{theme['id']}", json={"draft": _body(name="Heist films"), "tokens": 0})

        assert r.status_code == 422 and "is already the title of the row" in r.json()["detail"]


class TestThemePlaceholderRefusal:
    def test_refusal_knows_theme_the_way_it_knows_season(self):
        assert refusal("{theme} picks", "row_name") is not None
        assert refusal("{theme} picks", "row_name", row_has_theme=True) is None
        for field in ("fallback", "person_name", "global_name"):
            assert refusal("{theme_emoji}", field) is not None
