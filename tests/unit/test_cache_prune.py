"""`prune_expired_cache` clears rows nothing can read — expired ones, and stranded namespaces.

Renaming a cache key prefix strands every row under the old name: the rows are live by their TTL
and unreachable by their key, so expiry alone leaves them sitting in the file the nightly backup
copies whole. Measured on the maintainer's 46-user server after `websearch:` became `websearch2:`:
711 stranded rows, none of them expired.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.db.models import Base, CacheRow
from shortlist.server.services.run_persistence import prune_expired_cache

HOUR = 3600


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def _add(session, kind: str, key: str, *, expires_in: float) -> None:
    session.add(CacheRow(kind=kind, key=key, value="{}", expires_at=time.time() + expires_in))


def _keys(session) -> set[str]:
    return {row.key for row in session.query(CacheRow).all()}


class TestExpiredRowsGo:
    def test_an_expired_row_is_deleted_and_a_live_one_is_not(self, sessions):
        with sessions() as s:
            _add(s, "tmdb", "tmdb:live", expires_in=HOUR)
            _add(s, "tmdb", "tmdb:stale", expires_in=-HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert _keys(s) == {"tmdb:live"}


class TestStrandedNamespacesGo:
    def test_an_unexpired_row_under_a_retired_prefix_is_deleted(self, sessions):
        """The whole point: these rows are NOT expired, so the TTL sweep leaves them for ever."""
        with sessions() as s:
            _add(s, "websearch", "websearch:exa:show:95396", expires_in=14 * 24 * HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert _keys(s) == set()

    def test_the_live_namespace_survives_though_it_shares_a_kind(self, sessions):
        """`websearch:` and `websearch2:` are both `kind='websearch'`, so a kind-wide delete would
        take the live rows with the dead ones. Matching is on the KEY for exactly this reason."""
        with sessions() as s:
            _add(s, "websearch", "websearch:exa:show:95396", expires_in=14 * 24 * HOUR)
            _add(s, "websearch", "websearch2:exa:show:95396", expires_in=14 * 24 * HOUR)
            _add(s, "websearch", "websearch2:exa:movie:1234", expires_in=14 * 24 * HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert _keys(s) == {"websearch2:exa:show:95396", "websearch2:exa:movie:1234"}

    def test_other_kinds_are_untouched(self, sessions):
        with sessions() as s:
            _add(s, "tmdb", "tmdb:movie:1", expires_in=HOUR)
            _add(s, "mdblist", "mdblist:tt1", expires_in=HOUR)
            _add(s, "library_index", "library_index:1", expires_in=HOUR)
            _add(s, "websearch", "websearch:exa:show:1", expires_in=HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert _keys(s) == {"tmdb:movie:1", "mdblist:tt1", "library_index:1"}

    def test_the_count_covers_both_kinds_of_dead_row(self, sessions):
        with sessions() as s:
            _add(s, "tmdb", "tmdb:stale", expires_in=-HOUR)
            _add(s, "websearch", "websearch:exa:show:1", expires_in=14 * 24 * HOUR)
            _add(s, "websearch", "websearch:exa:show:2", expires_in=14 * 24 * HOUR)
            s.commit()

            assert prune_expired_cache(s) == 3

    def test_a_prefix_with_an_underscore_matches_literally(self, sessions, monkeypatch):
        """In SQL LIKE, `_` matches ANY single character. A hand-built `LIKE 'library_index:%'` would
        therefore also match `libraryXindex:` — and this code path DELETES. The prefix that prompted
        this (`websearch:`) has no underscore, so nothing here would have caught the mistake; the
        next retired namespace could easily have one."""
        monkeypatch.setattr("shortlist.server.services.run_persistence._RETIRED_CACHE_PREFIXES", ("library_index:",))
        with sessions() as s:
            _add(s, "library_index", "library_index:1", expires_in=HOUR)
            _add(s, "library_index", "libraryXindex:1", expires_in=HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert _keys(s) == {"libraryXindex:1"}

    def test_it_is_idempotent(self, sessions):
        """The maintenance job is replayed after a crash, so a second pass must find nothing."""
        with sessions() as s:
            _add(s, "websearch", "websearch:exa:show:1", expires_in=14 * 24 * HOUR)
            s.commit()

            assert prune_expired_cache(s) == 1
            s.commit()
            assert prune_expired_cache(s) == 0


class TestCacheTtlIsVisibleToTheOwner:
    """The TTL is a freshness promise, so the UI states it — and a promise that drifts is worse
    than none. These pin the number to the copy the owner actually reads."""

    def test_cache_ttl_matches_the_ui(self):
        """The Settings footnote quotes the TTL in days; the engine owns it in seconds.

        Two languages, so nothing but a test keeps them honest. If you change
        `WEB_SEARCH_CACHE_TTL_S`, change `WEB_SEARCH_CACHE_DAYS` in the TSX with it — otherwise the
        card tells the owner their picks are at most N days old when they can be older.
        """
        import re
        from pathlib import Path

        from shortlist.engine.candidates import WEB_SEARCH_CACHE_TTL_S

        tsx = Path(__file__).parents[2] / "web/src/components/settings/connections-section.tsx"
        declared = re.search(r"const WEB_SEARCH_CACHE_DAYS = (\d+);", tsx.read_text())
        assert declared, "the UI no longer declares WEB_SEARCH_CACHE_DAYS — update this test with it"
        assert int(declared.group(1)) == WEB_SEARCH_CACHE_TTL_S // (24 * 3600)

    def test_a_thin_result_is_not_held_for_the_full_ttl(self):
        """The short TTL only means anything while it stays SHORTER than the full one."""
        from shortlist.engine.candidates import _THIN_CACHE_TTL_S, WEB_SEARCH_CACHE_TTL_S

        assert _THIN_CACHE_TTL_S < WEB_SEARCH_CACHE_TTL_S
