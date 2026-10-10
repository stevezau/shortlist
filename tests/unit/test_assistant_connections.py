"""Browser credential handoffs expose presence only and remain bound to one grant."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.connections import ConnectionService
from shortlist.server.assistant_auth import AuthorizationDenied, Capability, GrantConstraints, GrantContext, GrantPreset
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.db.models import CacheRow, Server, Setting
from tests.db_helpers import create_schema, disposing_engine


@pytest.fixture
def connections(principal):
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        state = SimpleNamespace(
            sessions=sessionmaker(engine),
            secrets=None,
            assistant_auth=SimpleNamespace(oauth=SimpleNamespace(resource="https://example.test/shortlist/mcp")),
        )
        with state.sessions() as session:
            # These cases exercise connection cards after setup; fresh-wizard routing is covered separately.
            session.add(Setting(key="setup.completed", value={"v": True}))
            session.add(
                Server(
                    machine_id="connection-test", url="http://unused.invalid", token_enc="unused", owner_account_id=1
                )
            )
            session.add(
                AssistantGrant(
                    id=principal.grant_id,
                    owner_account_id=principal.owner_account_id,
                    client_id=principal.client_id,
                    name=principal.name,
                    preset=principal.preset.value,
                    capabilities=[cap.value for cap in principal.capabilities],
                    constraints=principal.constraints.as_dict(),
                    revision=1,
                )
            )
            session.commit()
        yield ConnectionService(state)


@pytest.fixture
def principal():
    return GrantContext(
        grant_id="grant",
        owner_account_id=1,
        client_id="client",
        name="Assistant",
        preset=GrantPreset.OWNER_AUTOMATION,
        capabilities=frozenset({Capability.CONNECTIONS_MANAGE}),
        constraints=GrantConstraints(),
        revision=1,
    )


def test_handoff_is_browser_only_and_status_never_contains_secret(connections, principal):
    flow = connections.start(principal, "tmdb")
    assert flow.data["browser_url"] == "https://example.test/shortlist/settings/connections#connection-tmdb"
    assert flow.data["configured"] is False
    with connections.state.sessions() as session:
        session.add(Setting(key="tmdb.apikey", value={"v": "never-return-this"}))
        session.commit()
    status = connections.status(principal, flow.data["flow_id"])
    assert status.data["status"] == "configuration_changed"
    assert status.data["configured"] is True
    assert status.data["connectivity_verified"] is False
    assert "never-return" not in status.model_dump_json()


def test_flow_id_does_not_authorize_other_connections_or_revisions(connections, principal):
    flow_id = connections.start(principal, "tmdb").data["flow_id"]
    for stranger in [
        replace(principal, grant_id="other"),
        replace(principal, revision=2),
        replace(principal, client_id="other"),
    ]:
        with pytest.raises(AuthorizationDenied):
            connections.status(stranger, flow_id)


def test_expiry_and_no_owner_api_token_handoff(connections, principal):
    flow_id = connections.start(principal, "tmdb").data["flow_id"]
    with connections.state.sessions() as session:
        session.get(CacheRow, ("assistant_connection", flow_id)).expires_at = 0
        session.commit()
    assert connections.status(principal, flow_id).data["status"] == "expired"
    with pytest.raises(ValueError):
        connections.start(principal, "api_token")


def test_handoff_requires_its_own_capability(connections, principal):
    with pytest.raises(AuthorizationDenied):
        connections.start(replace(principal, capabilities=frozenset({Capability.CONFIG_WRITE})), "tmdb")


def test_paid_and_notification_probes_require_browser_action(connections, principal, monkeypatch):
    monkeypatch.setattr(
        "shortlist.server.assistant.connections.probe_read_only_connection",
        lambda *args: pytest.fail("a paid or mutating probe was dispatched"),
    )
    for kind in ("curator", "exa", "notify"):
        assert connections.check(principal, kind).data["checked"] is False


def test_check_enforces_destination_and_never_returns_provider_error(connections, principal, monkeypatch):
    with connections.state.sessions() as session:
        session.add(Setting(key="tmdb.apikey", value={"v": "private-key"}))
        session.commit()
    connections.state.secrets = None
    monkeypatch.setattr(
        "shortlist.server.assistant.connections.SettingsStore",
        lambda *args: SimpleNamespace(get=lambda key: "private-key"),
    )
    with pytest.raises(AuthorizationDenied):
        connections.check(principal, "tmdb")
    permitted = replace(
        principal,
        revision=2,
        constraints=replace(principal.constraints, destination_ids=frozenset({"https://api.themoviedb.org/3"})),
    )
    with connections.state.sessions() as session:
        grant = session.get(AssistantGrant, principal.grant_id)
        grant.constraints = permitted.constraints.as_dict()
        grant.revision = 2
        session.commit()

    def failure(*args):
        raise RuntimeError("api_key=private-key")

    monkeypatch.setattr("shortlist.server.assistant.connections.probe_read_only_connection", failure)
    result = connections.check(permitted, "tmdb")
    assert result.data["ok"] is False
    assert "private-key" not in result.model_dump_json()


def test_probe_starts_real_snapshot_before_decryption_and_rechecks_revocation(connections, principal, monkeypatch):
    from datetime import UTC, datetime

    from sqlalchemy import event

    statements = []
    engine = connections.state.sessions.kw["bind"]

    def listener(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    with connections.state.sessions() as session:
        grant = session.get(AssistantGrant, principal.grant_id)
        grant.constraints = {**grant.constraints, "destination_ids": ["https://api.themoviedb.org/3"]}
        session.add(Setting(key="tmdb.apikey", value={"v": "private-key"}))
        session.commit()
    connections.state.secrets = None
    statements.clear()
    calls = []

    class Store:
        def __init__(self, session, _secrets):
            self.session = session

        def get(self, key):
            assert statements[0].strip().upper() == "BEGIN"
            assert self.session.connection().connection.driver_connection.in_transaction
            calls.append("decrypted")
            return "private-key"

    monkeypatch.setattr("shortlist.server.assistant.connections.SettingsStore", Store)
    monkeypatch.setattr(
        "shortlist.server.assistant.connections.probe_read_only_connection", lambda *args: calls.append("probe")
    )
    assert connections.check(principal, "tmdb").data["ok"] is True
    assert calls[-1] == "probe"
    with connections.state.sessions() as session:
        session.get(AssistantGrant, principal.grant_id).revoked_at = datetime.now(UTC)
        session.commit()
    calls.clear()
    with pytest.raises(AuthorizationDenied):
        connections.check(principal, "tmdb")
    assert calls == []
    event.remove(engine, "before_cursor_execute", listener)
