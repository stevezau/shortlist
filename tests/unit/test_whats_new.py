"""shortlist/server/whats_new.py: which release notes the owner has not read yet, and closing them for good."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shortlist.server import whats_new
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.settings_store import SettingsStore
from tests.db_helpers import disposing_engine

RELEASES_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "github_releases.json"


def _recorded_releases() -> list[dict]:
    """The recorded GitHub list, in the shape `version_check.published_releases` returns — and in
    GitHub's own order, where v1.2.1 comes AFTER v1.3.0."""
    return [
        {
            "version": release["tag_name"].lstrip("v"),
            "url": release["html_url"],
            "published_at": release["published_at"],
            "notes": release["body"],
        }
        for release in json.loads(RELEASES_FIXTURE.read_text())["releases"]
    ]


@pytest.fixture
def store(tmp_path: Path):
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine, make_session_factory(engine)() as session:
        yield SettingsStore(session)


@pytest.fixture
def fetches(monkeypatch) -> dict[str, int]:
    """Serve the recorded releases and count how often GitHub would have been asked."""
    calls = {"n": 0}

    def recorded() -> list[dict]:
        calls["n"] += 1
        return _recorded_releases()

    monkeypatch.setattr(whats_new, "published_releases", recorded)
    return calls


def _versions(releases: list[dict]) -> list[str]:
    return [release["version"] for release in releases]


class TestInitialise:
    def test_a_fresh_install_starts_with_this_version_already_read(self, store):
        """Nobody upgraded: a new owner running the wizard has nothing to catch up on."""
        whats_new.initialise(store, "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == "1.8.0"

    def test_an_install_set_up_before_this_existed_has_read_nothing_it_can_name(self, store):
        """It upgraded from a build that never recorded which notes it showed, so it cannot know its
        old version — only that setup was finished, which a fresh install's is not."""
        store.set("setup.completed", True)

        whats_new.initialise(store, "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == ""

    def test_a_restart_never_moves_what_was_already_recorded(self, store):
        """Boot runs this on every start. Re-recording would mark each upgrade read before anyone saw it."""
        store.set(whats_new.SEEN_KEY, "1.6.1")
        store.set("setup.completed", True)

        whats_new.initialise(store, "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == "1.6.1"


class TestPending:
    def test_nothing_is_pending_and_github_is_not_asked_when_this_version_was_read(self, store, fetches):
        store.set(whats_new.SEEN_KEY, "1.8.0")

        assert whats_new.pending(store, "1.8.0") == []
        assert fetches["n"] == 0

    def test_an_upgrade_shows_every_release_after_the_last_one_read_newest_first(self, store, fetches):
        store.set(whats_new.SEEN_KEY, "1.2.1")

        releases = whats_new.pending(store, "1.7.0")

        # Sorted by version, not GitHub's publish order; the version read and anything newer than the
        # running build are both left out.
        assert _versions(releases) == ["1.7.0", "1.6.1", "1.3.0"]
        assert releases[0] == _recorded_releases()[1]

    def test_an_install_that_cannot_name_its_old_version_sees_only_this_one(self, store, fetches):
        store.set(whats_new.SEEN_KEY, "")

        assert _versions(whats_new.pending(store, "1.7.0")) == ["1.7.0"]

    def test_a_downgrade_announces_nothing(self, store, fetches):
        store.set(whats_new.SEEN_KEY, "1.8.0")

        assert whats_new.pending(store, "1.7.0") == []

    def test_a_version_github_has_no_release_for_announces_nothing(self, store, fetches):
        """The image publishes before CI creates the release. Nothing to show yet, and nothing is
        marked read either, so the notes appear once the release exists."""
        store.set(whats_new.SEEN_KEY, "1.8.0")

        assert whats_new.pending(store, "1.9.0") == []
        assert store.get(whats_new.SEEN_KEY) == "1.8.0"

    def test_github_unreachable_announces_nothing(self, store, monkeypatch):
        monkeypatch.setattr(whats_new, "published_releases", lambda: [])
        store.set(whats_new.SEEN_KEY, "1.6.1")

        assert whats_new.pending(store, "1.8.0") == []


class TestMarkSeen:
    def test_closing_the_dialog_records_the_version_it_showed(self, store, fetches):
        store.set(whats_new.SEEN_KEY, "1.6.1")

        whats_new.mark_seen(store, "1.8.0", "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == "1.8.0"
        assert whats_new.pending(store, "1.8.0") == []

    def test_closing_a_stale_dialog_never_hides_a_newer_upgrade(self, store, fetches):
        """A tab left open overnight shows 1.7.0's notes while the server upgrades to 1.8.0 behind it.
        Closing that tab must record 1.7.0 — what the owner READ — not the version now running."""
        store.set(whats_new.SEEN_KEY, "1.6.1")

        whats_new.mark_seen(store, "1.7.0", "1.8.0")

        assert _versions(whats_new.pending(store, "1.8.0")) == ["1.8.0"]

    def test_closing_an_older_dialog_never_moves_the_record_backwards(self, store):
        """Two tabs: the newer one closed first. The older one closing after it must not re-open 1.8.0."""
        store.set(whats_new.SEEN_KEY, "1.8.0")

        whats_new.mark_seen(store, "1.7.0", "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == "1.8.0"

    @pytest.mark.parametrize("version", ["9.9.9", "not-a-version", ""])
    def test_a_version_this_build_cannot_have_shown_is_refused(self, store, version):
        """Recording a future version would silently swallow that release's notes when it arrives."""
        store.set(whats_new.SEEN_KEY, "1.6.1")

        with pytest.raises(ValueError):
            whats_new.mark_seen(store, version, "1.8.0")

        assert store.get(whats_new.SEEN_KEY) == "1.6.1"
