"""Month presets round-trip with exact windows and no private service calls."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from shortlist.engine import seasons


@pytest.mark.parametrize("preset_key", [preset.key for preset in seasons.PRESETS])
def test_every_preset_saves_its_defaults_and_only_removes_itself_from_the_catalogue(
    client: TestClient, preset_key: str
) -> None:
    offered = {preset["key"]: preset for preset in client.get("/api/seasons/presets").json()}
    preset = offered[preset_key]
    body = {
        key: value for key, value in preset.items() if key not in {"key", "label", "note", "category", "description"}
    }

    result = client.post("/api/seasons", json=body)

    assert result.status_code == 201, result.text
    saved = result.json()
    assert {key: saved[key] for key in body} == body
    assert saved["builtin"] is False
    assert saved["used_by"] == []
    listed = next(season for season in client.get("/api/seasons").json() if season["slug"] == saved["slug"])
    assert {key: listed[key] for key in body} == body
    assert {preset["key"] for preset in client.get("/api/seasons/presets").json()} == set(offered) - {preset_key}


def test_month_season_saves_normalised_and_reports_leap_year_windows(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("shortlist.server.services.context_builder.local_now", lambda: datetime(2028, 2, 15))
    result = client.post(
        "/api/seasons",
        json={
            "name": "February spotlight",
            "emoji": "🎬",
            "rule": {"kind": "month", "month": 2, "day": 29},
            "lead_days": 30,
            "after_days": 10,
            "picks": [{"tmdb_id": 11, "media_type": "movie", "title": "Star Wars", "year": 1977}],
        },
    )
    assert result.status_code == 201, result.text
    saved = result.json()
    assert (saved["lead_days"], saved["after_days"], saved["rule"]["day"]) == (0, 0, 1)
    expected = [{"start": "2028-02-01", "end": "2028-02-29"}, {"start": "2029-02-01", "end": "2029-02-28"}]
    assert saved["next_windows"] == expected
    assert saved["next_dates"] == ["2028-02-29", "2029-02-28"]
    listed = next(s for s in client.get("/api/seasons").json() if s["slug"] == saved["slug"])
    assert listed["next_windows"] == expected
    preview = client.post("/api/seasons/next-date", json={"kind": "month", "month": 2})
    assert preview.status_code == 200
    assert preview.json() == {"next_date": "2028-02-29", "rule_error": None, "next_windows": expected}


def test_curated_preset_sources_round_trip_without_network(client: TestClient) -> None:
    presets = client.get("/api/seasons/presets").json()
    assert len(presets) == 20
    curated = [p for p in presets if p["category"] != "holidays"]
    assert len(curated) == 10
    for preset in curated:
        assert preset["description"]
        assert preset["picks"] and not preset["tags"] and preset["genre"] is None
        assert all(p["title"] and p["year"] and p["media_type"] == "movie" for p in preset["picks"])
        engine = next(p for p in seasons.PRESETS if p.key == preset["key"])
        assert [(p["tmdb_id"], p["media_type"]) for p in preset["picks"]] == [
            (tmdb_id, media.value) for tmdb_id, media in engine.season.picks
        ]
        body = {k: v for k, v in preset.items() if k not in {"key", "label", "note", "category", "description"}}
        result = client.post("/api/seasons", json=body)
        assert result.status_code == 201, result.text
        assert result.json()["picks"] == preset["picks"]
    assert len(client.get("/api/seasons/presets").json()) == 10
