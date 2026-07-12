"""Privacy probe: full choreography with cleanup-in-finally guarantees."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from rowarr.engine.probe import PROBE_TITLE, run_privacy_probe
from tests.conftest import fake_media_item, make_profile, plextv_user

PROBE_ID = 777001


def _recording(inner, order: list[str], label: str):
    """Wrap a callable so the sequence of side effects can be asserted."""

    def wrapper(*args, **kwargs):
        order.append(label)
        if inner is not None:
            return inner(*args, **kwargs)
        return None

    return wrapper


def make_plex(hub_sequences: list[list[dict]]) -> MagicMock:
    """PlexClient mock whose user_hubs() returns each sequence element in turn (then repeats last)."""
    plex = MagicMock()
    section = MagicMock()
    section.type = "movie"
    plex.sections.return_value = [section]
    section.search.return_value = [fake_media_item(1, "Old A"), fake_media_item(2, "Old B")]
    collection = MagicMock()
    collection.ratingKey = PROBE_ID
    plex.create_collection.return_value = collection
    plex.stored_label.return_value = "Rowarr_probe"
    calls = {"n": 0}

    def hubs(token):
        i = min(calls["n"], len(hub_sequences) - 1)
        calls["n"] += 1
        return hub_sequences[i]

    plex.user_hubs.side_effect = hubs
    return plex


def probe_hub() -> dict:
    return {"title": PROBE_TITLE, "key": f"/library/collections/{PROBE_ID}/children"}


def make_plextv(mock_plextv):
    mock_plextv.users = [plextv_user(555000100, "canary")]
    mock_plextv.canary_server_token.return_value = "canary-tok"

    def put(account_id, fields):
        mock_plextv.users[0].filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put
    return mock_plextv


class TestPrivacyProbe:
    def test_happy_path_passes_and_cleans_up(self, mock_plextv, snapshot_store):
        plex = make_plex([[probe_hub()], []])  # visible at baseline, hidden after exclusion
        plextv = make_plextv(mock_plextv)
        canary = make_profile("canary", account_id=555000100)

        result = run_privacy_probe(plex, plextv, canary, snapshot_store, sleep=lambda s: None)

        assert result.passed
        assert result.detail["baseline_visible"] is True
        assert result.detail["t1_filter_persisted"] is True
        assert result.detail["hidden_after_exclusion"] is True
        # Cleanup: filters restored byte-identical, probe deleted.
        assert plextv.users[0].filters["filterMovies"] == ""
        plex.delete_owned_collection.assert_called_once()

    def test_snapshot_persisted_before_the_share_is_touched(self, mock_plextv, snapshot_store):
        """plex-safety rule 2: process death mid-probe must still be recoverable."""
        plex = make_plex([[probe_hub()], []])
        plextv = make_plextv(mock_plextv)
        canary = make_profile("canary", account_id=555000100)
        # A pre-existing foreign filter condition must be what we snapshot.
        plextv.users[0].filters["filterMovies"] = "contentRating!=R"
        order: list[str] = []
        snapshot_store.save = _recording(snapshot_store.save, order, "snapshot")
        plextv.update_user_filters.side_effect = _recording(
            plextv.update_user_filters.side_effect, order, "filter-write"
        )

        run_privacy_probe(plex, plextv, canary, snapshot_store, sleep=lambda s: None)

        assert order[0] == "snapshot", f"share was written before it was snapshotted: {order}"
        assert "filter-write" in order
        saved = snapshot_store.get(555000100)
        assert saved.filters["filterMovies"] == "contentRating!=R"  # true pre-probe state

    def test_existing_snapshot_is_never_overwritten(self, mock_plextv, snapshot_store):
        """A probe run after Rowarr is live must not clobber the real pre-Rowarr snapshot."""
        from datetime import UTC, datetime

        from rowarr.engine.models import FilterSnapshot

        original = FilterSnapshot(
            plex_account_id=555000100,
            username="canary",
            taken_at=datetime.now(UTC),
            filters={"filterMovies": "the-original-truth"},
        )
        snapshot_store.save(original)
        plex = make_plex([[probe_hub()], []])
        plextv = make_plextv(mock_plextv)

        run_privacy_probe(
            plex, plextv, make_profile("canary", account_id=555000100), snapshot_store, sleep=lambda s: None
        )

        assert snapshot_store.get(555000100).filters["filterMovies"] == "the-original-truth"

    def test_row_never_hides_fails_but_still_cleans_up(self, mock_plextv, snapshot_store):
        plex = make_plex([[probe_hub()], [probe_hub()]])  # stays visible forever
        plextv = make_plextv(mock_plextv)
        canary = make_profile("canary", account_id=555000100)

        result = run_privacy_probe(
            plex, plextv, canary, snapshot_store, hidden_timeout_s=0.01, poll_interval_s=0, sleep=lambda s: None
        )

        assert not result.passed
        assert plextv.users[0].filters["filterMovies"] == ""  # restored despite failure
        plex.delete_owned_collection.assert_called_once()

    def test_baseline_never_visible_fails_without_touching_filters(self, mock_plextv, snapshot_store):
        plex = make_plex([[]])  # promotion apparently broken
        plextv = make_plextv(mock_plextv)
        canary = make_profile("canary", account_id=555000100)

        result = run_privacy_probe(
            plex, plextv, canary, snapshot_store, visible_timeout_s=0.01, poll_interval_s=0, sleep=lambda s: None
        )

        assert not result.passed
        assert "promotion" in result.detail["error"]
        plextv.update_user_filters.assert_not_called()
        plex.delete_owned_collection.assert_called_once()  # probe still cleaned up

    def test_no_movie_library_short_circuits(self, snapshot_store):
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(type="artist")]
        result = run_privacy_probe(plex, MagicMock(), make_profile(), snapshot_store, sleep=lambda s: None)
        assert not result.passed
        assert "no movie library" in result.detail["error"]
