"""RunService.build_context branch matrix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import shortlist.server.services.context_builder as context_builder_mod
from shortlist.engine.history import FallbackHistorySource, PlexHistorySource
from shortlist.engine.models import MediaType
from shortlist.server.db.models import PickRow, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_history import StoreHistorySource
from shortlist.server.settings_store import SettingsStore


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def service(sessions, tmp_path):
    return RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))


@pytest.fixture
def configured(sessions, tmp_path, monkeypatch):
    """Configure plex+tmdb settings and stub the vendor client constructors (the boundary)."""
    box = SecretBox(tmp_path)
    with sessions() as session:
        store = SettingsStore(session, box)
        store.set("plex.url", "http://pms:32400")
        store.set("plex.token", "tok")
        store.set("tmdb.apikey", "k")
    plex_client = MagicMock()
    plex_client.machine_id = "m1"

    def _make_plex(url, token, timeout=20):
        plex_client.init_timeout = timeout  # so a test can assert the configured timeout flows through
        return plex_client

    monkeypatch.setattr(context_builder_mod, "PlexClient", _make_plex)
    monkeypatch.setattr(context_builder_mod, "PlexTvClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(context_builder_mod, "TmdbClient", lambda *a, **k: MagicMock())
    return box


class TestBuildContext:
    def test_unconfigured_raises_plainly(self, service):
        with pytest.raises(RuntimeError, match="not configured"):
            service.build_context(dry_run=True)

    def test_no_tautulli_uses_plex_history(self, service, configured):
        ctx = service.build_context(dry_run=True)
        # The engine reads through the local watch-history store, which wraps the real source.
        assert isinstance(ctx.history_source, StoreHistorySource)
        assert isinstance(ctx.history_source._upstream, PlexHistorySource)
        assert ctx.curator.name == "none"
        assert ctx.config.dry_run is True

    def test_the_progress_callback_carries_a_reason_without_polluting_the_counts(self, service, configured):
        """`counts` is a map of NUMBERS the UI renders as a "113 history · 40 seeds" tally, so a skip
        reason (a whole sentence) travels beside it, never inside it. This closure feeds BOTH the SSE
        stream and the replayable activity log, so it is where the contract has to hold."""
        entries: list[dict] = []
        ctx = service.build_context(dry_run=True, log_sink=entries.append)

        ctx.progress("sarah", "skipped", {}, "There are no per-person rows to build.")
        ctx.progress("sarah", "history", {"items": 12})

        assert entries[0]["reason"] == "There are no per-person rows to build."
        assert entries[0]["counts"] == {}, "the reason must not be smuggled into the counts tally"
        assert "reason" not in entries[1], "a stage that needs no explaining carries no reason"
        assert entries[1]["counts"] == {"items": 12}

    def test_tautulli_configured_uses_per_user_fallback(self, service, sessions, configured):
        with sessions() as session:
            store = SettingsStore(session, configured)
            store.set("tautulli.url", "http://taut:8181")
            store.set("tautulli.apikey", "tk")
        ctx = service.build_context(dry_run=False)
        assert isinstance(ctx.history_source, StoreHistorySource)
        assert isinstance(ctx.history_source._upstream, FallbackHistorySource)

    def test_plex_timeout_setting_flows_to_the_client(self, service, sessions, configured, monkeypatch):
        # A big TV library's collection rebuild legitimately takes 15-20s+; the run's PMS client must
        # get the configured per-call timeout (default 45s) so those don't time out and retry.
        captured: dict[str, int] = {}
        plex = MagicMock()
        plex.machine_id = "m1"

        def _make_plex(url, token, timeout=20):
            captured["timeout"] = timeout
            return plex

        monkeypatch.setattr(context_builder_mod, "PlexClient", _make_plex)
        service.build_context(dry_run=True)
        assert captured["timeout"] == 45  # default headroom
        with sessions() as session:
            SettingsStore(session, configured).set("plex.timeout_s", 90)
        service.build_context(dry_run=True)
        assert captured["timeout"] == 90  # an explicit setting overrides it

    def test_an_instance_still_stored_as_ollama_keeps_its_url(self):
        """Ollama was merged into the one local/OpenAI-compatible provider. An instance configured
        before that merge still has `ollama` and the OLD url key stored, and must keep working
        without the owner touching anything.

        Asserted on `curator_kwargs` rather than a built context because constructing the curator
        needs the `openai` extra — present in the shipped image, absent from a plain dev install."""
        from shortlist.server.services.context_builder import curator_kwargs

        stored = {"curator.provider": "ollama", "curator.ollama_url": "http://ollama.local:11434"}

        assert curator_kwargs(lambda k: stored.get(k, "")) == {"base_url": "http://ollama.local:11434"}

    def test_a_local_server_passes_its_url_and_needs_no_key(self):
        from shortlist.server.services.context_builder import curator_kwargs

        stored = {"curator.provider": "openai_compatible", "curator.openai_base_url": "http://llama:8080/v1"}

        assert curator_kwargs(lambda k: stored.get(k, "")) == {"base_url": "http://llama:8080/v1"}

    def test_a_hosted_gateway_may_still_carry_a_key(self):
        """A local server wants no key; OpenRouter does. Both are the same provider."""
        from shortlist.server.services.context_builder import curator_kwargs

        stored = {
            "curator.provider": "openai_compatible",
            "curator.openai_base_url": "https://openrouter.ai/api/v1",
            "curator.api_key": "sk-or-abc",
        }

        assert curator_kwargs(lambda k: stored.get(k, "")) == {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-abc",
        }

    def test_previous_picks_carries_the_latest_run_per_row_and_library(self, service, sessions, configured):
        from shortlist.server.db.models import Run

        with sessions() as session:
            session.add(User(plex_account_id=1, username="sarah", slug="sarah", enabled=True))
            old = Run(trigger="manual", status="ok", dry_run=False, stats={})
            new = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add_all([old, new])
            session.commit()
            user_id = session.query(User).one().id
            old_id, new_id = old.id, new.id

            def pick(run_id, tmdb_id, rank, slug="picked", section="movies-1", mt="movie"):
                return PickRow(
                    run_id=run_id,
                    user_id=user_id,
                    tmdb_id=tmdb_id,
                    media_type=mt,
                    rating_key=tmdb_id,
                    rank=rank,
                    collection_slug=slug,
                    section_key=section,
                    title=f"t{tmdb_id}",
                    reason="because",
                )

            # An older run's picks for the row, then a newer run that rebuilt it — carry-forward must
            # take the NEWER set. A blank-stamp pick (legacy) can't map to a row, so it's skipped.
            session.add_all([pick(old_id, 100, 1), pick(old_id, 101, 2)])
            session.add_all([pick(new_id, 200, 2), pick(new_id, 201, 1)])
            session.add(pick(new_id, 300, 1, slug="", section=""))
            session.commit()

        ctx = service.build_context(dry_run=True)
        got = ctx.previous_picks[("sarah", "picked", "movies-1")]
        # Only the newest run's picks, ordered by rank, reconstructed as engine Pick objects.
        assert [p.tmdb_id for p in got] == [201, 200]
        assert got[0].title == "t201" and got[0].reason == "because"
        # The legacy unstamped pick maps to no row and is dropped, not filed under ("", "").
        assert ("sarah", "", "") not in ctx.previous_picks


class TestBuildRequests:
    """The adapter turns request.* settings into a RequestConfig — off, whole, and half-configured."""

    def _store(self, sessions, tmp_path, values: dict):
        box = SecretBox(tmp_path)
        with sessions() as session:
            store = SettingsStore(session, box)
            for key, value in values.items():
                store.set(key, value)
        # A fresh store over a new session, so secret reads go through decrypt like production.
        session = sessions()
        return SettingsStore(session, box)

    def test_off_by_default_returns_none(self, sessions, tmp_path):
        store = self._store(sessions, tmp_path, {})
        assert ContextBuilder._build_requests(store) is None

    def test_enabled_with_both_apps_builds_both_targets(self, sessions, tmp_path):
        store = self._store(
            sessions,
            tmp_path,
            {
                "requests.enabled": True,
                "requests.radarr.url": "http://radarr:7878",
                "requests.radarr.apikey": "rk",
                "requests.radarr.quality_profile_id": 4,
                "requests.radarr.root_folder": "/movies",
                "requests.sonarr.url": "http://sonarr:8989",
                "requests.sonarr.apikey": "sk",
                "requests.sonarr.quality_profile_id": 7,
                "requests.sonarr.root_folder": "/tv",
                "requests.min_rating": 7.5,
                "requests.min_votes": 250,
                "requests.max_per_run": 3,
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg is not None and cfg.enabled
        assert cfg.radarr.url == "http://radarr:7878" and cfg.radarr.api_key == "rk"
        assert cfg.radarr.quality_profile_id == 4 and cfg.radarr.root_folder == "/movies"
        assert cfg.sonarr.api_key == "sk" and cfg.sonarr.quality_profile_id == 7
        assert (cfg.min_rating, cfg.min_votes, cfg.max_per_run) == (7.5, 250, 3)

    def test_half_configured_app_is_left_as_none(self, sessions, tmp_path):
        # Radarr has a URL but no key -> its target is None (movies skipped), Sonarr is whole.
        store = self._store(
            sessions,
            tmp_path,
            {
                "requests.enabled": True,
                "requests.radarr.url": "http://radarr:7878",
                "requests.sonarr.url": "http://sonarr:8989",
                "requests.sonarr.apikey": "sk",
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg.radarr is None  # no key -> not built, rather than erroring mid-run
        assert cfg.sonarr is not None


class TestRequestTag:
    """Only an EXPLICIT per-user request tag is applied — automatic username-tagging was removed
    (owner decision 2026-07-20; the requester is already shown in the inbox why-line)."""

    def test_only_explicit_tags_are_used_never_the_username(self, sessions, tmp_path):
        with sessions() as session:
            session.add_all(
                [
                    User(username="MooHouse", slug="moohouse", plex_account_id=1, user_type="shared", enabled=True),
                    User(
                        username="Sarah",
                        slug="sarah",
                        plex_account_id=2,
                        user_type="shared",
                        enabled=True,
                        request_tag="vip",
                    ),
                ]
            )
            session.commit()
        builder = ContextBuilder(sessions, SecretBox(tmp_path), EventBus())
        with sessions() as session:
            tags = {p.username: p.request_tag for p in builder.enabled_profiles(session)}
        assert tags["MooHouse"] == ""  # no explicit tag -> no per-user tag (never the username)
        assert tags["Sarah"] == "vip"  # an explicit tag is used


class TestSyncWatched:
    """Daily watch-sync: refresh watched_at from current history without rebuilding rows."""

    def test_marks_a_pick_watched_from_current_history(self, service, sessions, monkeypatch):
        import asyncio
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from shortlist.engine.models import UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import PickRow, Run, User

        with sessions() as s:
            user = User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True)
            s.add(user)
            s.flush()
            run = Run(trigger="manual", status="ok", started_at=datetime.now(UTC) - timedelta(days=1))
            s.add(run)
            s.flush()
            s.add(
                PickRow(
                    run_id=run.id, user_id=user.id, tmdb_id=42, media_type="movie", rating_key=1, rank=1, title="Dune"
                )
            )
            s.commit()

        # This person has since watched the recommended title — the sync must credit it, no run needed.
        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")
        watch = WatchedItem(title="Dune", media_type=MediaType.MOVIE, watched_at=datetime.now(UTC), tmdb_id=42)
        fake_ctx = SimpleNamespace(
            history_source=SimpleNamespace(fetch=lambda p, **k: [watch]),
            config=SimpleNamespace(min_completion=0.7),
        )
        monkeypatch.setattr(service, "build_context", lambda **k: fake_ctx)
        monkeypatch.setattr(service, "enabled_profiles", lambda session, user_ids=None: [profile])

        asyncio.run(service.sync_watched())

        with sessions() as s:
            assert s.query(PickRow).filter_by(tmdb_id=42).one().watched_at is not None


def test_build_scheduler_registers_the_daily_watch_sync(sessions):
    from types import SimpleNamespace

    from shortlist.server.scheduler import WATCH_SYNC_JOB_ID, build_scheduler

    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions, run_service=None))
    scheduler = build_scheduler(app)
    assert scheduler.get_job(WATCH_SYNC_JOB_ID) is not None  # daily, independent of any row's cron
