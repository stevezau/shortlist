"""The committed OpenAPI snapshot must match the app that is actually running.

`web/openapi.snapshot.json` is what `pnpm gen:api` falls back to when no backend is on :5959, so it
is the source the frontend's API types are generated from in CI and on a fresh clone. A snapshot that
has drifted from the routers is worse than no snapshot at all: hand-written types are visibly
hand-written, whereas a stale GENERATED type looks authoritative while describing an endpoint that
no longer exists.

Regenerate with:
    SHORTLIST_CONFIG=$(mktemp -d) python -c "import json,pathlib; \
        from shortlist.server.main import create_app; \
        pathlib.Path('web/openapi.snapshot.json').write_text( \
            json.dumps(create_app().openapi(), indent=2, sort_keys=True) + '\\n')"
    pnpm -C web gen:api
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[2] / "web" / "openapi.snapshot.json"


@pytest.fixture
def live_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTLIST_CONFIG", str(tmp_path))
    from shortlist.server.main import create_app

    return create_app().openapi()


class TestTheOpenApiSnapshotIsCurrent:
    def test_the_snapshot_exists(self):
        assert SNAPSHOT.exists(), f"{SNAPSHOT} is missing — `pnpm gen:api` has no offline source"

    def test_every_route_the_app_serves_is_in_the_snapshot(self, live_schema):
        """Paths are checked separately from the whole document so a drift names the endpoint.

        A whole-document equality failure prints two 76 KB blobs and tells you nothing.
        """
        snapshot = json.loads(SNAPSHOT.read_text())
        live_paths, snap_paths = set(live_schema["paths"]), set(snapshot["paths"])

        assert not live_paths - snap_paths, "endpoints exist but are NOT in the snapshot — regenerate it"
        assert not snap_paths - live_paths, "snapshot describes endpoints that no longer exist — regenerate it"

    def test_the_snapshot_matches_byte_for_byte(self, live_schema):
        """Catches a changed request/response SHAPE, which the path check above cannot see."""
        expected = json.dumps(live_schema, indent=2, sort_keys=True) + "\n"
        assert SNAPSHOT.read_text() == expected, (
            "the OpenAPI snapshot is stale — regenerate it (see this module's docstring)"
        )
