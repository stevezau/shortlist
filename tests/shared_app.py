"""One app per xdist worker, started against each test's own config dir.

FastAPI builds every route's request state the first time an app serves a request: ~0.13s for
~190 routes, paid again by every new app. With ~1,100 tests each booting their own app, that was
about 4 minutes of a full run. `app_for` hands out the same app instead, already warm.

Isolation is the cost to manage, so every test still gets:

- its own config dir, database and lifespan: everything the lifespan builds (sessions, scheduler,
  run service, job worker) is built fresh at each startup, exactly as before;
- a new `app.state` and no `dependency_overrides`: `reset_shared_apps` puts both back to how
  `create_app` left them after every test;
- a fresh app whenever sharing could be wrong: a second app in the same test, an app whose
  routes a test changed, or one whose lifespan never shut down is never handed out again.

The app's routes depend on the environment `create_app` reads (base path, docs flag, a built
SPA), so one app is kept per distinct environment.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.datastructures import State


@dataclass
class _Shared:
    app: FastAPI
    pristine_state: dict
    route_count: int
    running: int = 0
    handed_out: bool = False


_shared: dict[tuple, _Shared] = {}


def _environment_key() -> tuple:
    from shortlist.server import main

    return (
        os.environ.get("APP_BASE_PATH"),
        os.environ.get("SHORTLIST_ENABLE_DOCS"),
        str(main.WEB_DIST),
        main.WEB_DIST.exists(),
    )


def _build(config_dir: Path) -> _Shared:
    from shortlist.server.main import create_app

    app = create_app(config_dir=config_dir)
    shared = _Shared(app=app, pristine_state=dict(app.state._state), route_count=len(app.router.routes))
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def counted(application: FastAPI) -> AsyncGenerator[None, None]:
        shared.running += 1
        try:
            async with lifespan(application):
                yield
        finally:
            shared.running -= 1

    app.router.lifespan_context = counted
    return shared


def app_for(config_dir: Path) -> FastAPI:
    """The worker's shared app, set to start against `config_dir`.

    Used exactly like `create_app(config_dir=...)`. A second call in the same test gets a fresh,
    unshared app, so a test that runs two apps at once still gets two.
    """
    from shortlist.server.main import create_app

    key = _environment_key()
    shared = _shared.get(key)
    if shared is None:
        shared = _shared[key] = _build(config_dir)
    if shared.handed_out:
        return create_app(config_dir=config_dir)
    shared.handed_out = True
    config_dir.mkdir(parents=True, exist_ok=True)
    shared.app.state.config_dir = config_dir
    return shared.app


def reset_shared_apps() -> None:
    """Put every app handed out in this test back to how `create_app` left it."""
    for key, shared in list(_shared.items()):
        if not shared.handed_out:
            continue
        app = shared.app
        if shared.running or len(app.router.routes) != shared.route_count:
            del _shared[key]
            continue
        app.dependency_overrides.clear()
        # A new State, not a cleared one: some caches live in `state.__dict__` directly
        # (`connection_choices.cached_plex_read`), outside the dict `State` keeps its attributes in.
        app.state = State(dict(shared.pristine_state))
        shared.handed_out = False
