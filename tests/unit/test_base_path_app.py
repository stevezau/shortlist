"""APP_BASE_PATH against the REAL assembled app, not the pieces.

`test_base_path.py` drives the middleware with a stub that records its scope — which proves the
scope is shaped as intended, and would keep passing if Starlette stopped honouring that shape. The
load-bearing assumption is Starlette's, not ours: the ASGI spec says `path` carries the whole
request path INCLUDING `root_path`, and Starlette (>= 0.33, this repo is on 1.3) subtracts
`root_path` before matching. That is why the middleware leaves `path` alone. Nothing here would
survive that changing, so it is pinned against a real `create_app()`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shortlist.server import main as main_module

SECRET = "SUPER-SECRET-FERNET-KEY-DO-NOT-LEAK"
BUNDLE = "console.log('bundle')"
SHELL = (
    "<!doctype html><html><head><title>Shortlist</title>"
    '<script type="module" crossorigin src="/assets/index-abc.js"></script>'
    '<link rel="stylesheet" crossorigin href="/assets/index-def.css">'
    "</head><body><div id=root></div></body></html>"
)


async def _get(app, raw_path: str) -> tuple[int, bytes, dict[str, str]]:
    """One GET through the whole ASGI stack, with the path left UN-normalized (what a socket sends)."""
    status: dict[str, int] = {}
    headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            headers.update({k.decode().lower(): v.decode() for k, v in message.get("headers", [])})
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("1.2.3.4", 9999),
            "root_path": "",
        },
        receive,
        send,
    )
    return status.get("code", 0), b"".join(chunks), headers


def get(app, path: str) -> tuple[int, bytes, dict[str, str]]:
    return asyncio.run(_get(app, path))


def _build(tmp_path: Path, monkeypatch, base_path: str | None):
    web_dist = tmp_path / "web" / "dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text(SHELL)
    (web_dist / "assets" / "index-abc.js").write_text(BUNDLE)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secret.key").write_text(SECRET)
    monkeypatch.setattr(main_module, "WEB_DIST", web_dist)
    if base_path is None:
        monkeypatch.delenv("APP_BASE_PATH", raising=False)
    else:
        monkeypatch.setenv("APP_BASE_PATH", base_path)
    return main_module.create_app(config_dir=tmp_path / "config")


@pytest.fixture
def prefixed(tmp_path: Path, monkeypatch):
    return _build(tmp_path, monkeypatch, "/shortlist")


@pytest.fixture
def rooted(tmp_path: Path, monkeypatch):
    return _build(tmp_path, monkeypatch, None)


class TestServedUnderThePrefix:
    def test_api_routes_match(self, prefixed):
        status, body, _ = get(prefixed, "/shortlist/api/system/health")
        assert status == 200, body

    def test_the_assets_mount_matches(self, prefixed):
        status, body, _ = get(prefixed, "/shortlist/assets/index-abc.js")
        assert (status, body.decode()) == (200, BUNDLE)

    def test_the_shell_names_its_bundle_under_the_prefix(self, prefixed):
        _status, body, _ = get(prefixed, "/shortlist/")
        text = body.decode()
        assert 'src="/shortlist/assets/index-abc.js"' in text
        assert 'href="/shortlist/assets/index-def.css"' in text
        assert '="/assets/' not in text

    def test_the_prefix_is_published_to_the_spa(self, prefixed):
        _status, body, _ = get(prefixed, "/shortlist/")
        assert b'window.__SHORTLIST_BASE_PATH__ = "/shortlist";' in body

    def test_the_bare_prefix_redirects_to_the_slash(self, prefixed):
        # What someone types. Starlette builds the redirect from the FULL path, so the prefix
        # survives it — a redirect to "/" would bounce the browser out of the app.
        status, _body, headers = get(prefixed, "/shortlist")
        assert (status, headers.get("location")) == (307, "http://test/shortlist/")

    def test_a_deep_spa_route_serves_the_shell(self, prefixed):
        status, body, _ = get(prefixed, "/shortlist/users/42")
        assert status == 200
        assert b"__SHORTLIST_BASE_PATH__" in body

    def test_index_html_by_name_is_rewritten_too(self, prefixed):
        _status, body, _ = get(prefixed, "/shortlist/index.html")
        assert b'src="/shortlist/assets/index-abc.js"' in body

    def test_the_shell_carries_a_validator(self, prefixed):
        # The rewritten shell is not the file on disk, so nothing else would give it one, and a
        # caching proxy in front of a subpath install is the entire point of the feature.
        _status, _body, headers = get(prefixed, "/shortlist/")
        assert headers["etag"].startswith('"')

    def test_the_containment_guard_still_holds_under_the_prefix(self, prefixed):
        # The traversal guard reads the path AFTER root_path is subtracted; a prefix must not
        # smuggle anything past it.
        _status, body, _ = get(prefixed, "/shortlist/../config/secret.key")
        assert SECRET.encode() not in body


class TestTheRestOfTheServer:
    def test_an_unprefixed_path_still_routes(self, prefixed):
        # The container HEALTHCHECK curls an unprefixed /api/system/health on localhost, so a
        # subpath install that stopped answering at the root would report itself unhealthy for ever.
        status, _body, _ = get(prefixed, "/api/system/health")
        assert status == 200

    def test_a_lookalike_prefix_is_not_claimed(self, prefixed):
        # "/shortlistings" starts with "/shortlist" as a STRING but is not under it as a PATH.
        status, body, _ = get(prefixed, "/shortlistings/api/system/health")
        assert status == 200
        assert b"__SHORTLIST_BASE_PATH__" in body, "should fall through to the SPA, not the API"


class TestARootInstallIsUnchanged:
    """The feature has to be invisible when it is off — this is what every existing install runs."""

    def test_the_shell_is_served_straight_from_disk(self, rooted):
        status, body, headers = get(rooted, "/")
        assert status == 200
        assert body.decode() == SHELL, "no rewriting when there is no prefix to write in"
        # `FileResponse`'s own validators, i.e. still the pre-feature response.
        assert "etag" in headers and "last-modified" in headers

    def test_nothing_is_published_to_the_spa(self, rooted):
        _status, body, _ = get(rooted, "/")
        assert b"__SHORTLIST_BASE_PATH__" not in body
