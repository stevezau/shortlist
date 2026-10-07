"""End-to-end MCP SDK matrix against only disposable loopback dependencies.

The matrix deliberately calls tools through Streamable HTTP instead of importing tool
functions.  It is the contract that a desktop MCP host uses: authentication, Pydantic
validation, policy, plans, durable operations and workers are all live.  PMS, plex.tv,
TMDB and paid/acquisition boundaries are the only fakes.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from fastapi import FastAPI, Request
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import shortlist
from shortlist.engine.delivery import row_marker
from shortlist.server.assistant.operation_models import AssistantChange
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.catalogs.settings import get_settings_catalog
from shortlist.server.db.models import (
    CacheRow,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    Event,
    Job,
    RequestCandidate,
    Run,
    Server,
    Theme,
    User,
)
from shortlist.server.main import create_app
from shortlist.server.settings_store import SettingsStore
from tests.e2e.conftest import OWNER_ACCOUNT_ID, PMS_VERSION, _make_fake_tmdb
from tests.fakes.fake_plex import FakeCollection, FakePlexState, make_fake_plex, make_fake_plextv, seed_state

pytestmark = pytest.mark.integration


REGISTERED_TOOLS = frozenset(
    {
        "shortlist_get_instance",
        "shortlist_get_setup_status",
        "shortlist_start_connection",
        "shortlist_get_connection_status",
        "shortlist_check_connection",
        "shortlist_get_guide",
        "shortlist_describe_settings",
        "shortlist_get_configuration",
        "shortlist_get_choices",
        "shortlist_list_people",
        "shortlist_get_person_row_settings",
        "shortlist_list_libraries",
        "shortlist_list_templates",
        "shortlist_list_rows",
        "shortlist_get_row",
        "shortlist_list_themes",
        "shortlist_get_theme",
        "shortlist_list_seasons",
        "shortlist_search_titles",
        "shortlist_plan_configuration",
        "shortlist_plan_theme",
        "shortlist_plan_setup",
        "shortlist_generate_theme",
        "shortlist_plan_row",
        "shortlist_plan_requests",
        "shortlist_plan_maintenance",
        "shortlist_plan_schedule",
        "shortlist_plan_run",
        "shortlist_preview_row",
        "shortlist_plan_people",
        "shortlist_plan_season",
        "shortlist_get_change",
        "shortlist_apply_change",
        "shortlist_get_operation",
        "shortlist_cancel_operation",
        "shortlist_list_runs",
        "shortlist_get_run_report",
        "shortlist_get_activity",
        "shortlist_list_requests",
        "shortlist_diagnose",
    }
)

# Every entry is invoked through ``McpWire.call`` below.  Keeping this inventory beside
# the cases makes a newly registered tool fail the matrix rather than silently escaping it.
EXERCISED_BY_GROUP = {
    "person_row_settings": {"shortlist_get_person_row_settings"},
    "choices": {"shortlist_get_choices"},
    "discovery": {
        "shortlist_get_instance",
        "shortlist_get_setup_status",
        "shortlist_start_connection",
        "shortlist_get_connection_status",
        "shortlist_check_connection",
        "shortlist_get_guide",
        "shortlist_describe_settings",
        "shortlist_get_configuration",
        "shortlist_list_people",
        "shortlist_list_libraries",
        "shortlist_list_templates",
        "shortlist_list_rows",
        "shortlist_get_row",
        "shortlist_list_themes",
        "shortlist_get_theme",
        "shortlist_list_seasons",
        "shortlist_search_titles",
        "shortlist_diagnose",
    },
    "plans": {
        "shortlist_plan_configuration",
        "shortlist_plan_theme",
        "shortlist_plan_setup",
        "shortlist_generate_theme",
        "shortlist_plan_row",
        "shortlist_plan_requests",
        "shortlist_plan_maintenance",
        "shortlist_plan_schedule",
        "shortlist_plan_run",
        "shortlist_preview_row",
        "shortlist_plan_people",
        "shortlist_plan_season",
        "shortlist_get_change",
        "shortlist_apply_change",
    },
    "durable_operations": {
        "shortlist_get_operation",
        "shortlist_cancel_operation",
        "shortlist_list_runs",
        "shortlist_get_run_report",
        "shortlist_get_activity",
        "shortlist_list_requests",
    },
}


class _ThreadedServer(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.port = port

    def run(self) -> None:
        self.server.run()

    def wait_until_up(self, path: str, timeout_s: float = 20) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}{path}", timeout=1).raise_for_status()
                return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise AssertionError(f"loopback server on port {self.port} did not become ready")

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=10)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class McpWire:
    url: str
    credential: str
    calls: set[str] = field(default_factory=set)

    def schemas(self) -> dict[str, dict]:
        """Read the actual public tools/list schema clients use to construct nested requests."""

        async def run() -> dict[str, dict]:
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.credential}"}) as http,
                Client(streamable_http_client(self.url, http_client=http), mode="auto") as session,
            ):
                listed = await session.list_tools()
                return {tool.name: tool.input_schema for tool in listed.tools}

        return asyncio.run(run())

    def exercise(self, cases: list[tuple[str, dict | None]]) -> dict[str, dict]:
        """Make real SDK calls and return only structured tool data."""

        async def run() -> dict[str, dict]:
            headers = {"Authorization": f"Bearer {self.credential}"}
            async with (
                httpx2.AsyncClient(headers=headers) as http,
                Client(streamable_http_client(self.url, http_client=http), mode="auto") as session,
            ):
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == REGISTERED_TOOLS
                results = {}
                for name, request in cases:
                    result = await session.call_tool(name, {} if request is None else {"request": request})
                    assert not result.is_error, (name, result.content)
                    assert result.structured_content is not None, name
                    self.calls.add(name)
                    results[name] = result.structured_content
                return results

        return asyncio.run(run())

    def expect_error(self, name: str, request: dict | None, *, code: str | None = None) -> dict:
        """Make a schema/policy-negative SDK call without bypassing MCP error framing."""

        async def run() -> dict:
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.credential}"}) as http,
                Client(streamable_http_client(self.url, http_client=http), mode="auto") as session,
            ):
                result = await session.call_tool(name, {} if request is None else {"request": request})
                assert result.is_error, (name, result.structured_content)
                if code is not None:
                    assert any(f'"code": "{code}"' in getattr(block, "text", "") for block in result.content), (
                        result.content
                    )
                return result.structured_content or {}

        return asyncio.run(run())


@contextmanager
def _wire_app(
    tmp_path: Path,
    monkeypatch,
    *,
    configured_provider: bool = True,
    capabilities: frozenset[Capability] = ASSISTANT_CAPABILITIES,
) -> Iterator[tuple[McpWire, object, FakePlexState]]:
    """Boot a fresh real app with loopback-only service boundaries."""

    assert Path(shortlist.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    state = seed_state()
    pms = _ThreadedServer(make_fake_plex(state), _port())
    plex_tv = _ThreadedServer(make_fake_plextv(state), _port())
    tmdb = _ThreadedServer(_make_fake_tmdb(state), _port())
    for server, health in ((pms, "/identity"), (plex_tv, "/api/users"), (tmdb, "/configuration")):
        server.start()
        server.wait_until_up(health)
    pms_url = f"http://127.0.0.1:{pms.port}"
    plex_tv_url = f"http://127.0.0.1:{plex_tv.port}"
    tmdb_url = f"http://127.0.0.1:{tmdb.port}"
    state.pms_url = pms_url
    monkeypatch.setattr("shortlist.engine.clients.plextv.PLEXTV", plex_tv_url)
    monkeypatch.setattr("shortlist.server.auth.PLEXTV", plex_tv_url)
    monkeypatch.setattr("shortlist.server.api.setup.PLEXTV", plex_tv_url)
    monkeypatch.setattr("shortlist.server.services.setup_probe.PLEXTV", plex_tv_url)
    monkeypatch.setattr("shortlist.engine.clients.tmdb.API", tmdb_url)
    from shortlist.server.assistant import generation

    first_movie = next(iter(state.movies.values()))
    provider_calls: list[dict] = []

    class _FakeProvider:
        name = "loopback-fake"
        can_complete = True
        last_tokens = 17

        def complete(self, _system: str, _user: str, *, max_tokens: int | None = None) -> str:
            provider_calls.append({"max_tokens": max_tokens})
            return (
                '{"name":"SDK generated","emoji":"S","rules":{},"tags":[],"genres":["Drama"],'
                f'"titles":[{{"title":"{first_movie.title}","year":{first_movie.year},"media":"movie"}}]}}'
            )

    # The provider class is the external boundary.  Its job, reservation and response
    # checkpoint remain real; it has no network route other than the local fake suite.
    monkeypatch.setattr(generation, "make_curator", lambda _provider, **_kwargs: _FakeProvider())

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    url = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setenv("SHORTLIST_MCP_URL", url)
    monkeypatch.delenv("APP_BASE_PATH", raising=False)
    app = create_app(config_dir=tmp_path)
    app.state.matrix_provider_calls = provider_calls
    app_server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    app_thread = threading.Thread(target=lambda: app_server.run(sockets=[sock]), daemon=True)
    app_thread.start()
    deadline = time.monotonic() + 15
    while not app_server.started and app_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert app_server.started, "the disposable MCP app did not start"
    try:
        with app.state.sessions() as session:
            store = SettingsStore(session, app.state.secrets)
            store.set("plex.url", pms_url)
            store.set("plex.token", "synthetic-owner-token")
            store.set("tmdb.apikey", "synthetic-tmdb-key")
            if configured_provider:
                store.set("curator.provider", "openai")
                store.set("curator.api_key", "synthetic-provider-key")
            store.set("setup.completed", True)
            session.add(
                Server(
                    machine_id=state.machine_id,
                    url=pms_url,
                    token_enc=app.state.secrets.encrypt("synthetic-owner-token"),
                    name="Synthetic Plex",
                    version=PMS_VERSION,
                    owner_account_id=OWNER_ACCOUNT_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            for fake_user in state.users.values():
                session.add(
                    User(
                        plex_account_id=fake_user.id,
                        username=fake_user.username,
                        slug=fake_user.username.lower(),
                        user_type="managed" if fake_user.home else "shared",
                        restricted=fake_user.home,
                        restriction_profile=fake_user.restriction_profile,
                        enabled=True,
                    )
                )
            session.flush()
            first_person = session.query(User).order_by(User.id).first()
            session.add(Theme(id=20, slug="matrix-theme", name="Matrix theme", media=["movie"], genres=["Drama"]))
            session.flush()
            session.add(
                Collection(
                    id=10,
                    slug="matrix-row",
                    name="Matrix row",
                    audience="subset",
                    library_keys=["1"],
                    enabled=False,
                    theme_id=20,
                )
            )
            session.add(CollectionAudience(collection_id=10, user_id=first_person.id))
            session.commit()
        repository = app.state.assistant_auth.repository
        grant = repository.create_grant(
            owner_account_id=OWNER_ACCOUNT_ID,
            client_id="mcp-wire-matrix",
            name="Disposable MCP wire matrix",
            preset=GrantPreset.OWNER_AUTOMATION,
            capabilities=capabilities,
            constraints=GrantConstraints(
                destination_ids=frozenset({"https://api.openai.com/v1", "https://api.themoviedb.org/3"}),
                setting_groups=frozenset(item.group.value for item in get_settings_catalog()),
                include_future_rows=True,
                include_future_people=True,
                include_future_libraries=True,
                max_provider_calls=4,
            ),
        )
        credential = repository.issue_local_credential(grant.grant_id).take()
        yield McpWire(url=url, credential=credential), app, state
    finally:
        app_server.should_exit = True
        app_thread.join(timeout=10)
        sock.close()
        for server in (tmdb, plex_tv, pms):
            server.stop()


def _assert_inventory() -> None:
    assert set().union(*EXERCISED_BY_GROUP.values()) == REGISTERED_TOOLS


def _idempotency(name: str) -> str:
    return f"mcp-wire-matrix-{name}".replace("_", "-")


def _apply(wire: McpWire, change_id: str, name: str) -> dict:
    return wire.exercise([("shortlist_apply_change", {"change_id": change_id, "idempotency_key": _idempotency(name)})])[
        "shortlist_apply_change"
    ]["data"]


def _approve_owner_change(wire: McpWire, app, change_id: str) -> None:
    cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID})
    with httpx.Client(
        base_url=wire.url.removesuffix("/mcp"),
        cookies={SESSION_COOKIE: cookie},
        headers={CSRF_HEADER: "1"},
    ) as owner:
        approved = owner.post(f"/api/assistant/changes/{change_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["authorization"] == {"can_apply": True, "approved": True}


def test_mcp_sdk_discovery_connection_and_catalogue_matrix(tmp_path, monkeypatch):
    """Every read/connection tool reaches the real service through SDK framing."""
    _assert_inventory()
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        with app.state.sessions() as session:
            SettingsStore(session, app.state.secrets).set("recommendations.max_seeds", 13)
            session.commit()
            expected_people = {person.id for person in session.query(User).all()}
        movie = next(iter(state.movies.values()))
        first = wire.exercise([("shortlist_start_connection", {"service": "tmdb"})])
        flow_id = first["shortlist_start_connection"]["data"]["flow_id"]
        handoff = first["shortlist_start_connection"]["data"]
        assert handoff["service"] == "tmdb" and handoff["configured"] is True
        assert handoff["browser_url"] == wire.url.removesuffix("/mcp") + "/settings/connections#connection-tmdb"
        assert handoff["connectivity_verified"] is False
        with app.state.sessions() as session:
            assert session.get(CacheRow, ("assistant_connection", flow_id)).value["service"] == "tmdb"
        results = wire.exercise(
            [
                ("shortlist_get_instance", None),
                ("shortlist_get_setup_status", None),
                ("shortlist_get_connection_status", {"flow_id": flow_id}),
                ("shortlist_check_connection", {"service": "tmdb"}),
                ("shortlist_get_guide", {"topic": "rows"}),
                ("shortlist_describe_settings", {"group": "recommendations", "limit": 100}),
                ("shortlist_get_configuration", {"group": "recommendations"}),
                ("shortlist_list_people", {"limit": 100}),
                ("shortlist_list_libraries", None),
                ("shortlist_list_templates", None),
                ("shortlist_list_rows", {"limit": 100}),
                ("shortlist_get_row", {"id": 10}),
                ("shortlist_list_themes", {"limit": 100}),
                ("shortlist_get_theme", {"id": 20}),
                ("shortlist_list_seasons", None),
                ("shortlist_search_titles", {"query": movie.title, "media": "movie", "year": movie.year}),
                ("shortlist_diagnose", {"topic": "row", "row_id": 10}),
            ]
        )
        data = {name: result["data"] for name, result in results.items()}
        instance = data["shortlist_get_instance"]
        assert instance["connection_name"] == "Disposable MCP wire matrix"
        assert set(instance["capabilities"]) == {capability.value for capability in ASSISTANT_CAPABILITIES}
        assert instance["provider_call_quota"]["remaining"] == 4
        with app.state.sessions() as session:
            assert session.get(CacheRow, ("assistant_connection", flow_id)).value["grant_id"] == instance["grant_id"]
        setup = data["shortlist_get_setup_status"]
        assert setup["ready"] is True
        assert {check["id"] for check in setup["checks"] if check["ready"]} == {"plex_ownership", "metadata"}
        connection = data["shortlist_get_connection_status"]
        assert connection["service"] == "tmdb" and connection["status"] == "waiting_for_owner"
        assert connection["configured"] is True and connection["connectivity_verified"] is False
        checked = data["shortlist_check_connection"]
        assert checked["service"] == "tmdb" and checked["ok"] is True and checked["checked"] is True
        assert checked["generation_performed"] is False
        guide = data["shortlist_get_guide"]
        assert guide["topic"] == "rows" and guide["title"] == "Design a recommendation row"
        assert any("disabled" in step for step in guide["steps"])
        catalog = data["shortlist_describe_settings"]["items"]
        assert catalog and all(setting["group"] == "recommendations" for setting in catalog)
        assert any(
            setting["key"] == "recommendations.max_seeds" and setting["assistant_writable"] for setting in catalog
        )
        configuration = data["shortlist_get_configuration"]
        assert configuration["group"] == "recommendations"
        assert configuration["values"]["recommendations.max_seeds"]["value"] == 13
        assert configuration["values"]["recommendations.max_seeds"]["source"] == "saved"
        people = data["shortlist_list_people"]["items"]
        assert {person["id"] for person in people} == expected_people
        assert {person["name"] for person in people} == {person.username for person in state.users.values()}
        libraries = data["shortlist_list_libraries"]["items"]
        assert {(item["key"], item["title"], item["type"]) for item in libraries} == {
            (str(section.key), section.title, section.type) for section in state.sections.values()
        }
        templates = data["shortlist_list_templates"]
        assert {"picked-for-you", "describe-a-row"} <= {item["id"] for item in templates["items"]}
        assert {"ai_paused", "theme_mode", "candidate_sources"} <= templates["field_definitions"].keys()
        assert templates["creation_defaults"]["enabled"] is False
        rows = data["shortlist_list_rows"]["items"]
        assert any(row["id"] == 10 and row["slug"] == "matrix-row" and row["enabled"] is False for row in rows)
        row = data["shortlist_get_row"]
        assert row["id"] == 10 and row["name"] == "Matrix row" and row["fields"]["theme_id"] == 20
        assert row["audience_user_ids"] == [min(expected_people)]
        assert any(
            theme["id"] == 20 and theme["name"] == "Matrix theme" for theme in data["shortlist_list_themes"]["items"]
        )
        theme = data["shortlist_get_theme"]
        assert theme["id"] == 20 and theme["name"] == "Matrix theme"
        assert theme["media"] == ["movie"] and theme["genres"] == ["Drama"]
        assert any(
            season["slug"] == "halloween" and season["movie_genres"]
            for season in data["shortlist_list_seasons"]["items"]
        )
        search = data["shortlist_search_titles"]
        assert any(
            item == {"tmdb_id": movie.tmdb_id, "title": movie.title, "year": movie.year, "media": "movie"}
            for item in search["items"]
        )
        assert search["verification_lifetime_seconds"] > 0
        diagnosis = data["shortlist_diagnose"]
        assert diagnosis["row"]["id"] == 10 and "The row is disabled." in diagnosis["checks"]
    assert EXERCISED_BY_GROUP["discovery"] <= wire.calls


def test_mcp_sdk_configured_choices_read_real_loopback_service_shapes(tmp_path, monkeypatch):
    """Plex and both Arr choice families cross SDK framing and their real HTTP clients."""
    arr = FastAPI()
    upstream_requests = []

    @arr.get("/health")
    def health():
        return {"ok": True}

    @arr.get("/{service}/api/v3/{endpoint}")
    def options(service: str, endpoint: str, request: Request):
        upstream_requests.append((request.method, request.url.path, request.headers.get("X-Api-Key")))
        assert service in {"radarr", "sonarr"}
        if endpoint == "qualityprofile":
            return [{"id": 7, "name": f"{service} HD", "ignored_vendor_field": "not public"}]
        assert endpoint == "rootfolder"
        return [{"id": 9, "path": f"/media/{service}", "ignored_vendor_field": "not public"}]

    boundary = _ThreadedServer(arr, _port())
    boundary.start()
    boundary.wait_until_up("/health")
    try:
        with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
            state.collections[8801] = FakeCollection(
                rating_key=8801, title="SDK foreign shelf", section_id=1, promoted_recommended=True
            )
            state.collections[8802] = FakeCollection(
                rating_key=8802, title="SDK private row" + row_marker(9), section_id=1, promoted_recommended=True
            )
            choices = wire.exercise([("shortlist_get_choices", {"kind": "plex_anchors", "library_key": "1"})])[
                "shortlist_get_choices"
            ]["data"]
            assert choices["kind"] == "plex_anchors"
            assert {"kind": "plex_anchor", "title": "SDK foreign shelf", "on_shelf": True} in choices["items"]
            assert all("SDK private row" not in item["title"] for item in choices["items"])

            urls = {kind: f"http://127.0.0.1:{boundary.port}/{kind}" for kind in ("radarr", "sonarr")}
            with app.state.sessions() as session:
                store = SettingsStore(session, app.state.secrets)
                for kind, url in urls.items():
                    store.set(f"requests.{kind}.url", url)
                    store.set(f"requests.{kind}.apikey", f"synthetic-{kind}-key")
                session.commit()
            repository = app.state.assistant_auth.repository
            grant = repository.find_grant_for_client("mcp-wire-matrix")
            repository.replace_grant_authority(
                grant.grant_id,
                capabilities=grant.capabilities,
                constraints=replace(
                    grant.constraints, destination_ids=grant.constraints.destination_ids | frozenset(urls.values())
                ),
                expected_revision=grant.revision,
            )
            for kind in urls:
                choice = wire.exercise([("shortlist_get_choices", {"kind": kind})])["shortlist_get_choices"]["data"]
                assert choice["kind"] == kind and choice["next_offset"] is None
                assert choice["items"] == [
                    {"kind": "quality_profile", "id": 7, "name": f"{kind} HD"},
                    {"kind": "root_folder", "id": 9, "path": f"/media/{kind}"},
                ]
            assert upstream_requests == [
                ("GET", f"/{kind}/api/v3/{endpoint}", f"synthetic-{kind}-key")
                for kind in ("radarr", "sonarr")
                for endpoint in ("qualityprofile", "rootfolder")
            ]
            wire.expect_error("shortlist_get_choices", {"kind": "radarr", "library_key": "1"})
            wire.expect_error("shortlist_get_choices", {"kind": "unknown"})
            assert len(upstream_requests) == 4
        assert EXERCISED_BY_GROUP["choices"] <= wire.calls
    finally:
        boundary.stop()


def test_mcp_sdk_plan_apply_and_real_worker_matrix(tmp_path, monkeypatch):
    """Plans are reread and applied through the durable service, never a fake operation id."""
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        catalog = wire.exercise(
            [
                ("shortlist_list_people", {"limit": 100}),
                ("shortlist_list_libraries", None),
                ("shortlist_list_templates", None),
                ("shortlist_list_seasons", None),
            ]
        )
        person_id = catalog["shortlist_list_people"]["data"]["items"][0]["id"]
        library_key = catalog["shortlist_list_libraries"]["data"]["items"][0]["key"]
        template_id = next(
            item["id"]
            for item in catalog["shortlist_list_templates"]["data"]["items"]
            if item["id"] == "picked-for-you"
        )
        season_genre = next(
            genre for season in catalog["shortlist_list_seasons"]["data"]["items"] for genre in season["movie_genres"]
        )
        planned = wire.exercise(
            [
                (
                    "shortlist_plan_row",
                    {
                        "action": "create",
                        "template_id": template_id,
                        "values": {
                            "name": "SDK matrix row",
                            "enabled": False,
                            "schedule": "",
                            "audience": "subset",
                            "audience_user_ids": [person_id],
                            "library_keys": [library_key],
                            "hub_anchor": {library_key: {"row": "matrix-row", "enabled": True}},
                            "ai_instructions": {"mode": "add", "text": "Prefer quiet comedies."},
                        },
                    },
                )
            ]
        )["shortlist_plan_row"]["data"]
        reread = wire.exercise([("shortlist_get_change", {"change_id": planned["change_id"]})])
        assert reread["shortlist_get_change"]["data"]["kind"] == "row"
        created = _apply(wire, planned["change_id"], "create-row")
        row_id = created["result"]["row_id"]
        saved_row = wire.exercise([("shortlist_get_row", {"id": row_id})])["shortlist_get_row"]["data"]
        assert saved_row["id"] == row_id
        assert saved_row["fields"]["hub_anchor"] == {
            library_key: {"anchor": "", "row": "matrix-row", "before": False, "top": False, "enabled": True}
        }
        assert saved_row["fields"]["avoid_rows"] is None
        assert saved_row["fields"]["ai_instructions"] == {"mode": "add", "text": "Prefer quiet comedies."}
        assert saved_row["fields"]["ai_paused"] is False

        with app.state.sessions() as session:
            session.add(
                RequestCandidate(
                    tmdb_id=990001,
                    media_type="movie",
                    title="Matrix request",
                    row_slug="matrix-row",
                    status="pending",
                )
            )
            session.commit()
        setup_request = {
            "theme": {"name": "SDK bundle", "media": ["movie"], "genres": ["Drama"]},
            "row": {
                "action": "create",
                "template_id": template_id,
                "values": {
                    "name": "SDK bundled row",
                    "audience": "subset",
                    "audience_user_ids": [person_id],
                    "library_keys": [library_key],
                    "enabled": False,
                    "schedule": "",
                },
            },
        }
        stale_setup = wire.exercise([("shortlist_plan_setup", setup_request)])["shortlist_plan_setup"]["data"]
        # Changes are deliberately prepared immediately before their success-path
        # apply.  A configuration mutation invalidates prior dependent plans; the
        # one stale setup below proves that boundary rather than masking it.
        configuration_plan = wire.exercise(
            [("shortlist_plan_configuration", {"values": {"recommendations.max_seeds": 16}})]
        )["shortlist_plan_configuration"]["data"]
        wire.expect_error(
            "shortlist_plan_season",
            {"action": "create", "definition": {"name": "No sources", "emoji": "N", "rule": {"kind": "fixed"}}},
        )
        configuration = _apply(wire, configuration_plan["change_id"], "configuration")
        assert "recommendations.max_seeds" in configuration["result"]["changed_keys"]
        saved = wire.exercise([("shortlist_get_configuration", {"group": "recommendations"})])
        assert saved["shortlist_get_configuration"]["data"]["values"]["recommendations.max_seeds"]["value"] == 16
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": stale_setup["change_id"], "idempotency_key": _idempotency("stale-setup")},
        )
        theme_plan = wire.exercise(
            [
                (
                    "shortlist_plan_theme",
                    {"action": "create", "draft": {"name": "SDK drama", "media": ["movie"], "genres": ["Drama"]}},
                )
            ]
        )["shortlist_plan_theme"]["data"]
        theme = _apply(wire, theme_plan["change_id"], "theme")
        assert theme["result"]["theme_id"] > 0
        saved_theme = wire.exercise([("shortlist_get_theme", {"id": theme["result"]["theme_id"]})])[
            "shortlist_get_theme"
        ]["data"]
        assert saved_theme["name"] == "SDK drama" and saved_theme["genres"] == ["Drama"]
        setup_plan = wire.exercise([("shortlist_plan_setup", setup_request)])["shortlist_plan_setup"]["data"]
        setup = _apply(wire, setup_plan["change_id"], "setup")
        assert setup["result"]["row_id"] > 0
        request_plan = wire.exercise([("shortlist_plan_requests", {"action": "reject", "candidate_ids": [1]})])[
            "shortlist_plan_requests"
        ]["data"]
        request = _apply(wire, request_plan["change_id"], "request-reject")
        assert {key: request["result"][key] for key in ("action", "changed", "candidate_ids")} == {
            "action": "reject",
            "changed": 1,
            "candidate_ids": [1],
        }
        with app.state.sessions() as session:
            assert session.get(RequestCandidate, 1).status == "rejected"
        season_plan = wire.exercise(
            [
                (
                    "shortlist_plan_season",
                    {
                        "action": "create",
                        "definition": {
                            "name": "SDK day",
                            "emoji": "S",
                            "rule": {"kind": "fixed", "month": 1, "day": 2},
                            "genre": season_genre,
                        },
                    },
                )
            ]
        )["shortlist_plan_season"]["data"]
        season = _apply(wire, season_plan["change_id"], "season")
        assert season["result"]["action"] == "create"
        saved_season = next(
            item
            for item in wire.exercise([("shortlist_list_seasons", None)])["shortlist_list_seasons"]["data"]["items"]
            if item["slug"] == season["result"]["slug"]
        )
        assert saved_season["name"] == "SDK day" and saved_season["movie_genres"] == [season_genre]
        assert {key: saved_season["rule"][key] for key in ("kind", "month", "day")} == {
            "kind": "fixed",
            "month": 1,
            "day": 2,
        }
        people_plan = wire.exercise(
            [("shortlist_plan_people", {"person_id": person_id, "patch": {"nickname": "SDK"}})]
        )["shortlist_plan_people"]["data"]
        people = _apply(wire, people_plan["change_id"], "people")
        assert people["result"]["person_id"] == person_id
        saved_person = next(
            item
            for item in wire.exercise([("shortlist_list_people", {"limit": 100})])["shortlist_list_people"]["data"][
                "items"
            ]
            if item["id"] == person_id
        )
        assert saved_person["name"] == "SDK"
        with app.state.sessions() as session:
            assert session.get(User, person_id).nickname == "SDK"
        wire.expect_error("shortlist_plan_schedule", {"row_id": row_id, "schedule": "not a cron expression"})
        assert wire.exercise([("shortlist_get_row", {"id": row_id})])["shortlist_get_row"]["data"]["schedule"] == ""
        schedule_plan = wire.exercise([("shortlist_plan_schedule", {"row_id": row_id, "schedule": "30 3 * * *"})])[
            "shortlist_plan_schedule"
        ]["data"]
        schedule = _apply(wire, schedule_plan["change_id"], "schedule")
        assert schedule["result"]["row_id"] == row_id
        assert (
            wire.exercise([("shortlist_get_row", {"id": row_id})])["shortlist_get_row"]["data"]["schedule"]
            == "30 3 * * *"
        )
        assert _wait_for_operation(wire, schedule["operation_id"])["status"] == "completed"
        # Cache refresh must change an observable read after its approved worker
        # finishes. A saved plan or queued job alone would leave the stale title.
        cached_libraries = wire.exercise([("shortlist_list_libraries", None)])["shortlist_list_libraries"]["data"][
            "items"
        ]
        state.sections[int(library_key)].title = "Freshly renamed matrix library"
        assert (
            wire.exercise([("shortlist_list_libraries", None)])["shortlist_list_libraries"]["data"]["items"]
            == cached_libraries
        )
        maintenance = wire.exercise([("shortlist_plan_maintenance", {"task": "cache.refresh"})])[
            "shortlist_plan_maintenance"
        ]["data"]
        assert maintenance["authorization"]["can_apply"] is False
        assert maintenance["required_capabilities"] == [Capability.MAINTENANCE_EXECUTE.value]
        with app.state.sessions() as session:
            stored_maintenance = session.get(AssistantChange, maintenance["change_id"])
            assert stored_maintenance is not None
            assert stored_maintenance.requirements["requires_approval"] is True
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": maintenance["change_id"], "idempotency_key": _idempotency("maintenance-unapproved")},
        )
        assert (
            wire.exercise([("shortlist_list_libraries", None)])["shortlist_list_libraries"]["data"]["items"]
            == cached_libraries
        )
        _approve_owner_change(wire, app, maintenance["change_id"])
        maintenance_receipt = _apply(wire, maintenance["change_id"], "maintenance-approved")
        assert _wait_for_operation(wire, maintenance_receipt["operation_id"])["status"] == "completed"
        fresh_libraries = wire.exercise([("shortlist_list_libraries", None)])["shortlist_list_libraries"]["data"][
            "items"
        ]
        assert (
            next(item for item in fresh_libraries if item["key"] == library_key)["title"]
            == "Freshly renamed matrix library"
        )
        generation_plan = wire.exercise(
            [
                (
                    "shortlist_generate_theme",
                    {
                        "action": "prepare",
                        "definition": {"brief": "A synthetic local theme", "media": "movie", "max_output_tokens": 256},
                    },
                )
            ]
        )["shortlist_generate_theme"]["data"]
        generation = wire.exercise(
            [
                (
                    "shortlist_generate_theme",
                    {
                        "action": "apply",
                        "change_id": generation_plan["change_id"],
                        "idempotency_key": _idempotency("generate"),
                    },
                )
            ]
        )["shortlist_generate_theme"]["data"]
        generated = _wait_for_operation(wire, generation["operation_id"])
        assert generated["status"] == "completed"
        assert generated["result"]["generation_stage"] == "completed"
        assert generated["result"]["draft"]["name"] == "SDK generated"
        assert app.state.matrix_provider_calls == [{"max_tokens": 256}]
        preview_plan = wire.exercise(
            [
                (
                    "shortlist_preview_row",
                    {
                        "row_id": row_id,
                        "person_ids": [person_id],
                        "max_provider_calls": 0,
                        "max_images": 0,
                        "max_acquisitions": 0,
                    },
                )
            ]
        )["shortlist_preview_row"]["data"]
        plex_before_preview = deepcopy(state.collections)
        preview_receipt = _apply(wire, preview_plan["change_id"], "preview")
        preview = _wait_for_operation(wire, preview_receipt["operation_id"])
        assert preview["status"] == "completed"
        assert state.collections == plex_before_preview
        with app.state.sessions() as session:
            assert session.get(Run, preview["result"]["run_id"]).dry_run is True
        run_plan = wire.exercise(
            [
                (
                    "shortlist_plan_run",
                    {
                        "row_ids": [row_id],
                        "person_ids": [person_id],
                        "dry_run": True,
                        "max_provider_calls": 0,
                        "max_images": 0,
                        "max_acquisitions": 0,
                    },
                )
            ]
        )["shortlist_plan_run"]["data"]
        run_receipt = _apply(wire, run_plan["change_id"], "run")
        operation_id = run_receipt["operation_id"]
        report = _wait_for_operation(wire, operation_id)
        assert report["status"] == "completed"
        assert report["result"]["run_id"] > 0
        assert report["run_usage"]["provider_calls_started"] == 0
        assert app.state.run_service is not None
    assert EXERCISED_BY_GROUP["plans"] <= wire.calls


def _wait_for_operation(wire: McpWire, operation_id: str, timeout_s: float = 60) -> dict:
    """Wait for the operation's run and its owed jobs, never merely its launch job."""
    deadline = time.monotonic() + timeout_s
    last: dict | None = None
    while time.monotonic() < deadline:
        last = wire.exercise([("shortlist_get_operation", {"operation_id": operation_id})])["shortlist_get_operation"][
            "data"
        ]
        if last["status"] in {"completed", "failed", "cancelled", "outcome_unknown", "partially_applied"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"operation {operation_id} did not settle: {last}")


