"""Transaction-owned row mutations and their durable consequence plans."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.server.api.row_changes import PRIVACY_SYNC, RECONCILE, RENAME, PlannedWork
from shortlist.server.db.models import Base, Collection
from shortlist.server.services.row_mutations import delete_row_in_session, steps_for_row_plan


def test_row_plan_becomes_one_ordered_closed_step_list():
    plan = [
        PlannedWork(RECONCILE, "row.audience", only_user_ids=[4, 8], in_sections=["2"]),
        PlannedWork(PRIVACY_SYNC, "row audience changed"),
        PlannedWork(RENAME, "row.rename", old_template="Old {user}", new_template="New {user}"),
    ]
    assert [step["kind"] for step in steps_for_row_plan(plan, slug="picked", build="per_person")] == [
        "row.reconcile",
        "privacy.sync",
        "row.rename",
    ]


def test_delete_row_is_rollback_safe_and_clears_local_anchors():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        gone = Collection(slug="gone", name="Gone", build="per_person")
        follower = Collection(slug="follower", name="Follower", build="per_person", hub_anchor={"1": {"row": "gone"}})
        session.add_all([gone, follower])
        session.commit()
        deleted = delete_row_in_session(session, gone.id)
        assert deleted.slug == "gone"
        assert session.get(Collection, gone.id) is None
        assert session.get(Collection, follower.id).hub_anchor == {}
        session.rollback()
    with Session(engine) as session:
        assert session.query(Collection).count() == 2
        assert session.query(Collection).filter_by(slug="follower").one().hub_anchor["1"]["row"] == "gone"
    engine.dispose()
