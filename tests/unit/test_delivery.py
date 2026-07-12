from __future__ import annotations

from unittest.mock import MagicMock

from rowarr.engine.clients.plex import PlexClient
from rowarr.engine.delivery import deliver_row, render_row_name
from rowarr.engine.models import EngineConfig, Pick
from tests.conftest import make_profile


def picks(n: int = 2) -> list[Pick]:
    return [
        Pick(
            tmdb_id=i,
            rating_key=1000 + i,
            title=f"Movie {i}",
            rank=i,
            reason="Because you watched Fargo",
            seed_title="Fargo",
            seed_tmdb_id=900,
        )
        for i in range(1, n + 1)
    ]


class TestRenderRowName:
    def test_top_seed_substitution(self):
        assert render_row_name("Because you watched {top_seed}", make_profile(), picks()) == "Because you watched Fargo"

    def test_empty_template_falls_back(self):
        assert render_row_name("{top_seed}", make_profile(), [Pick(1, 1, "X", 1, "r")]) == "Picked for You"


class TestDeliverRow:
    def _plex(self) -> MagicMock:
        return MagicMock(spec=PlexClient)

    def test_creates_collection_when_missing(self, engine_config: EngineConfig):
        plex = self._plex()
        plex.find_owned_collection.return_value = None
        plex.stored_label.return_value = "Rowarr_sarah"
        section = MagicMock()

        diff, stored = deliver_row(plex, section, make_profile(), picks(), engine_config)

        assert diff.created is True
        assert diff.added == ["Movie 1", "Movie 2"]
        assert stored == "Rowarr_sarah"
        plex.fetch_items.assert_called_once_with([1001, 1002])
        create = plex.create_collection.call_args
        assert create.args[1] == "✨ Picked for You"
        label_call = plex.stored_label.call_args
        assert label_call.args[1] == "rowarr_sarah"
        # Promotion is the pipeline's job, AFTER filters are merged — never delivery's.
        plex.promote.assert_not_called()

    def test_updates_existing_collection_found_by_label_not_title(self, engine_config: EngineConfig):
        plex = self._plex()
        existing = MagicMock()
        existing.title = "Old Name"
        existing.items.return_value = [MagicMock(title="Movie 1"), MagicMock(title="Stale Movie")]
        plex.find_owned_collection.return_value = existing
        plex.stored_label.return_value = "Rowarr_sarah"

        diff, _ = deliver_row(plex, MagicMock(), make_profile(), picks(), engine_config)

        assert diff.created is False
        assert diff.added == ["Movie 2"]
        assert diff.removed == ["Stale Movie"]
        assert diff.kept == ["Movie 1"]
        existing.editTitle.assert_called_once_with("✨ Picked for You")
        plex.set_items.assert_called_once()

    def test_dry_run_makes_zero_writes(self, engine_config: EngineConfig):
        plex = self._plex()
        plex.find_owned_collection.return_value = None

        diff, stored = deliver_row(plex, MagicMock(), make_profile(), picks(), engine_config, dry_run=True)

        assert diff.created is True
        assert stored == "rowarr_sarah"  # requested form; nothing was written to read back
        plex.create_collection.assert_not_called()
        plex.set_items.assert_not_called()
        plex.stored_label.assert_not_called()
        plex.promote.assert_not_called()

    def test_per_user_template_override(self, engine_config: EngineConfig):
        plex = self._plex()
        plex.find_owned_collection.return_value = None
        plex.stored_label.return_value = "Rowarr_sarah"
        profile = make_profile(row_name_template="Sarah's Picks")

        deliver_row(plex, MagicMock(), profile, picks(), engine_config)

        assert plex.create_collection.call_args.args[1] == "Sarah's Picks"