def test_mcp_sdk_durable_monitoring_and_negative_matrix(tmp_path, monkeypatch):
    """Operation progress stays distinct from launch, and denied input has no state effect."""
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        before_collections = dict(state.collections)
        with app.state.sessions() as session:
            collection_count_before_invalid = session.query(Collection).count()
            historical = Run(
                trigger="manual", status="ok", dry_run=True, stats={"expected_rows": [{"slug": "matrix-row"}]}
            )
            candidate = RequestCandidate(
                tmdb_id=990002, media_type="movie", title="Monitoring request", row_slug="matrix-row", status="pending"
            )
            session.add_all([historical, candidate])
            session.commit()
            historical_id, candidate_id = historical.id, candidate.id
        wire.expect_error("shortlist_plan_row", {"action": "create", "template_id": "missing", "values": {}})
        with app.state.sessions() as session:
            assert session.query(Collection).count() == collection_count_before_invalid
        assert state.collections == before_collections
        targets = wire.exercise(
            [
                ("shortlist_list_people", {"limit": 100}),
                ("shortlist_list_rows", {"limit": 100}),
            ]
        )
        person_id = targets["shortlist_list_people"]["data"]["items"][0]["id"]
        row_id = targets["shortlist_list_rows"]["data"]["items"][0]["id"]
        plan = wire.exercise(
            [
                (
                    "shortlist_plan_run",
                    {
                        "row_ids": [row_id],
                        "person_ids": [person_id],
                        "dry_run": True,
                        "max_provider_calls": 0,
                        "max_images": 0,
                        "max_acquisitions": 0,
                    },
                )
            ]
        )["shortlist_plan_run"]["data"]
        receipt = _apply(wire, plan["change_id"], "monitor-run")
        operation_id = receipt["operation_id"]
        run_id = receipt["result"]["run_id"]
        observed = wire.exercise([("shortlist_get_operation", {"operation_id": operation_id})])
        assert observed["shortlist_get_operation"]["data"]["status"] in {"queued", "running", "completed"}
        monitored = wire.exercise(
            [
                ("shortlist_cancel_operation", {"operation_id": operation_id}),
                ("shortlist_get_run_report", {"id": run_id}),
                ("shortlist_list_runs", {"limit": 100}),
                ("shortlist_get_activity", {"limit": 100}),
                ("shortlist_list_requests", {"limit": 100}),
            ]
        )
        assert monitored["shortlist_get_run_report"]["data"]["run_id"] == run_id
        cancellation = monitored["shortlist_cancel_operation"]["data"]
        assert cancellation["run_id"] == run_id
        assert cancellation["cancel_requested"] or cancellation["status"] in {"ok", "aborted"}
        listed_runs = monitored["shortlist_list_runs"]["data"]["items"]
        assert any(
            run["run_id"] == historical_id and run["status"] == "ok" and run["expected_row_slugs"] == ["matrix-row"]
            for run in listed_runs
        )
        activity = monitored["shortlist_get_activity"]["data"]["items"]
        assert any(
            event["action"] == "assistant.prepared" and event["change_id"] == plan["change_id"] for event in activity
        )
        assert any(
            event["action"] == "assistant.applied" and event["operation_id"] == operation_id for event in activity
        )
        requests = monitored["shortlist_list_requests"]["data"]["items"]
        assert any(
            item["id"] == candidate_id and item["title"] == "Monitoring request" and item["status"] == "pending"
            for item in requests
        )
        terminal = _wait_for_operation(wire, operation_id)
        assert terminal["status"] in {"completed", "cancelled"}
    assert EXERCISED_BY_GROUP["durable_operations"] <= wire.calls


