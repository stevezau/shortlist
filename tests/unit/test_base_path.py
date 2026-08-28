"""APP_BASE_PATH is read at runtime, so the published image can be served from a subpath
without rebuilding its web bundle. A root deployment must render byte-identical output.
"""

from __future__ import annotations

import asyncio

import pytest

from shortlist.server.base_path import (
    BasePathMiddleware,
    base_path_from_env,
    render_shell,
    resolve_base_path,
)

SHELL = (
    "<!doctype html><html><head><title>x</title></head>"
    '<body><script type="module" src="/assets/index-abc.js"></script>'
    '<link rel="stylesheet" href="/assets/index-def.css"></body></html>'
)


@pytest.mark.parametrize("raw", [None, "", "   ", "/"])
def test_root_deployments_resolve_to_empty(raw: str | None) -> None:
    assert resolve_base_path(raw) == ""


@pytest.mark.parametrize(
    "raw",
    ["/shortlist", "shortlist", "/shortlist/", "shortlist/", "  /shortlist  ", "/shortlist///"],
)
def test_accepts_what_people_type_into_a_proxy_config(raw: str) -> None:
    assert resolve_base_path(raw) == "/shortlist"


def test_nested_prefix() -> None:
    assert resolve_base_path("apps/shortlist/") == "/apps/shortlist"


def test_env_is_read_and_normalised() -> None:
    assert base_path_from_env({"APP_BASE_PATH": "shortlist/"}) == "/shortlist"
    assert base_path_from_env({}) == ""


def test_root_shell_is_returned_untouched() -> None:
    assert render_shell(SHELL, "") == SHELL


def test_subpath_shell_rewrites_assets_and_publishes_the_prefix() -> None:
    out = render_shell(SHELL, "/shortlist")
    assert 'src="/shortlist/assets/index-abc.js"' in out
    assert 'href="/shortlist/assets/index-def.css"' in out
    assert '"/assets/' not in out
    assert 'window.__SHORTLIST_BASE_PATH__ = "/shortlist";' in out


def test_prefix_is_injected_before_the_bundle_loads() -> None:
    out = render_shell(SHELL, "/shortlist")
    assert out.index("__SHORTLIST_BASE_PATH__") < out.index("index-abc.js")


def test_prefix_cannot_break_out_of_the_injected_script() -> None:
    out = render_shell(SHELL, '/a"</script><script>alert(1)</script>')
    injected = out.split("<script>window.__SHORTLIST_BASE_PATH__ = ")[1].split("</script>")[0]
    assert "alert(1)" in injected, "the whole value should stay inside the one script element"
    assert "<\\/script>" in injected


class _Recorder:
    """Minimal ASGI app that records the scope it was called with."""

    def __init__(self) -> None:
        self.scope: dict | None = None

    async def __call__(self, scope, receive, send) -> None:
        self.scope = scope


def _call(middleware: BasePathMiddleware, path: str, raw_path: bytes | None = None) -> dict:
    scope: dict = {"type": "http", "path": path, "root_path": ""}
    if raw_path is not None:
        scope["raw_path"] = raw_path
    asyncio.run(middleware(scope, None, None))
    return middleware.app.scope


def test_middleware_publishes_the_prefix_as_root_path() -> None:
    mw = BasePathMiddleware(_Recorder(), "/shortlist")
    scope = _call(mw, "/shortlist/api/system/health")
    assert scope["root_path"] == "/shortlist"


def test_path_is_left_whole() -> None:
    mw = BasePathMiddleware(_Recorder(), "/shortlist")
    assert _call(mw, "/shortlist/assets/index-abc.js")["path"] == "/shortlist/assets/index-abc.js"


def test_an_unprefixed_path_is_untouched() -> None:
    mw = BasePathMiddleware(_Recorder(), "/shortlist")
    scope = _call(mw, "/api/system/health")
    assert scope["root_path"] == ""
    assert scope["path"] == "/api/system/health"


def test_a_lookalike_prefix_is_not_claimed() -> None:
    mw = BasePathMiddleware(_Recorder(), "/shortlist")
    assert _call(mw, "/shortlistings/api")["root_path"] == ""


def test_the_bare_prefix_is_claimed() -> None:
    assert _call(BasePathMiddleware(_Recorder(), "/shortlist"), "/shortlist")["root_path"] == "/shortlist"


def test_no_base_path_is_a_pass_through() -> None:
    mw = BasePathMiddleware(_Recorder(), "")
    scope = _call(mw, "/shortlist/api/x")
    assert scope["path"] == "/shortlist/api/x"
    assert scope["root_path"] == ""
