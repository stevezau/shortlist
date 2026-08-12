"""In-memory fake of agregarr's REST API, served over real HTTP.

``make_fake_agregarr(state)`` builds a FastAPI app speaking the endpoints the shelf mirror uses, so
an integration test drives the real ``AgregarrClient`` over real httpx with nothing mocked.

Fidelity notes — mirrors of real agregarr 2.4.2 behaviour the mirror depends on, recorded from a
live instance. Per the testing rules the fake must be **no easier than the real server**, and each
of these is a way the real one refuses a request that a lenient fake would have accepted:

- ``/preexisting`` and ``/defaulthubs`` answer BARE LISTS (not an enveloped object).
- ``/hubs/configs`` — the route the OpenAPI spec advertises for hub configs — is SESSION
  authenticated and answers a 307 to ``/login`` for an API key. Reading it yields a sign-in page,
  never JSON, so the mirror has to use ``/defaulthubs``.
- Configs agregarr has never placed are served **without a ``position`` field at all** (106 of 213
  on a real server), while ``POST /reorder`` REQUIRES ``position`` on every item and rejects the
  whole request with 400 when any lacks it. Echoing its own payload straight back therefore fails.
- ``POST /reorder`` assigns ``sortOrderHome = index + 1``; ``0`` is a "void" value meaning unplaced,
  and void items sort LAST, not first.
- Items absent from ``mixedItems`` keep their stored value and are not renumbered.
- A wrong API key is a 401; a bearer token is not accepted at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse

API_KEY = "fake-agregarr-key"


@dataclass
class FakeAgregarrState:
    """Agregarr's stored configs, plus a record of what the mirror asked it to do."""

    preexisting: list[dict[str, Any]] = field(default_factory=list)
    hubs: list[dict[str, Any]] = field(default_factory=list)
    #: One entry per accepted POST /reorder — (libraryId, context, [item ids in order]).
    writes: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: Ids agregarr's type guards rejected — it returns 200 having silently ignored them.
    skipped: list[str | None] = field(default_factory=list)
    #: GET count per path. Both read endpoints return the WHOLE instance regardless of library, so
    #: a caller that reads them once per library re-downloads every config each time.
    reads: dict[str, int] = field(default_factory=dict)

    def order_for(self, library_id: str) -> list[str]:
        """Ids for one library in the order agregarr would now push, mirroring its own sync:
        real keys ascending, then unplaced (0/missing) items last."""
        items = [c for c in self.preexisting + self.hubs if str(c.get("libraryId")) == str(library_id)]

        def rank(config: dict[str, Any]) -> float:
            value = config.get("sortOrderHome")
            return float(value) if isinstance(value, int) and value > 0 else float("inf")

        return [c["id"] for c in sorted(items, key=rank)]


def seed_agregarr_state(rows: list[str], foreign: list[str], library_id: str = "1") -> FakeAgregarrState:
    """A library where agregarr has our rows SCATTERED below its own collections.

    ``rows`` are Plex ratingKeys of Shortlist rows, ``foreign`` are agregarr's own collections. The
    foreign rows take the low sort keys, which is the real-world shape: there is no room at the top
    for our block, so a mirror that renumbered only our rows could not fix it.
    """
    state = FakeAgregarrState()
    state.hubs.append(
        {
            "id": f"{library_id}:movie.recentlyadded",
            "hubIdentifier": "movie.recentlyadded",
            "libraryId": library_id,
            "name": "Recently Added Movies",
            "sortOrderHome": 1,
            "configType": "hub",
            "position": 0,
        }
    )
    for index, rating_key in enumerate(foreign):
        state.preexisting.append(
            {
                "id": f"foreign-{rating_key}",
                "collectionRatingKey": rating_key,
                "libraryId": library_id,
                "name": f"Foreign Collection {rating_key}",
                "sortOrderHome": index + 2,
                "configType": "preExisting",
                "position": index + 1,
            }
        )
    for index, rating_key in enumerate(rows):
        config = {
            "id": f"row-{rating_key}",
            "collectionRatingKey": rating_key,
            "libraryId": library_id,
            "name": "✨ Movies Picked for You",
            # Scattered far below the foreign collections; the last one is UNPLACED (0), the shape
            # that sinks a row to the bottom of the shelf.
            "sortOrderHome": 0 if index == len(rows) - 1 else 40 + index,
            "configType": "preExisting",
        }
        if index % 2 == 0:
            # Half carry no `position` at all — exactly what a real instance serves for anything it
            # has not placed, and what POST /reorder then refuses to accept back.
            config["position"] = 20 + index
        state.preexisting.append(config)
    return state


def make_fake_agregarr(state: FakeAgregarrState) -> FastAPI:
    app = FastAPI()

    def _authed(api_key: str | None) -> bool:
        return api_key == API_KEY

    @app.get("/api/v1")
    def root() -> dict:
        return {"api": "Agregarr API", "version": "1.0"}

    @app.get("/api/v1/preexisting")
    def preexisting(x_api_key: str | None = Header(default=None)):
        if not _authed(x_api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        state.reads["preexisting"] = state.reads.get("preexisting", 0) + 1
        return state.preexisting

    @app.get("/api/v1/hubs/configs")
    def hub_configs_session_only():
        # The real instance bounces an API key to the login page here — following the redirect
        # yields HTML, never JSON. Present so a mirror that used this route fails in tests too.
        return RedirectResponse("/login", status_code=307)

    @app.get("/login")
    def login_page():
        return JSONResponse({"error": "sign in"}, status_code=200)

    @app.get("/api/v1/defaulthubs")
    def defaulthubs(x_api_key: str | None = Header(default=None)):
        if not _authed(x_api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        state.reads["defaulthubs"] = state.reads.get("defaulthubs", 0) + 1
        return state.hubs

    @app.post("/api/v1/reorder")
    async def reorder(request: Request, x_api_key: str | None = Header(default=None)):
        if not _authed(x_api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        for field_name in ("libraryId", "context", "mixedItems"):
            if field_name not in body:
                return JSONResponse({"message": f"missing {field_name}"}, status_code=400)
        if body["context"] not in ("home", "recommended", "library"):
            return JSONResponse({"message": "invalid context"}, status_code=400)

        errors = [
            f"request.body.mixedItems[{i}] should have required property '{prop}'"
            for i, item in enumerate(body["mixedItems"])
            for prop in ("id", "configType", "position")
            if prop not in item
        ]
        if errors:
            return JSONResponse({"message": "An unexpected error occurred", "errors": errors}, status_code=400)

        by_id = {c["id"]: c for c in state.preexisting + state.hubs}
        applied = 0
        for index, item in enumerate(body["mixedItems"]):
            # The type guards are bare `in` checks on the join key, and an item satisfying NEITHER
            # is skipped with a warning while the request still returns 200 — a write that silently
            # does nothing. Reproduced here so a payload missing the join key fails a test rather
            # than passing one.
            if "collectionRatingKey" not in item and "hubIdentifier" not in item:
                state.skipped.append(item.get("id"))
                continue
            stored = by_id.get(item["id"])
            if stored is None:
                continue
            # `{...stored, ...sent}`: every field the caller sends OVERWRITES storage, everything
            # else survives. sortOrderHome then comes from the INDEX, not from the sent item.
            stored.update({k: v for k, v in item.items() if k != "configType"})
            stored["sortOrderHome"] = index + 1
            applied += 1
        state.writes.append((str(body["libraryId"]), body["context"], [i["id"] for i in body["mixedItems"]]))
        return {"success": True, "totalItemsProcessed": applied}

    return app