def test_mcp_sdk_cancellation_stops_a_queued_run_before_worker_handoff(tmp_path, monkeypatch):
    """Hold the worker queue until cancellation is durably visible, then release it."""
    from shortlist.server.services import jobs

    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        app.state.scheduler.pause()
        person_id = wire.exercise([("shortlist_list_people", {"limit": 100})])["shortlist_list_people"]["data"][
            "items"
        ][0]["id"]
        plan = wire.exercise(
            [
                (
                    "shortlist_plan_run",
                    {
                        "row_ids": [10],
                        "person_ids": [person_id],
                        "dry_run": True,
                        "max_provider_calls": 0,
                        "max_images": 0,
                        "max_acquisitions": 0,
                    },
                )
            ]
        )["shortlist_plan_run"]["data"]
        plex_before = deepcopy(state.collections)
        # Only scheduling is held; apply, the cancellation service and subsequent
        # durable job dispatch all remain real. No Run status is fabricated.
        with monkeypatch.context() as gate:
            gate.setattr(jobs, "drain_in_background", lambda *_args: None)
            receipt = _apply(wire, plan["change_id"], "cancel-queued")
            run_id = receipt["result"]["run_id"]
            with app.state.sessions() as session:
                queued = session.get(Run, run_id)
                assert queued.status == "queued" and queued.began_at is None

            repository = app.state.assistant_auth.repository
            foreign = repository.create_grant(
                owner_account_id=OWNER_ACCOUNT_ID,
                client_id="another-matrix-client",
                name="Another matrix connection",
                preset=GrantPreset.OWNER_AUTOMATION,
                capabilities=ASSISTANT_CAPABILITIES,
                constraints=GrantConstraints(
                    include_future_rows=True, include_future_people=True, include_future_libraries=True
                ),
            )
            foreign_wire = McpWire(wire.url, repository.issue_local_credential(foreign.grant_id).take())
            foreign_wire.expect_error("shortlist_cancel_operation", {"operation_id": receipt["operation_id"]})
            with app.state.sessions() as session:
                untouched = session.get(Run, run_id)
                assert untouched.status == "queued" and not untouched.stats.get("cancel_requested")

            cancelled = wire.exercise([("shortlist_cancel_operation", {"operation_id": receipt["operation_id"]})])[
                "shortlist_cancel_operation"
            ]["data"]
            assert cancelled == {"run_id": run_id, "status": "cancelled", "cancel_requested": True}
            with app.state.sessions() as session:
                stopped = session.get(Run, run_id)
                assert stopped.status == "aborted" and stopped.began_at is None and stopped.finished_at is not None
                assert stopped.stats["cancel_requested"] is True
                event = session.query(Event).filter(Event.scope == "assistant.run_cancelled").one()
                assert event.message["run_id"] == run_id

        asyncio.run(jobs.drain_now(app.state, "Release the cancelled SDK test run"))
        assert _wait_for_operation(wire, receipt["operation_id"])["status"] == "cancelled"
        report = wire.exercise([("shortlist_get_run_report", {"id": run_id})])["shortlist_get_run_report"]["data"]
        assert report["status"] == "aborted"
        with app.state.sessions() as session:
            assert session.get(Run, run_id).began_at is None
        assert not app.state.run_service._tasks
        assert state.collections == plex_before
        assert app.state.matrix_provider_calls == []


