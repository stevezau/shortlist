"""The shared test app (`tests.shared_app`) must not carry anything from one test into the next."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.shared_app import app_for, reset_shared_apps

pytestmark = pytest.mark.integration


class TestTheSharedAppIsResetBetweenTests:
    def test_state_written_by_a_test_is_gone_for_the_next_one(self, tmp_path: Path):
        app = app_for(tmp_path / "first")
        with TestClient(app):
            app.state.planted = "by the first test"
            # The way `cached_plex_read` stores its cache: past `State`'s own attribute dict.
            app.state.__dict__["_plex_read_cache"] = {"libraries": "stale"}
            app.dependency_overrides[object] = object
        reset_shared_apps()

        again = app_for(tmp_path / "second")
        assert again is app
        assert not hasattr(again.state, "planted")
        assert "_plex_read_cache" not in again.state.__dict__
        assert again.dependency_overrides == {}

    def test_each_test_starts_against_its_own_config_dir(self, tmp_path: Path):
        app = app_for(tmp_path / "first")
        with TestClient(app):
            pass
        reset_shared_apps()

        with TestClient(app_for(tmp_path / "second")) as client:
            assert client.app.state.config_dir == tmp_path / "second"
            assert (tmp_path / "second" / "shortlist.db").exists()

    def test_a_second_app_in_the_same_test_is_a_different_app(self, tmp_path: Path):
        assert app_for(tmp_path / "a") is not app_for(tmp_path / "b")

    def test_an_app_whose_lifespan_never_ended_is_not_handed_out_again(self, tmp_path: Path):
        app = app_for(tmp_path / "first")
        client = TestClient(app)
        client.__enter__()
        try:
            reset_shared_apps()
            assert app_for(tmp_path / "second") is not app
        finally:
            client.__exit__(None, None, None)
