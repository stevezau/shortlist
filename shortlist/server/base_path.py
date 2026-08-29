"""Runtime base path for serving the SPA under a reverse-proxy prefix."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

ENV_VAR = "APP_BASE_PATH"

GLOBAL_NAME = "__SHORTLIST_BASE_PATH__"

#: A base path is a URL path and nothing else. A value carrying a query, a fragment, a space or a
#: protocol-relative "//" normalises without complaint and then matches no request path ever sent,
#: so the app serves from the root while the SPA believes it is prefixed — a blank page with
#: nothing in the log to explain it. The same value is interpolated into the shell's `src=`/`href=`
#: attributes, which are NOT escaped (the injected script is), so refusing junk at this one
#: boundary is what keeps it out of both sinks.
#:
#: Excluded by character rather than permitted by one, so a perfectly ordinary non-ASCII prefix
#: (`/фильмы`) still works: what is refused is the set that either breaks an HTML attribute or
#: cannot appear in a path the server is asked for. Requiring one or more non-slash characters per
#: segment is what rejects a protocol-relative `//host`.
_VALID_BASE_PATH = re.compile(r"^(?:/[^/\s\"'<>&?#\\\x00-\x1f]+)+$")


def resolve_base_path(raw: str | None) -> str:
    """Normalise a configured base path to "" (root) or "/prefix".

    Args:
        raw: The configured value, typically ``os.environ["APP_BASE_PATH"]``.

    Returns:
        ``""`` for a root deployment — including for a value this cannot use, which is reported
        with a warning rather than raised, so one typo does not put the container in a crash loop.
    """
    if raw is None:
        return ""
    candidate = raw.strip()
    if not candidate or candidate == "/":
        return ""
    if not candidate.startswith("/"):
        candidate = f"/{candidate}"
    candidate = candidate.rstrip("/")
    if not candidate:
        return ""
    if not _VALID_BASE_PATH.match(candidate):
        logger.warning(
            "{}={!r} is not a usable URL path — serving from the server root instead. "
            "Use a plain path with no query, fragment or spaces, e.g. /shortlist.",
            ENV_VAR,
            raw,
        )
        return ""
    return candidate


def base_path_from_env(environ: dict[str, str] | None = None) -> str:
    """Read and normalise APP_BASE_PATH from the process environment."""
    source = os.environ if environ is None else environ
    return resolve_base_path(source.get(ENV_VAR))


def render_shell(html: str, base_path: str) -> str:
    """Rewrite the built shell so it loads from, and knows about, `base_path`.

    Rewriting the HTML is enough only because the bundle contains no absolute asset URLs of its
    own: the build emits a single JS and a single CSS file, both named here. Add a `React.lazy`
    route or a webfont and Vite starts writing `/assets/...` INSIDE the JS/CSS, where nothing
    rewrites it — the app would then break behind a prefix as a blank page, not a failing test.
    Either move to a relative `base` in `vite.config.ts` at that point, or keep the bundle flat.
    """
    if not base_path:
        return html
    if "<head>" not in html:
        # Loud, because the quiet version is a white screen: the assets would still load from the
        # right place, so the app boots, and then every API call goes to the server root and 404s.
        raise RuntimeError(
            "the app shell has no <head> element, so the base path could not be published to the "
            f"SPA — {ENV_VAR} cannot be honoured. This is a build problem, not a configuration one."
        )
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