def test_mcp_sdk_person_row_mute_requires_plex_write_authority_but_numeric_preferences_do_not(tmp_path, monkeypatch):
    """The mute's real cleanup needs Plex authority before even local values can commit."""
    with _wire_app(tmp_path, monkeypatch, capabilities=ASSISTANT_CAPABILITIES - {Capability.RUNS_EXECUTE}) as (
        wire,
        app,
        state,
    ):
        with app.state.sessions() as session:
            person_id = session.query(User).order_by(User.id).first().id
        numeric = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "row_size": 20}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        assert numeric["authorization"]["can_apply"] is True
        _apply(wire, numeric["change_id"], "numeric-without-plex-write")
        plex_before = deepcopy(state.collections)
        with app.state.sessions() as session:
            jobs_before = session.query(Job).count()
        mute = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "muted": True}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        assert mute["authorization"]["can_apply"] is False
        wire.expect_error(
            "shortlist_apply_change", {"change_id": mute["change_id"], "idempotency_key": _idempotency("mute-denied")}
        )
        with app.state.sessions() as session:
            stored = session.get(CollectionUserOverride, (10, person_id))
            assert stored.row_size == 20 and stored.muted is False
            assert session.query(Job).count() == jobs_before
        assert state.collections == plex_before
        assert app.state.matrix_provider_calls == []


