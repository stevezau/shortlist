"""RunService.build_context branch matrix."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import shortlist.server.services.context_builder as context_builder_mod
from shortlist.engine.history import ShareTokenWatchSource
from shortlist.engine.models import MediaType
from shortlist.server.db.models import PickRow, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
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

    def test_watched_state_is_read_via_the_share_token_source(self, service, configured):
        ctx = service.build_context(dry_run=True)
        # The one watch source: each user's complete watched set read from the PMS AS them, with the
        # per-user server token plex.tv mints. No Tautulli/history-API/DB-mirror wrapper anymore.
        assert isinstance(ctx.history_source, ShareTokenWatchSource)
        assert ctx.curator.name == "none"
        assert ctx.config.dry_run is True

    def test_plex_only_skips_the_clients_a_label_walk_never_touches(self, service, configured, monkeypatch):
        """The reconciles, the pause/disable handlers and the watch sync only ever walk collections
        under a label — but every one of them opened Trakt, Exa, MDBList, the LLM curator and the
        poster studio first, so a wrong LLM key could fail a read of watch history.

        `_refuse_a_different_server` still runs: it is what stops a reconcile enumerating a stranger's
        PMS, finding zero Shortlist collections, and concluding every row is gone.
        """
        monkeypatch.setattr(
            context_builder_mod, "make_studio", lambda *a, **k: pytest.fail("plex-only built the poster studio")
        )

        ctx = service.build_context(dry_run=True, plex_only=True)

        assert ctx.plex is not None and isinstance(ctx.history_source, ShareTokenWatchSource)
        assert ctx.curator.name == "none"  # the NullCurator, not whatever provider is configured
        assert ctx.trakt is None and ctx.search is None and ctx.mdblist is None and ctx.poster_artist is None
        assert ctx.config.dry_run is True

    def test_plex_only_still_refuses_a_different_server(self, service, sessions, configured):
        from shortlist.server.db.models import Server

        with sessions() as session:
            session.add(
                Server(machine_id="a-different-machine", name="elsewhere", url="http://elsewhere:32400", token_enc="x")
            )
            session.commit()

        with pytest.raises(RuntimeError, match="different server"):
            service.build_context(dry_run=False, plex_only=True)

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

    def test_tautulli_config_does_not_change_the_watch_source(self, service, sessions, configured):
        # Tautulli is no longer a watch SOURCE (only friendly names + a setup probe) — configuring it
        # must not swap in a different history source. The share-token read is used either way.
        with sessions() as session:
            store = SettingsStore(session, configured)
            store.set("tautulli.url", "http://taut:8181")
            store.set("tautulli.apikey", "tk")
        ctx = service.build_context(dry_run=False)
        assert isinstance(ctx.history_source, ShareTokenWatchSource)

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

    def test_a_managed_user_with_no_parental_profile_is_built_for(self, service, sessions, configured):
        """Issue #20's other half. `enabled_profiles` dropped every account with `restricted` set —
        which plex.tv reports for EVERY Plex Home user. So a managed user with no age restriction got
        no row, silently, while the Users page offered an enable toggle and the docs promised one.

        Plex hides nothing from these accounts; they are ordinary users."""
        with sessions() as session:
            session.add(
                User(
                    plex_account_id=1,
                    username="kid",
                    slug="kid",
                    enabled=True,
                    user_type="managed",
                    restricted=True,
                    restriction_profile="",
                )
            )
            session.commit()
            profiles = service.enabled_profiles(session)

        assert [p.slug for p in profiles] == ["kid"]

    def test_a_managed_user_WITH_a_profile_is_still_skipped(self, service, sessions, configured):
        """Plex hides every collection from a profiled account, so a row would be invisible — and Plex
        refuses the share filters that would make it private anyway."""
        with sessions() as session:
            session.add(
                User(
                    plex_account_id=2,
                    username="littleone",
                    slug="littleone",
                    enabled=True,
                    user_type="managed",
                    restricted=True,
                    restriction_profile="little_kid",
                )
            )
            session.commit()
            profiles = service.enabled_profiles(session)

        assert profiles == []

    def test_delivered_keys_reach_the_engine_under_the_key_delivery_looks_up(self, service, sessions, configured):
        """The DB→engine wiring for the delivery ledger, asserted as the literal tuple.

        `rows.py` unpacks `(user_slug, row_slug, section_key)` and looks up by `str(section.key)`. Swap
        two elements here, or drop the `str()`, and every lookup silently returns nothing — delivery
        falls back to the count guess, which is a full regression to the bug the ledger exists to fix,
        with a green suite. The sibling `_previous_picks` has exactly this test; this reader had none.
        """
        from shortlist.server.db.models import Delivery

        with sessions() as session:
            session.add(User(plex_account_id=1, username="sarah", slug="sarah", enabled=True))
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=9001))
            session.add(Delivery(collection_slug="gems", user_slug="sarah", library_key="2", rating_key=9002))
            session.commit()

        ctx = service.build_context(dry_run=True)

        assert ctx.delivered_keys[("sarah", "picked", "1")] == 9001
        assert ctx.delivered_keys[("sarah", "gems", "2")] == 9002

    def test_a_ratingkey_two_rows_claim_is_dropped_rather_than_arbitrated(self, service, sessions, configured):
        """The safety valve that makes a bad ledger self-heal. Two rows naming one collection is
        reachable if a run died between the delete and the persist on delivery's rebuild path — and
        picking a winner would let the loser's build retitle the winner's live collection.

        Dropping BOTH sends delivery back to matching by title, which is where it was before the
        ledger: no worse, and it recovers on the next successful run."""
        from shortlist.server.db.models import Delivery

        with sessions() as session:
            session.add(User(plex_account_id=1, username="sarah", slug="sarah", enabled=True))
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=9001))
            session.add(Delivery(collection_slug="gems", user_slug="sarah", library_key="1", rating_key=9001))
            session.add(Delivery(collection_slug="safe", user_slug="sarah", library_key="2", rating_key=9002))
            session.commit()

        ctx = service.build_context(dry_run=True)

        assert ("sarah", "picked", "1") not in ctx.delivered_keys
        assert ("sarah", "gems", "1") not in ctx.delivered_keys
        assert ctx.delivered_keys[("sarah", "safe", "2")] == 9002, "an unambiguous key is unaffected"

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
                    sources="tmdb_similar",
                    affinity=0.42,
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
        # Provenance round-trips. Without this a carried-forward pick comes back blank, so on every
        # non-refresh night the UI's "suggested by …" line vanishes and the pick is RE-PERSISTED as
        # "not recorded" — provenance would survive exactly one run.
        assert got[0].sources == ["tmdb_similar"]
        assert got[0].affinity == 0.42


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
                "requests.sonarr.quality_profile_id": 1,
                "requests.sonarr.root_folder": "/tv",
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg.radarr is None  # no key -> not built, rather than erroring mid-run
        assert cfg.sonarr is not None

    def test_incomplete_target_missing_profile_or_folder(self, sessions, tmp_path):
        # URL+key set but no quality profile or root folder -> treated as not configured, with warning.
        store = self._store(
            sessions,
            tmp_path,
            {
                "requests.enabled": True,
                "requests.radarr.url": "http://radarr:7878",
                "requests.radarr.apikey": "rk",
                "requests.sonarr.url": "http://sonarr:8989",
                "requests.sonarr.apikey": "sk",
                "requests.sonarr.quality_profile_id": 1,
                "requests.sonarr.root_folder": "/tv",
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg.radarr is None  # key present but no profile/folder -> None
        assert cfg.sonarr is not None  # fully configured
        assert len(cfg.incomplete_targets) == 1
        assert "Radarr" in cfg.incomplete_targets[0]
        assert "quality profile" in cfg.incomplete_targets[0]


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
        # The sync reads through the watched-title cache, which walks the server's sections and asks
        # the history source for one library at a time — so the fake has to offer both.
        fake_ctx = SimpleNamespace(
            plex=SimpleNamespace(sections=lambda: [SimpleNamespace(key="1", type="movie")]),
            history_source=SimpleNamespace(
                fetch=lambda p, **k: [watch],
                fetch_section=lambda p, section, media_type, since=None: [watch],
            ),
            config=SimpleNamespace(min_completion=0.7),
        )
        monkeypatch.setattr(service, "build_context", lambda **k: fake_ctx)
        monkeypatch.setattr(service, "enabled_profiles", lambda session, user_ids=None: [profile])

        asyncio.run(service.sync_watched())

        with sessions() as s:
            assert s.query(PickRow).filter_by(tmdb_id=42).one().watched_at is not None

    def test_an_unreadable_library_falls_back_to_a_complete_read(self, service, sessions, monkeypatch):
        """A PARTIAL cache must never be served as if it were complete.

        The watched set is what stops an already-seen title being recommended again, so serving a
        stale one is a visible regression. If any section fails — most likely the PMS refusing the
        incremental filter — fall back to the direct complete read: the behaviour before the cache
        existed, so it cannot be worse, only slower.
        """
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User

        with sessions() as session:
            session.add(User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True))
            session.commit()

        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")
        complete = WatchedItem(
            title="From the complete read", media_type=MediaType.MOVIE, watched_at=datetime.now(UTC), tmdb_id=9
        )

        def boom(_profile, _section, _media, since=None):
            raise RuntimeError("PMS refused the filter")

        ctx = SimpleNamespace(
            plex=SimpleNamespace(sections=lambda: [SimpleNamespace(key="1", type="movie")]),
            history_source=SimpleNamespace(fetch=lambda p, **k: [complete], fetch_section=boom),
            config=SimpleNamespace(min_completion=0.7),
        )

        history = service.refresh_watched(ctx, profile)

        assert [i.title for i in history] == ["From the complete read"]

    def test_an_unshared_library_is_skipped_and_keeps_the_cache(self, service, sessions, monkeypatch):
        """A 403 is "not shared with them", NOT an unreadable section.

        `ctx.plex.sections()` is the OWNER's library list walked for every person, so every library
        someone isn't given 403s on every single sync. Counting that as a failure discarded their
        whole cache and forced an uncached complete re-read of every library, hourly, for ever
        (SFLIX: two users). The readable library's cached titles must come back WITHOUT the
        complete-read fallback firing.
        """
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from shortlist.engine.clients.plex_pms import SectionNotShared
        from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User

        with sessions() as session:
            session.add(User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True))
            session.commit()

        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")
        shared_title = WatchedItem(title="Dune", media_type=MediaType.MOVIE, watched_at=datetime.now(UTC), tmdb_id=42)
        fallback_calls: list[object] = []

        def read_section(_profile, section, _media, since=None):
            if str(section.key) == "12":
                raise SectionNotShared("section 12 is not shared with this user")
            return [shared_title]

        def complete_read(p, **k):
            fallback_calls.append(p)
            return []

        ctx = SimpleNamespace(
            plex=SimpleNamespace(
                sections=lambda: [
                    SimpleNamespace(key="1", type="movie"),
                    SimpleNamespace(key="12", type="show"),
                ]
            ),
            history_source=SimpleNamespace(fetch=complete_read, fetch_section=read_section),
            config=SimpleNamespace(min_completion=0.7),
        )

        history = service.refresh_watched(ctx, profile)

        assert [i.title for i in history] == ["Dune"]
        assert fallback_calls == [], "a 403 must not trigger the complete-read fallback"

    def _two_library_ctx(self, sections, read_section):
        from types import SimpleNamespace

        return SimpleNamespace(
            plex=SimpleNamespace(sections=lambda: [SimpleNamespace(key=k, type="movie") for k in sections]),
            history_source=SimpleNamespace(fetch=lambda p, **k: [], fetch_section=read_section),
            config=SimpleNamespace(min_completion=0.7),
        )

    def test_a_library_removed_from_the_server_is_forgotten(self, service, sessions, monkeypatch):
        """Nothing else sweeps these. `sync_section` only ever replaces sections it READ, so titles
        cached from a library that no longer exists would go on counting as watched for ever —
        suppressing recommendations on behalf of a library nobody can watch."""
        from datetime import UTC, datetime

        from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User, WatchedTitle, WatchSyncState

        with sessions() as session:
            session.add(User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True))
            session.commit()

        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")

        def read_section(_profile, section, _media, since=None):
            title = "Dune" if str(section.key) == "1" else "Heat"
            return [
                WatchedItem(
                    title=title,
                    media_type=MediaType.MOVIE,
                    watched_at=datetime.now(UTC),
                    tmdb_id=int(section.key),
                    rating_key=int(section.key),
                )
            ]

        service.refresh_watched(self._two_library_ctx(["1", "2"], read_section), profile)
        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Dune", "Heat"}

        # Library 2 deleted from the server: it is simply absent from sections() now. Swept on the
        # weekly pass only — the sweep believes one cached `/library/sections` answer, so it runs at
        # the cadence of the full read rather than every sync.
        history = service.refresh_watched(self._two_library_ctx(["1"], read_section), profile, force_full=True)

        assert [item.title for item in history] == ["Dune"]
        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Dune"]
            assert [state.section_key for state in session.query(WatchSyncState).all()] == ["1"]

    def test_an_ordinary_sync_does_not_sweep_libraries(self, service, sessions, monkeypatch):
        """One short `/library/sections` response would otherwise be applied to every user in the
        sync — `PlexClient` caches that list for the life of the client, so a bad answer is pinned
        for the whole run. Restricting the sweep to the weekly pass cuts the exposure ~168x."""
        from datetime import UTC, datetime

        from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User, WatchedTitle

        with sessions() as session:
            session.add(User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True))
            session.commit()

        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")

        def read_section(_profile, section, _media, since=None):
            return [
                WatchedItem(
                    title=f"T{section.key}",
                    media_type=MediaType.MOVIE,
                    watched_at=datetime.now(UTC),
                    tmdb_id=int(section.key),
                    rating_key=int(section.key),
                )
            ]

        service.refresh_watched(self._two_library_ctx(["1", "2"], read_section), profile)

        # A blip: sections() briefly answers with one library on an ordinary hourly sync.
        service.refresh_watched(self._two_library_ctx(["1"], read_section), profile)

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"T1", "T2"}

    def test_a_library_that_is_merely_unshared_keeps_its_cached_history(self, service, sessions, monkeypatch):
        """The distinction the sweep turns on. An unshared library is still ON the server — it 403s
        per-person, on every sync, for every library someone isn't given — and what they watched there
        is still true. Only libraries gone server-wide may be forgotten."""
        from datetime import UTC, datetime

        from shortlist.engine.clients.plex_pms import SectionNotShared
        from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
        from shortlist.server.db.models import User, WatchedTitle

        with sessions() as session:
            session.add(User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True))
            session.commit()

        profile = UserProfile(username="sarah", plex_account_id=1, user_type=UserType.SHARED, slug="sarah")
        shared: dict[str, bool] = {"2": True}

        def read_section(_profile, section, _media, since=None):
            key = str(section.key)
            if not shared.get(key, True):
                raise SectionNotShared(f"section {key} is not shared with this user")
            return [
                WatchedItem(
                    title=f"T{key}",
                    media_type=MediaType.MOVIE,
                    watched_at=datetime.now(UTC),
                    tmdb_id=int(key),
                    rating_key=int(key),
                )
            ]

        service.refresh_watched(self._two_library_ctx(["1", "2"], read_section), profile)
        shared["2"] = False  # access revoked, but the library is still on the server

        # force_full so the sweep actually RUNS — otherwise this passes for the wrong reason.
        service.refresh_watched(self._two_library_ctx(["1", "2"], read_section), profile, force_full=True)

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"T1", "T2"}

    def test_the_prefill_skips_people_this_run_will_not_build_for(self, service):
        """Every row carries its own cron, so a SCHEDULED run is always scoped to a subset of rows.

        `_run_user` returns "skipped" before reading any history for anyone with no row in scope, so
        pre-filling them is a complete per-user PMS read spent on someone the run then skips.

        REQUIRES `shortlist.engine.rows.builds_anything_for(profile, config)`. The server asks the
        engine that question now instead of importing the engine's private `_in_audience`/`_is_muted`
        and re-assembling the rule. `_has_a_row_in_scope` fails OPEN when the export is missing — so
        this test failing with `True` means the engine has not exported it, and every scoped run is
        pre-filling history for people it then skips.
        """
        from shortlist.engine.models import EngineConfig, RowSpec, UserProfile, UserType

        included = UserProfile(username="in", plex_account_id=1, user_type=UserType.SHARED, slug="in")
        excluded = UserProfile(username="out", plex_account_id=2, user_type=UserType.SHARED, slug="out")
        # One row, whose audience is only `included`'s account, and it IS in this run's scope.
        config = EngineConfig(
            rows=[RowSpec(slug="picked", name_template="Picked", size=10, audience={1})],
            build_only=["picked"],
        )
        ctx = SimpleNamespace(config=config)

        assert service._has_a_row_in_scope(ctx, included) is True
        assert service._has_a_row_in_scope(ctx, excluded) is False

    def test_the_prefill_scope_check_fails_open(self, service):
        """A context that cannot answer must be treated as in-scope — the worst case is then exactly
        the behaviour before the narrowing, never a person silently missing their history."""
        assert service._has_a_row_in_scope(SimpleNamespace(), object()) is True

    def test_streams_per_user_progress_and_a_finished_event(self, service, monkeypatch):
        """The Tools page bar is driven by these events — a sync that emits nothing shows no bar."""
        import asyncio
        from types import SimpleNamespace

        from shortlist.engine.models import UserProfile, UserType

        published: list[tuple[str, dict]] = []
        monkeypatch.setattr(service._bus, "publish", lambda event, data: published.append((event, data)))

        profiles = [
            UserProfile(username=f"u{i}", plex_account_id=i, user_type=UserType.SHARED, slug=f"u{i}") for i in range(3)
        ]
        fake_ctx = SimpleNamespace(
            history_source=SimpleNamespace(fetch=lambda p, **k: []),
            config=SimpleNamespace(min_completion=0.7),
        )
        monkeypatch.setattr(service, "build_context", lambda **k: fake_ctx)
        monkeypatch.setattr(service, "enabled_profiles", lambda session, user_ids=None: profiles)

        asyncio.run(service.sync_watched())

        progress = [d for e, d in published if e == "sync.progress"]
        # An initial 0/3 plus one per user, all tagged for the watched card, counting up to the total.
        assert progress[0] == {"kind": "watched", "done": 0, "total": 3}
        assert [d["done"] for d in progress] == [0, 1, 2, 3]
        assert all(d["total"] == 3 for d in progress)
        assert ("sync.finished", {"kind": "watched", "ok": True, "count": 3}) in published

    def test_a_sync_that_cannot_start_still_reports_a_failed_finish(self, service, monkeypatch):
        """Plex not configured raises inside build_context — the bar must resolve to an error, not hang."""
        import asyncio

        published: list[tuple[str, dict]] = []
        monkeypatch.setattr(service._bus, "publish", lambda event, data: published.append((event, data)))

        def boom(**kwargs):
            raise RuntimeError("Plex is not configured")

        monkeypatch.setattr(service, "build_context", boom)

        asyncio.run(service.sync_watched())  # must not raise — the scheduler relies on this

        finished = [d for e, d in published if e == "sync.finished"]
        assert finished == [{"kind": "watched", "ok": False, "error": "RuntimeError"}]


def test_build_scheduler_registers_the_daily_watch_sync(sessions, tmp_path):
    from types import SimpleNamespace

    from shortlist.server.scheduler import BACKUP_JOB_ID, WATCH_SYNC_JOB_ID, build_scheduler

    app = SimpleNamespace(state=SimpleNamespace(sessions=sessions, run_service=None, config_dir=tmp_path))
    scheduler = build_scheduler(app)
    assert scheduler.get_job(WATCH_SYNC_JOB_ID) is not None  # daily, independent of any row's cron
    assert scheduler.get_job(BACKUP_JOB_ID) is not None  # daily DB backup


class TestRefusingADifferentServer:
    """Every record Shortlist holds is scoped to ONE Plex machine — the delivery ledger says which
    collection is whose, `restriction_snapshots` holds each account's filters as they were before we
    touched them, the user table says who the owner is.

    Settings refuses a repoint, but only when the new server ANSWERS at save time; a box that is down
    then and up later slips past. This is the check at the point of use, where it cannot be skipped —
    and it matters most for the privacy sync, because a stranger's PMS enumerates ZERO Shortlist
    collections, which reads as "every row is gone".
    """

    def _check(self, linked: str | None, reported: str):
        from types import SimpleNamespace

        from shortlist.server.services.context_builder import _refuse_a_different_server

        session = SimpleNamespace(
            query=lambda model: SimpleNamespace(first=lambda: SimpleNamespace(machine_id=linked) if linked else None)
        )
        _refuse_a_different_server(session, reported)

    def test_a_different_machine_aborts_before_anything_is_touched(self):
        with pytest.raises(RuntimeError, match="different server"):
            self._check(linked="m1", reported="someone-elses-server")

    def test_the_linked_machine_passes(self):
        self._check(linked="m1", reported="m1")

    def test_an_unlinked_instance_passes(self):
        """Setup itself builds a context before a `Server` row exists — refusing there would make the
        wizard unable to complete."""
        self._check(linked=None, reported="m1")


class TestUserWatched:
    """`user_watched` is the searchable view of the CACHE — the set recommendations are filtered
    against. It is a plain DB query, so no Plex fixtures: what matters is that the filters compose,
    the paging totals are honest, and one person's search can never reach another's rows."""

    @staticmethod
    def _seed(sessions):
        from datetime import UTC, datetime

        from shortlist.server.db.models import WatchedTitle, WatchSyncState

        with sessions() as session:
            session.add(User(id=1, plex_account_id=1, username="sarah", slug="sarah"))
            session.add(User(id=2, plex_account_id=2, username="mike", slug="mike"))
            rows = [
                ("Teacup", "show", 2024, 3, 8, 1),
                ("The Bear", "show", 2022, 30, 30, 1),
                ("Dune: Part Two", "movie", 2024, None, None, 2),
                ("Tea Leaves", "movie", 2019, None, None, 1),
            ]
            for i, (title, media, year, viewed, leaf, count) in enumerate(rows):
                session.add(
                    WatchedTitle(
                        user_id=1,
                        section_key="1",
                        rating_key=100 + i,
                        tmdb_id=500 + i,
                        media_type=media,
                        title=title,
                        year=year,
                        watch_count=count,
                        viewed_leaf_count=viewed,
                        leaf_count=leaf,
                        viewed_at=datetime(2026, 8, 1 + i, tzinfo=UTC),
                    )
                )
            # Mike watched something that would match sarah's search, to prove the scoping.
            session.add(
                WatchedTitle(
                    user_id=2,
                    section_key="1",
                    rating_key=999,
                    tmdb_id=999,
                    media_type="show",
                    title="Teacup",
                    year=2024,
                    viewed_at=datetime(2026, 8, 1, tzinfo=UTC),
                )
            )
            session.add(
                WatchSyncState(
                    user_id=1,
                    section_key="1",
                    last_full_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
                    item_count=4,
                )
            )
            session.commit()

    def test_returns_the_whole_set_newest_first_with_sync_state(self, service, sessions):
        self._seed(sessions)

        page = service.user_watched(1)

        assert [i["title"] for i in page["items"]] == ["Tea Leaves", "Dune: Part Two", "The Bear", "Teacup"]
        assert page["total"] == 4
        assert page["synced_titles"] == 4
        assert page["last_full_sync_at"].startswith("2026-08-04T10:00")

    def test_search_is_case_insensitive_and_matches_a_substring(self, service, sessions):
        self._seed(sessions)

        page = service.user_watched(1, q="tea")

        assert sorted(i["title"] for i in page["items"]) == ["Tea Leaves", "Teacup"]
        assert page["total"] == 2

    def test_search_never_reaches_another_persons_rows(self, service, sessions):
        """Mike has a "Teacup" too. Sarah's search must not see it, and Mike's must not see hers."""
        self._seed(sessions)

        assert service.user_watched(1, q="teacup")["total"] == 1
        assert service.user_watched(2, q="tea")["total"] == 1
        assert service.user_watched(2, q="bear")["total"] == 0

    def test_media_filter_composes_with_search(self, service, sessions):
        self._seed(sessions)

        assert [i["title"] for i in service.user_watched(1, q="tea", media_type="movie")["items"]] == ["Tea Leaves"]
        assert [i["title"] for i in service.user_watched(1, q="tea", media_type="show")["items"]] == ["Teacup"]

    def test_total_counts_the_whole_match_not_the_page(self, service, sessions):
        """The "Show more" button reads this — a total that shrank to the page size would hide rows."""
        self._seed(sessions)

        page = service.user_watched(1, limit=2)

        assert len(page["items"]) == 2
        assert page["total"] == 4
        assert [i["title"] for i in service.user_watched(1, limit=2, offset=2)["items"]] == ["The Bear", "Teacup"]

    def test_a_wildcard_in_the_query_searches_literally(self, service, sessions):
        """`%` is a SQL wildcard. Unescaped, searching "%" would return the entire history — which
        reads as "your filter matched everything" rather than "nothing is called that"."""
        self._seed(sessions)

        assert service.user_watched(1, q="%")["total"] == 0

    def test_show_progress_and_watch_count_come_through(self, service, sessions):
        """The fields that let the page say "3 of 8 episodes" — the answer to "I watched that, why
        was it recommended?" — and that a movie was watched twice."""
        self._seed(sessions)

        by_title = {i["title"]: i for i in service.user_watched(1)["items"]}

        assert (by_title["Teacup"]["viewed_leaf_count"], by_title["Teacup"]["leaf_count"]) == (3, 8)
        assert by_title["Dune: Part Two"]["watch_count"] == 2
        assert by_title["Dune: Part Two"]["leaf_count"] is None  # a movie has no episode totals

    def test_sync_state_is_none_when_a_library_has_never_had_a_full_read(self, service, sessions):
        """ "Synced 4h ago" while a whole library is missing is a false claim of completeness."""
        from shortlist.server.db.models import WatchSyncState

        self._seed(sessions)
        with sessions() as session:
            session.add(WatchSyncState(user_id=1, section_key="2", last_full_at=None, item_count=0))
            session.commit()

        assert service.user_watched(1)["last_full_sync_at"] is None

    def test_unknown_user_is_none(self, service, sessions):
        self._seed(sessions)

        assert service.user_watched(999) is None

    def test_a_transferred_row_is_dated_and_ordered_by_its_TRUE_watch_date(self, service, sessions):
        """The panel must not rank a transferred history by the day the scrobbles landed. The two
        dates are inverted so ordering on `viewed_at` gives exactly the wrong answer — without that,
        removing the coalesce would not fail anything (every other seeded row leaves it NULL)."""
        from datetime import UTC, datetime

        from shortlist.server.db.models import WatchedTitle

        self._seed(sessions)
        with sessions() as session:
            session.add(
                WatchedTitle(
                    user_id=1,
                    section_key="1",
                    rating_key=900,
                    tmdb_id=900,
                    media_type="movie",
                    title="Transferred",
                    year=2021,
                    watch_count=1,
                    viewed_at=datetime(2020, 1, 1, tzinfo=UTC),  # scrobbled long ago by viewed_at
                    source_viewed_at=datetime(2026, 8, 9, tzinfo=UTC),  # actually the newest watch
                )
            )
            session.commit()

        page = service.user_watched(1)

        assert page["items"][0]["title"] == "Transferred"
        assert page["items"][0]["watched_at"].startswith("2026-08-09")
