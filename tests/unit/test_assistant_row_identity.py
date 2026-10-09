"""A deleted row must never transfer its assistant authority to a replacement."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.api.collections import CollectionIn
from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.discovery import DiscoveryService
from shortlist.server.assistant.monitoring import MonitoringService
from shortlist.server.assistant.row_adapter import RowAdapter
from shortlist.server.assistant_auth import AuthorizationDenied, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import (
    Collection,
    Job,
    RowDeliverySnapshot,
    Run,
    RunSharedRow,
    RunUser,
    Server,
    User,
)
from shortlist.server.services.row_mutations import create_row_in_session, delete_row_in_session
from shortlist.server.services.secrets import SecretBox
from tests.db_helpers import create_schema, disposing_engine


@pytest.fixture
def row_identity_world(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'row-identity.db'}")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
        body = CollectionIn(
            name="Original private row",
            enabled=False,
            schedule="",
            audience="subset",
            audience_user_ids=[],
            media="movie",
            library_keys=["1"],
        )
        with sessions() as session:
            session.add(
                Server(machine_id="identity-test", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            original = create_row_in_session(session, state.secrets, body)
            original_id = original.id
            session.commit()
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"row-identity-regression-key-00000"))
        principal = repository.create_grant(
            owner_account_id=42,
            client_id="limited-row-client",
            name="Only the original row",
            preset=GrantPreset.MANAGE_SELECTED_ROWS,
            constraints=GrantConstraints(row_ids=frozenset({original_id}), library_keys=frozenset({"1"})),
        )
        yield SimpleNamespace(
            state=state,
            repository=repository,
            principal=principal,
            original_id=original_id,
            body=body,
            changes=ChangeService(sessions, {"row": RowAdapter(state)}),
            discovery=DiscoveryService(state),
        )


def _replace_row(world, *, same_configuration=False):
    with world.state.sessions() as session:
        delete_row_in_session(session, world.original_id)
        session.commit()
    body = world.body if same_configuration else world.body.model_copy(update={"name": "New unrelated private row"})
    with world.state.sessions() as session:
        replacement = create_row_in_session(session, world.state.secrets, body)
        replacement_id = replacement.id
        session.commit()
    return replacement_id


def test_deleted_row_grant_cannot_read_a_replacement(row_identity_world):
    world = row_identity_world
    assert world.discovery.row(world.principal, world.original_id).data["name"] == world.body.name
    replacement_id = _replace_row(world)
    current = world.repository.get_grant_context(world.principal.grant_id)

    with pytest.raises(AuthorizationDenied):
        world.discovery.row(current, replacement_id)


def test_deleted_row_grant_cannot_mutate_a_replacement(row_identity_world):
    world = row_identity_world
    replacement_id = _replace_row(world)
    current = world.repository.get_grant_context(world.principal.grant_id)
    plan = world.changes.prepare(current, "row", {"action": "update", "row_id": replacement_id, "values": {"size": 7}})

    with pytest.raises(ChangeError, match="owner approval"):
        world.changes.apply(current, plan["change_id"], "cannot-inherit-deleted-row-authority")
    with world.state.sessions() as session:
        assert session.get(Collection, replacement_id).size == world.body.size


def test_prepared_change_cannot_target_an_identical_replacement(row_identity_world):
    world = row_identity_world
    plan = world.changes.prepare(
        world.principal, "row", {"action": "update", "row_id": world.original_id, "values": {"size": 7}}
    )
    replacement_id = _replace_row(world, same_configuration=True)
    current = world.repository.get_grant_context(world.principal.grant_id)

    with pytest.raises((ChangeError, AuthorizationDenied, ValueError, HTTPException)):
        world.changes.apply(current, plan["change_id"], "cannot-apply-to-an-identical-replacement")
    with world.state.sessions() as session:
        assert session.get(Collection, replacement_id).size == world.body.size


@pytest.mark.parametrize("read", ["list", "report"])
@pytest.mark.parametrize("history", ["shared", "personal", "expected_only"])
def test_replacement_scope_does_not_inherit_prior_same_slug_history(row_identity_world, read, history):
    world = row_identity_world
    with world.state.sessions() as session:
        original = session.get(Collection, world.original_id)
        slug = original.slug
        run = Run(status="ok", trigger="manual", stats={"expected_rows": [{"slug": slug}]})
        session.add(run)
        session.flush()
        run_id = run.id
        if history == "shared":
            session.add(
                RunSharedRow(run_id=run_id, collection_slug=slug, status="ok", picks=[{"private": "prior row"}])
            )
        elif history == "personal":
            person = User(plex_account_id=12, username="Person", slug="person")
            session.add(person)
            session.flush()
            session.add(RunUser(run_id=run_id, user_id=person.id, status="skipped", rows_considered={slug: "not_due"}))
        session.commit()
    replacement_id = _replace_row(world, same_configuration=True)
    new_principal = world.repository.create_grant(
        owner_account_id=42,
        client_id="new-row-client",
        name="Replacement only",
        preset=GrantPreset.MANAGE_SELECTED_ROWS,
        constraints=GrantConstraints(row_ids=frozenset({replacement_id}), library_keys=frozenset({"1"})),
    )
    monitoring = MonitoringService(world.state)

    if read == "list":
        assert monitoring.runs(new_principal).data["items"] == []
    else:
        with pytest.raises(AuthorizationDenied):
            monitoring.run(new_principal, run_id)


def test_pending_assistant_cleanup_cannot_target_a_replacement_slug(row_identity_world):
    world = row_identity_world
    with world.state.sessions() as session:
        slug = session.get(Collection, world.original_id).slug
        session.add(
            Job(
                kind="assistant.converge",
                status="queued",
                payload={
                    "domain": "rows",
                    "steps": [
                        {
                            "kind": "row.reconcile",
                            "payload": {"slug": slug, "build": "per_person", "scope": "collection.delete"},
                        }
                    ],
                },
            )
        )
        session.commit()
    replacement_id = _replace_row(world, same_configuration=True)

    with world.state.sessions() as session:
        assert session.get(Collection, replacement_id).slug != slug


@pytest.mark.parametrize("case", ["done", "failed", "unrelated_slug", "unrelated_step"])
def test_terminal_or_unrelated_convergence_does_not_reserve_an_unused_slug(row_identity_world, case):
    world = row_identity_world
    with world.state.sessions() as session:
        slug = session.get(Collection, world.original_id).slug
        step = {"kind": "row.reconcile", "payload": {"slug": slug, "build": "per_person", "scope": "collection.delete"}}
        if case == "unrelated_slug":
            step["payload"]["slug"] = f"{slug}_other"
        elif case == "unrelated_step":
            step = {"kind": "privacy.sync", "payload": {"reason": slug}}
        session.add(
            Job(
                kind="assistant.converge",
                status=case if case in {"done", "failed"} else "queued",
                payload={"domain": "rows", "steps": [step]},
            )
        )
        session.commit()
    replacement_id = _replace_row(world, same_configuration=True)

    with world.state.sessions() as session:
        assert session.get(Collection, replacement_id).slug == slug


def test_retained_delivery_snapshot_reserves_the_deleted_row_slug(row_identity_world):
    world = row_identity_world
    with world.state.sessions() as session:
        slug = session.get(Collection, world.original_id).slug
        session.add(
            RowDeliverySnapshot(
                source_key="confirmed:former-row",
                collection_slug=slug,
                user_slug="",
                library_key="1",
                shared=True,
                rating_key=100,
                delivered_at=datetime.now(UTC),
                picks=[],
                audience=[],
            )
        )
        session.commit()
    replacement_id = _replace_row(world, same_configuration=True)

    with world.state.sessions() as session:
        assert session.get(Collection, replacement_id).slug != slug


@settings(max_examples=12)
@given(suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-\".'", min_size=1, max_size=15))
def test_personal_history_reservation_matches_exact_slug_keys(suffix):
    from shortlist.server.api.collections import _unique_slug

    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        sessions = sessionmaker(engine)
        slug = f"history_{suffix}"
        with sessions() as session:
            person = User(plex_account_id=12, username="Person", slug="person")
            run = Run(trigger="manual", status="ok")
            session.add_all([person, run])
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=person.id, rows_considered={slug: "not_due"}))
            session.commit()

            assert _unique_slug(session, slug) != slug
            assert _unique_slug(session, f"{slug}_unrelated") == f"{slug}_unrelated"