def test_mcp_sdk_person_row_settings_roundtrip_inheritance_staleness_and_scope(tmp_path, monkeypatch):
    """An override is sparse stored configuration, with exact effective defaults and no taste data."""
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        schemas = wire.schemas()

        def resolve(schema, node):
            if "$ref" not in node:
                return node
            target = schema
            for component in node["$ref"].removeprefix("#/").split("/"):
                target = target[component]
            return target

        people_schema = schemas["shortlist_plan_people"]
        request_schema = resolve(people_schema, people_schema["properties"]["request"])
        overrides_schema = request_schema["properties"]["row_overrides"]
        assert overrides_schema["type"] == "array" and overrides_schema["maxItems"] == 25
        item_schema = resolve(people_schema, overrides_schema["items"])
        assert item_schema["additionalProperties"] is False
        assert item_schema["required"] == ["row_id"]
        assert item_schema["properties"]["row_id"]["exclusiveMinimum"] == 0
        for field_name, low, high in (("row_size", 5, 40), ("recent_count", 1, 25)):
            alternatives = item_schema["properties"][field_name]["anyOf"]
            assert {"type": "null"} in alternatives
            assert {"type": "integer", "minimum": low, "maximum": high} in alternatives
        getter_schema = schemas["shortlist_get_person_row_settings"]
        getter_request = resolve(getter_schema, getter_schema["properties"]["request"])
        assert set(getter_request["required"]) == {"person_id", "row_id"}
        assert getter_request["additionalProperties"] is False
        for identifier in ("person_id", "row_id"):
            assert getter_request["properties"][identifier]["type"] == "integer"
            assert getter_request["properties"][identifier]["exclusiveMinimum"] == 0
        with app.state.sessions() as session:
            person, outsider = session.query(User).order_by(User.id).limit(2).all()
            person.enabled = False
            person.prefs = {"blocked_seeds": [{"tmdb_id": 990003, "title": "Private taste sentinel"}]}
            person_id, outsider_id = person.id, outsider.id
            row = session.get(Collection, 10)
            row.size, row.recent_count = 22, 7
            session.add(CollectionUserOverride(collection_id=10, user_id=person_id, row_size=20, recent_count=4))
            session.add(Collection(id=11, slug="outside-row", name="Outside row", library_keys=["2"], enabled=False))
            session.commit()
        repository = app.state.assistant_auth.repository
        grant = repository.find_grant_for_client("mcp-wire-matrix")
        repository.replace_grant_authority(
            grant.grant_id,
            capabilities=grant.capabilities,
            constraints=replace(
                grant.constraints,
                row_ids=frozenset({10}),
                library_keys=frozenset({"1"}),
                include_future_rows=False,
                include_future_libraries=False,
            ),
            expected_revision=grant.revision,
        )

        def read():
            result = wire.exercise([("shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})])[
                "shortlist_get_person_row_settings"
            ]
            assert "Private taste sentinel" not in str(result)
            assert "blocked_seeds" not in str(result)
            return result["data"]

        assert read() == {
            "person_id": person_id,
            "row_id": 10,
            "row_slug": "matrix-row",
            "supported": True,
            "stored": {"muted": False, "row_size": 20, "recent_count": 4},
            "effective": {
                "muted": False,
                "row_size": 20,
                "recent_count": 4,
                "base_row_size": 22,
                "base_recent_count": 7,
            },
        }
        for suffix, patch, stored, effective in (
            ("resize", {"row_size": 26}, {"muted": False, "row_size": 26, "recent_count": 4}, (26, 4)),
            (
                "reset",
                {"row_size": None, "recent_count": None},
                {"muted": False, "row_size": None, "recent_count": None},
                (22, 7),
            ),
        ):
            plan = wire.exercise(
                [
                    (
                        "shortlist_plan_people",
                        {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, **patch}]},
                    )
                ]
            )["shortlist_plan_people"]["data"]
            assert plan["authorization"]["can_apply"] is True, "An override-only plan must not need unrelated row scope"
            assert "Private taste sentinel" not in str(plan)
            _apply(wire, plan["change_id"], f"person-row-{suffix}")
            result = read()
            assert result["stored"] == stored
            assert (result["effective"]["row_size"], result["effective"]["recent_count"]) == effective
            with app.state.sessions() as session:
                saved = session.get(CollectionUserOverride, (10, person_id))
                assert (saved.muted, saved.row_size, saved.recent_count) == tuple(stored.values())

        stale = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "recent_count": 5}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID})
        with httpx.Client(
            base_url=wire.url.removesuffix("/mcp"), cookies={SESSION_COOKIE: cookie}, headers={CSRF_HEADER: "1"}
        ) as owner:
            changed = owner.put(f"/api/users/{person_id}/rows/10", json={"recent_count": 9})
        assert changed.status_code == 200, changed.text
        assert changed.json()["recent_count"] == 9
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": stale["change_id"], "idempotency_key": _idempotency("stale-person-row")},
            code="stale_plan",
        )
        assert read()["stored"]["recent_count"] == 9

        plex_before = deepcopy(state.collections)
        with app.state.sessions() as session:
            jobs_before = session.query(Job).count()
        for person_selection, row_selection in ((outsider_id, 10), (999999, 10), (person_id, 11)):
            wire.expect_error(
                "shortlist_get_person_row_settings", {"person_id": person_selection, "row_id": row_selection}
            )
        wire.expect_error(
            "shortlist_plan_people",
            {"person_id": outsider_id, "patch": {}, "row_overrides": [{"row_id": 10, "muted": True}]},
        )
        wire.expect_error(
            "shortlist_plan_people",
            {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "recent_count": "6"}]},
        )
        wire.expect_error(
            "shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10, "include_history": True}
        )
        denied_row = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 11, "row_size": 30}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        assert denied_row["authorization"]["can_apply"] is False
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": denied_row["change_id"], "idempotency_key": _idempotency("out-of-row-override")},
        )

        grant = repository.find_grant_for_client("mcp-wire-matrix")
        repository.replace_grant_authority(
            grant.grant_id,
            capabilities=grant.capabilities,
            constraints=replace(grant.constraints, library_keys=frozenset({"2"})),
            expected_revision=grant.revision,
        )
        wire.expect_error("shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        denied = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "row_size": 30}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        assert denied["authorization"]["can_apply"] is False
        wire.expect_error(
            "shortlist_apply_change",
            {"change_id": denied["change_id"], "idempotency_key": _idempotency("out-of-library-override")},
        )
        grant = repository.find_grant_for_client("mcp-wire-matrix")
        repository.replace_grant_authority(
            grant.grant_id,
            capabilities=grant.capabilities - {Capability.PEOPLE_READ},
            constraints=replace(grant.constraints, library_keys=frozenset({"1"})),
            expected_revision=grant.revision,
        )
        wire.expect_error("shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        with app.state.sessions() as session:
            saved = session.get(CollectionUserOverride, (10, person_id))
            assert (saved.muted, saved.row_size, saved.recent_count) == (False, None, 9)
            assert session.get(CollectionUserOverride, (11, person_id)) is None
            assert session.query(Job).count() == jobs_before
        assert state.collections == plex_before
    assert EXERCISED_BY_GROUP["person_row_settings"] <= wire.calls


def test_mcp_sdk_person_row_mute_removes_only_the_selected_persons_row(tmp_path, monkeypatch):
    """Dispatch the real reconcile worker against Plex's HTTP shape and inspect actual collections."""
    with _wire_app(tmp_path, monkeypatch) as (wire, app, state):
        with app.state.sessions() as session:
            person, other = session.query(User).order_by(User.id).limit(2).all()
            person_id = person.id
            # Old collections can remain after disabling; configuration and cleanup still work.
            person.enabled = False
            session.add(Collection(id=11, slug="other-row", name="Other row", library_keys=["1"], enabled=True))
            session.commit()
            state.collections.update(
                {
                    8901: FakeCollection(
                        rating_key=8901,
                        title="Matrix row" + row_marker(person.plex_account_id),
                        section_id=1,
                        labels=[f"shortlist_{person.slug}"],
                    ),
                    8902: FakeCollection(
                        rating_key=8902,
                        title="Matrix row" + row_marker(other.plex_account_id),
                        section_id=1,
                        labels=[f"shortlist_{other.slug}"],
                    ),
                    8903: FakeCollection(
                        rating_key=8903,
                        title="Other row" + row_marker(person.plex_account_id),
                        section_id=1,
                        labels=[f"shortlist_{person.slug}"],
                    ),
                    8904: FakeCollection(rating_key=8904, title="Matrix row", section_id=1),
                }
            )
        expected_survivors = deepcopy({key: value for key, value in state.collections.items() if key != 8901})
        plan = wire.exercise(
            [
                (
                    "shortlist_plan_people",
                    {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "muted": True}]},
                )
            ]
        )["shortlist_plan_people"]["data"]
        assert plan["authorization"]["can_apply"] is True
        receipt = _apply(wire, plan["change_id"], "mute-one-person-row")
        assert _wait_for_operation(wire, receipt["operation_id"])["status"] == "completed"
        assert state.collections == expected_survivors
        with app.state.sessions() as session:
            assert session.get(CollectionUserOverride, (10, person_id)).muted is True
            assert session.query(CollectionUserOverride).count() == 1
        assert app.state.matrix_provider_calls == []


