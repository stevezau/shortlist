"""Every response model must pass undeclared keys through, not filter them out.

This is the guard for the rule in `shortlist/server/api/schemas.py`. It exists because the obvious
test does NOT catch a violation: asserting an endpoint's key set passes whether or not the model
declares every field, precisely BECAUSE `extra="allow"` lets the undeclared ones through. Those
assertions protect the passthrough; nothing protected the passthrough itself.

So this walks the live route table and checks the config directly. A new endpoint whose model sits on
a bare `BaseModel` fails here, at the one moment someone is looking — rather than in production, by
quietly dropping a field the SPA reads.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from shortlist.server.api.schemas import PassthroughModel


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTLIST_CONFIG", str(tmp_path))
    from shortlist.server.main import create_app

    return create_app()


def _walk_routes(routes) -> list:
    """Every route, descending into included routers.

    `app.routes` does NOT contain the API endpoints: `include_router` leaves an `_IncludedRouter`
    proxy holding the real router on `.original_router`, so a flat walk finds 15 routes and one
    model. Getting this wrong is why the vacuous-pass test below exists.
    """
    out = []
    for route in routes:
        out.append(route)
        nested = getattr(route, "original_router", None) or getattr(route, "app", None)
        if nested is not None and hasattr(nested, "routes") and nested.routes is not routes:
            out.extend(_walk_routes(nested.routes))
    return out


def _response_models(app) -> list[tuple[str, type]]:
    """(route path, model) for every route declaring a Pydantic response model."""
    found = []
    for route in _walk_routes(app.routes):
        model = getattr(route, "response_model", None)
        if model is None:
            continue
        for candidate in _unwrap(model):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                found.append((getattr(route, "path", str(route)), candidate))
    return found


def _unwrap(annotation) -> list:
    """Pull models out of `list[X]`, `X | None`, `dict[str, X]` and bare `X` alike."""
    args = getattr(annotation, "__args__", None)
    if not args:
        return [annotation]
    return [inner for arg in args for inner in _unwrap(arg)]


def _nested(model: type, seen: set[type]) -> list[type]:
    """Every model reachable from `model`'s fields — nested ones drop fields exactly the same way."""
    if model in seen:
        return []
    seen.add(model)
    out = [model]
    for field in model.model_fields.values():
        for candidate in _unwrap(field.annotation):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                out.extend(_nested(candidate, seen))
    return out


class TestEveryResponseModelIsAPassthrough:
    def test_the_route_table_actually_has_response_models(self, app):
        """Guards the guard: if the walk stops finding models, everything below passes vacuously."""
        assert len(_response_models(app)) > 40

    def test_no_response_model_filters_undeclared_keys(self, app):
        offenders = sorted(
            {
                f"{path} -> {model.__name__}"
                for path, root in _response_models(app)
                for model in _nested(root, set())
                if model.model_config.get("extra") != "allow"
            }
        )
        assert offenders == [], (
            "these response models DROP undeclared keys from the payload — inherit "
            f"{PassthroughModel.__name__} (see shortlist/server/api/schemas.py): " + ", ".join(offenders)
        )

    def test_a_passthrough_model_really_does_keep_an_undeclared_key(self):
        """The property itself, asserted once rather than assumed of Pydantic."""

        class Example(PassthroughModel):
            declared: int

        dumped = Example.model_validate({"declared": 1, "not_declared": "kept"}).model_dump()

        assert dumped == {"declared": 1, "not_declared": "kept"}
