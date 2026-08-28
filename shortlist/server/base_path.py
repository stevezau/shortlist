"""Runtime base path for serving the SPA under a reverse-proxy prefix."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

ENV_VAR = "APP_BASE_PATH"

GLOBAL_NAME = "__SHORTLIST_BASE_PATH__"


def resolve_base_path(raw: str | None) -> str:
    """Normalise a configured base path to "" (root) or "/prefix"."""
    if raw is None:
        return ""
    candidate = raw.strip()
    if not candidate or candidate == "/":
        return ""
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    return candidate.rstrip("/")


def base_path_from_env(environ: dict[str, str] | None = None) -> str:
    """Read and normalise APP_BASE_PATH from the process environment."""
    source = os.environ if environ is None else environ
    return resolve_base_path(source.get(ENV_VAR))


def render_shell(html: str, base_path: str) -> str:
    """Rewrite the built shell so it loads from, and knows about, `base_path`."""
    if not base_path:
        return html
    rewritten = html.replace('="/assets/', f'="{base_path}/assets/')
    # JS quoting is not enough in a <script>: the HTML parser ends the element at a
    # literal "</script>" even inside a string.
    literal = json.dumps(base_path).replace("</", "<\\/").replace("<!--", "<\\!--")
    script = f"<script>window.{GLOBAL_NAME} = {literal};</script>"
    return rewritten.replace("<head>", f"<head>{script}", 1)


class BasePathMiddleware:
    """Publish `base_path` as the ASGI ``root_path`` so routing skips the prefix.

    ``path`` is left whole: Starlette subtracts ``root_path`` from it when matching,
    so stripping here too would subtract it twice and 404 the ``/assets`` mount.
    """

    def __init__(self, app: Any, base_path: str) -> None:
        self.app = app
        self.base_path = base_path

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if self.base_path and scope.get("type") in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.base_path or path.startswith(f"{self.base_path}/"):
                scope = dict(scope)
                scope["root_path"] = self.base_path
        await self.app(scope, receive, send)