def test_mcp_sdk_verified_paused_theme_setup_needs_no_ai_provider_or_capability(tmp_path, monkeypatch):
    """A supplied fixed theme is not provider authoring, and pausing prevents later top-ups."""
    from shortlist.server.assistant.budgets import AssistantBudget
    from shortlist.server.db.models import Job

    delegated_without_generation = ASSISTANT_CAPABILITIES - {Capability.AI_GENERATE}
    with _wire_app(
        tmp_path,
        monkeypatch,
        configured_provider=False,
        capabilities=delegated_without_generation,
    ) as (wire, app, state):
        catalog = wire.exercise(
            [
                ("shortlist_list_people", {"limit": 100}),
                ("shortlist_list_libraries", None),
                ("shortlist_list_templates", None),
                ("shortlist_list_themes", {"limit": 100}),
            ]
        )
        person_id = catalog["shortlist_list_people"]["data"]["items"][0]["id"]
        library_key = catalog["shortlist_list_libraries"]["data"]["items"][0]["key"]
        template_id = next(
            item["id"]
            for item in catalog["shortlist_list_templates"]["data"]["items"]
            if item["id"] == "describe-a-row"
        )
        saved_theme_id = catalog["shortlist_list_themes"]["data"]["items"][0]["id"]
        movie = next(iter(state.movies.values()))
        resolved = wire.exercise(
            [("shortlist_search_titles", {"query": movie.title, "media": "movie", "year": movie.year})]
        )["shortlist_search_titles"]["data"]["items"]
        assert resolved
        pick = resolved[0]
        setup = {
            "theme": {
                "name": "Verified supplied theme",
                "media": ["movie"],
                "picks": [{"tmdb_id": pick["tmdb_id"], "media": "movie"}],
            },
            "row": {
                "action": "create",
                "template_id": template_id,
                "values": {
                    "name": "Verified supplied picks",
                    "enabled": False,
                    "schedule": "",
                    "audience": "subset",
                    "audience_user_ids": [person_id],
                    "library_keys": [library_key],
                    "ai_paused": True,
                    "theme_mode": "fixed",
                    "avoid_rows": ["matrix-row"],
                },
            },
        }
        plan = wire.exercise([("shortlist_plan_setup", setup)])["shortlist_plan_setup"]["data"]
        assert plan["authorization"]["can_apply"] is True
        assert plan["summary"]["row"]["future_effects"]["provider_generation"] is False
        for name, values in (
            ("unpaused", {"ai_paused": False}),
            ("web", {"candidate_sources": ["llm_web"]}),
            ("ai-poster", {"poster": {"mode": "ai", "title": "Synthetic"}}),
            ("explore", {"theme_mode": "explore"}),
        ):
            denied_setup = deepcopy(setup)
            denied_setup["theme"]["name"] = f"Verified supplied theme {name}"
            denied_setup["row"]["values"].update(values)
            denied = wire.exercise([("shortlist_plan_setup", denied_setup)])["shortlist_plan_setup"]["data"]
            assert denied["authorization"]["can_apply"] is False
            assert Capability.AI_GENERATE.value in denied["required_capabilities"]
        receipt = _apply(wire, plan["change_id"], "verified-paused-theme")
        row_id = receipt["result"]["row_id"]
        saved_row = wire.exercise([("shortlist_get_row", {"id": row_id})])["shortlist_get_row"]["data"]
        assert saved_row["fields"]["ai_paused"] is True
        assert saved_row["fields"]["avoid_rows"] == ["matrix-row"]
        with app.state.sessions() as session:
            row = session.get(Collection, row_id)
            assert row is not None and row.ai_paused is True and row.theme_id is not None
            assert (
                session.get(
                    AssistantBudget,
                    app.state.assistant_auth.repository.find_grant_for_client("mcp-wire-matrix").grant_id,
                )
                is None
            )
            assert not session.query(Job).filter(Job.kind == "assistant.generate_theme").count()
        assert app.state.matrix_provider_calls == []
        unpause = wire.exercise(
            [("shortlist_plan_row", {"action": "update", "row_id": row_id, "values": {"ai_paused": False}})]
        )["shortlist_plan_row"]["data"]
        # Unpausing a disabled row is inert. The later activation plan is where
        # effective provider work is re-evaluated and must require authority.
        assert unpause["authorization"]["can_apply"] is True
        assert Capability.AI_GENERATE.value not in unpause["required_capabilities"]
        active_unpause = wire.exercise(
            [
                (
                    "shortlist_plan_row",
                    {"action": "update", "row_id": row_id, "values": {"enabled": True, "ai_paused": False}},
                )
            ]
        )["shortlist_plan_row"]["data"]
        assert active_unpause["authorization"]["can_apply"] is False
        assert Capability.AI_GENERATE.value in active_unpause["required_capabilities"]
        arbitrary_theme = wire.exercise(
            [
                (
                    "shortlist_plan_row",
                    {
                        "action": "create",
                        "template_id": template_id,
                        "values": {
                            **setup["row"]["values"],
                            "name": "Arbitrary saved theme",
                            "theme_id": saved_theme_id,
                        },
                    },
                )
            ]
        )["shortlist_plan_row"]["data"]
        assert arbitrary_theme["authorization"]["can_apply"] is False
        assert Capability.AI_GENERATE.value in arbitrary_theme["required_capabilities"]
