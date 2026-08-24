"""The SPA fallback route must serve the app shell and its own bundle files — never anything
outside the web bundle. Regression for an unauthenticated path-traversal file read (a crafted
`../../config/secret.key` leaked the Fernet key + DB, collapsing the whole encryption design).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shortlist.server import main as main_module

SECRET = "SUPER-SECRET-FERNET-KEY-DO-NOT-LEAK"


async def _asgi_get(app, raw_path: str) -> tuple[int, bytes]:
    """Drive the ASGI app with an UN-normalized path — what a raw socket / `curl --path-as-is`
    sends. TestClient (httpx) would collapse `..` before it ever reached the app, hiding the bug."""
    status: dict[str, int] = {}
    headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
            headers.update({k.decode().lower(): v.decode() for k, v in msg.get("headers", [])})
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    scope = {
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
    }
    await app(scope, receive, send)
    return status.get("code", 0), b"".join(chunks), headers


@pytest.fixture
def spa_app(tmp_path: Path, monkeypatch):
    """A built app whose web bundle is a temp dir, with a secret planted just outside it."""
    web_dist = tmp_path / "web" / "dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text("<html>shortlist app shell</html>")
    (web_dist / "robots.txt").write_text("User-agent: *\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "secret.key").write_text(SECRET)

    monkeypatch.setattr(main_module, "WEB_DIST", web_dist)
    return main_module.create_app(config_dir=tmp_path / "config")


class TestSpaTraversal:
    @pytest.mark.parametrize(
        "raw_path",
        [
            "/../config/secret.key",
            "/../../config/secret.key",
            "/../../../../../../etc/hostname",
        ],
    )
    def test_spa_traversal_falls_through_to_the_shell(self, spa_app, raw_path):
        status, body, _headers = asyncio.run(_asgi_get(spa_app, raw_path))
        # An escaped path is served the app shell, never the file outside the bundle.
        assert SECRET.encode() not in body
        assert b"app shell" in body
        assert status == 200

    def test_symlink_out_of_bundle_is_blocked(self, spa_app, tmp_path):
        # A symlink *inside* the bundle pointing at a secret outside it: `.resolve()` follows it to
        # the real target, so containment still rejects it. Guards against a future refactor that
        # swaps `.resolve()` for a purely lexical normalize (which would silently reopen this).
        link = tmp_path / "web" / "dist" / "leak.key"
        link.symlink_to(tmp_path / "config" / "secret.key")
        status, body, _headers = asyncio.run(_asgi_get(spa_app, "/leak.key"))
        assert SECRET.encode() not in body
        assert b"app shell" in body
        assert status == 200

    def test_assets_mount_traversal_is_blocked(self, spa_app):
        # The /assets StaticFiles mount has its own containment check; a traversal there is
        # rejected outright (404) rather than falling through — either way, no leak.
        status, body, _headers = asyncio.run(_asgi_get(spa_app, "/assets/../../config/secret.key"))
        assert SECRET.encode() not in body
        assert status == 404

    def test_real_bundle_file_is_served(self, spa_app):
        status, body, _headers = asyncio.run(_asgi_get(spa_app, "/robots.txt"))
        assert status == 200
        assert b"User-agent" in body

    def test_unknown_route_serves_the_shell(self, spa_app):
        status, body, _headers = asyncio.run(_asgi_get(spa_app, "/settings/curation"))
        assert status == 200
        assert b"app shell" in body


class TestTheShellIsNeverServedFromCacheWithoutAsking:
    """`index.html` is the only file that NAMES the hashed bundles.

    Served with no `cache-control`, the browser guesses freshness from `last-modified` — and a
    browser that guesses "still fresh" keeps the old shell AND the old bundle it names, so a deploy
    is invisible. Reported 2026-08-25 by an owner looking straight at wording that had already
    shipped, on a server already running the new build.
    """

    def test_the_shell_must_be_revalidated(self, spa_app):
        status, body, headers = asyncio.run(_asgi_get(spa_app, "/dashboard"))

        assert status == 200
        assert b"app shell" in body
        assert "no-cache" in headers.get("cache-control", ""), (
            f"the shell may be reused without asking: cache-control={headers.get('cache-control')!r}"
        )

    def test_the_shell_by_its_own_name_is_treated_the_same(self, spa_app):
        """`/index.html` reaches the file branch rather than the fallback — same file, same rule."""
        status, _body, headers = asyncio.run(_asgi_get(spa_app, "/index.html"))

        assert status == 200
        assert "no-cache" in headers.get("cache-control", "")

    def test_a_sibling_file_is_not_forced_to_revalidate(self, spa_app):
        """Only the shell. Everything else under the bundle is content-addressed or static, and
        making it all no-cache would throw away the caching the hashed filenames exist to enable."""
        status, _body, headers = asyncio.run(_asgi_get(spa_app, "/robots.txt"))

        assert status == 200
        assert "no-cache" not in headers.get("cache-control", "")
