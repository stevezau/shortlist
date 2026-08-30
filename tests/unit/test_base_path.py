"""APP_BASE_PATH is read at runtime, so the published image can be served from a subpath
without rebuilding its web bundle. A root deployment must render byte-identical output.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

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


@pytest.mark.parametrize(
    "raw",
    [
        '/a"onload="alert(1)',  # breaks out of the src="…" attribute the prefix is written into
        '/"><img src=x onerror=alert(1)>',
        "/shortlist?x=1",  # a query is not part of a path, so this matches no request ever sent
        "/shortlist#frag",
        "//evil.com",  # protocol-relative: the SPA would send every API call off-origin
        "/has space",
        "/back\\slash",
        "/short%2flist",  # uvicorn decodes before matching, so this never equals what was configured
        "/a%20b",
        "/a/../b",  # the browser normalises this out of the asset URL before it asks
        "/..",
    ],
)
def test_a_value_that_is_not_a_url_path_is_refused(raw: str) -> None:
    """Refused at the ONE boundary, because it has two sinks with different escaping.

    `render_shell` JSON-escapes the value for the injected `<script>` but interpolates it RAW into
    the shell's `src=`/`href=` attributes. The four middle cases are worse than they look: each
    normalises without complaint and then matches no path the server will ever be asked for, so the
    app serves from the root while the SPA believes it is prefixed — a blank page, and before this
    there was nothing in the log to say why.
    """
    assert resolve_base_path(raw) == ""


@pytest.mark.parametrize("raw", ["/\u0444\u0438\u043b\u044c\u043c\u044b", "/media-1_2.3~x", "/apps/shortlist"])
def test_an_ordinary_path_is_still_accepted(raw: str) -> None:
    # Refused by CHARACTER, not permitted by one — an operator whose site is not in ASCII gets to
    # use a prefix too. The rejected set is only what breaks an HTML attribute or cannot appear in
    # a request path.
    assert resolve_base_path(raw) == raw


def test_refusing_a_value_says_so() -> None:
    # loguru does not route through stdlib logging, so `caplog` sees nothing — sink pattern, as
    # used by the rest of the suite. The whole point of the warning is that the symptom this
    # prevents (an app serving from the root while the SPA thinks otherwise) is otherwise silent.
    lines: list[str] = []
    sink = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        assert resolve_base_path("/shortlist?x=1") == ""
    finally:
        logger.remove(sink)
    assert any("APP_BASE_PATH" in line and "/shortlist?x=1" in line for line in lines), lines


def test_a_shell_with_no_head_is_a_hard_failure() -> None:
    # The quiet version of this is a white screen: the assets still resolve, so the app boots, and
    # then every API call goes to the server root. Better to refuse to start.
    with pytest.raises(RuntimeError, match="<head>"):
        render_shell("<html><body>no head here</body></html>", "/shortlist")
