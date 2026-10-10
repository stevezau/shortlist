"""Clearing diagnostic runs must not erase the delivered rows used to credit watches."""

from datetime import UTC, datetime, timedelta

import pytest

from shortlist.engine.models import MediaType, Pick, UserRunReport
from shortlist.server.db.models import Collection, PickRow, Run, SharedRowWatch, User, WatchEvent
from shortlist.server.services.pick_history import DbPickHistory
from shortlist.server.services.run_persistence import (
    _persist_shared_row_report,
    _persist_user_report,
    live_pick_ids,
    reconcile_from_events,
)


def test_new_watch_and_current_picks_survive_clearing_runs(client):
    delivered = datetime.now(UTC) - timedelta(hours=2)
    with client.app.state.sessions() as session:
        user = session.query(User).first()
        row = Collection(slug="clear-watch", name="Clear watch", enabled=True)
        run = Run(trigger="manual", status="ok", started_at=delivered)
        session.add_all([row, run])
        session.flush()
        pick = Pick(
            collection_slug=row.slug,
            section_key="1",
            library="Movies",
            tmdb_id=90210,
            media_type=MediaType.MOVIE,
            rating_key=991,
            rank=1,
            title="Recorded pick",
            reason="Recorded delivery",
        )
        report = UserRunReport(username=user.username, slug=user.slug, status="ok", picks=[pick])
        report.breakdown = [
            {
                "row_slug": row.slug,
                "library_key": "1",
                "library_title": "Movies",
                "rating_key": 777,
                "delivered_at": delivered.isoformat(),
                "picks": [
                    {
                        "tmdb_id": 90210,
                        "media_type": "movie",
                        "rating_key": 991,
                        "rank": 1,
                        "title": "Recorded pick",
                        "reason": "Recorded delivery",
                    }
                ],
            }
        ]
        _persist_user_report(session, run.id, user, report, False)
        session.add(Collection(slug="clear-shared", name="Clear shared", enabled=True, build="shared"))
        shared = UserRunReport(username="Shared", slug="shared_clear-shared", status="ok")
        shared.breakdown = [
            {
                "row_slug": "clear-shared",
                "library_key": "1",
                "rating_key": 778,
                "delivered_at": delivered.isoformat(),
                "audience": None,
                "muted": [],
                "picks": [{"tmdb_id": 90211, "media_type": "movie", "rating_key": 992, "title": "Shared pick"}],
            }
        ]
        _persist_shared_row_report(session, run.id, shared, False)
        session.commit()
        persisted = session.query(PickRow).filter_by(run_id=run.id, user_id=user.id).one()
        user_id, account_id, pick_id = user.id, user.plex_account_id, persisted.id
        assert pick_id in live_pick_ids(session).get(user_id, set())

    before = client.get(f"/api/users/{user_id}/rows")
    assert before.status_code == 200
    before_row = next(row for row in before.json() if row["slug"] == "clear-watch")
    assert [pick["rating_key"] for pick in before_row["picks"]] == [991]
    before_preview = next(row for row in client.get("/api/collections").json() if row["slug"] == "clear-watch")
    assert before_preview["preview_titles"] == [{"rating_key": 991, "title": "Recorded pick"}]
    assert client.delete("/api/runs").status_code == 200
    after = client.get(f"/api/users/{user_id}/rows")
    assert after.status_code == 200
    after_row = next(row for row in after.json() if row["slug"] == "clear-watch")
    assert after_row["picks"] == before_row["picks"]
    after_preview = next(row for row in client.get("/api/collections").json() if row["slug"] == "clear-watch")
    assert after_preview["preview_titles"] == before_preview["preview_titles"]
    assert after_preview["last_run_id"] is None
    with client.app.state.sessions() as session:
        user_slug = session.get(User, user_id).slug
    assert DbPickHistory(client.app.state.sessions).latest(user_slug, "clear-watch") == {(MediaType.MOVIE, 90210)}
    with client.app.state.sessions() as session:
        assert pick_id in live_pick_ids(session).get(user_id, set())
        assert session.get(PickRow, pick_id).run_id is None
        session.add(
            WatchEvent(
                plex_account_id=account_id,
                rating_key=991,
                media_type="movie",
                viewed_at=delivered + timedelta(hours=1),
                source="history",
                history_key="after-clear",
            )
        )
        session.add(
            WatchEvent(
                plex_account_id=account_id,
                rating_key=992,
                media_type="movie",
                viewed_at=delivered + timedelta(hours=1),
                source="history",
                history_key="shared-after-clear",
            )
        )
        session.commit()

    assert reconcile_from_events(client.app.state.sessions) == 1
    with client.app.state.sessions() as session:
        assert session.get(PickRow, pick_id).watched_at is not None
        assert session.query(SharedRowWatch).filter_by(user_id=user_id, collection_slug="clear-shared").count() == 1
    report = client.get("/api/report?window=all")
    assert report.status_code == 200
    assert report.json()["overall"]["watched"] == 2
    person = next(person for person in client.get("/api/users").json() if person["id"] == user_id)
    assert person["last_pick_watched_at"] is not None


@pytest.mark.parametrize("status", ["queued", "running"])
def test_clear_history_waits_for_an_active_delivery(client, status):
    with client.app.state.sessions() as session:
        session.add(Run(trigger="manual", status=status))
        session.commit()

    response = client.delete("/api/runs")

    assert response.status_code == 409
    with client.app.state.sessions() as session:
        assert session.query(Run).filter_by(status=status).count() == 1
