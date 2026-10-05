"""Run reports must never return unrelated outcomes or personal traces."""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.monitoring import MonitoringService
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantContext, GrantPreset
from shortlist.server.db.models import Base, Collection, Run, RunUser, User


def test_report_filters_people_rows_and_free_text():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions() as session:
        session.add_all(
            [
                User(id=1, plex_account_id=1, username="Allowed", slug="one"),
                User(id=2, plex_account_id=2, username="Other", slug="two"),
                Collection(id=1, slug="one", name="One"),
                Collection(id=2, slug="two", name="Two"),
                Run(id=1, trigger="manual", status="ok"),
            ]
        )
        session.flush()
        session.add_all(
            [
                RunUser(
                    run_id=1,
                    user_id=1,
                    rows_considered={"one": "due", "two": "not_due"},
                    error="Private failure text",
                    trace={"history": "Secret seed"},
                ),
                RunUser(run_id=1, user_id=2, rows_considered={"two": "due"}, trace={"history": "Other history"}),
            ]
        )
        session.commit()
    principal = GrantContext(
        "grant",
        1,
        "client",
        "Assistant",
        GrantPreset.INSPECT,
        frozenset({Capability.ACTIVITY_READ}),
        GrantConstraints(row_ids=frozenset({1}), person_ids=frozenset({1})),
        1,
    )
    result = MonitoringService(SimpleNamespace(sessions=sessions)).run(principal, 1)
    assert result.data["people"] == [
        {"person_id": 1, "status": "pending", "rows_considered": {"one": "due"}, "has_error": True}
    ]
    assert "Private failure" not in result.model_dump_json()
    assert "Secret seed" not in result.model_dump_json()
    assert "Other history" not in result.model_dump_json()
    engine.dispose()
