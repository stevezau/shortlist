"""Transparent stdio-to-HTTP bridge for local MCP hosts.

The bridge has no tools, resources, catalog, or authorization policy of its own.
It forwards MCP session messages to the running Shortlist HTTP endpoint, where
the same authentication and application services used by remote clients remain
authoritative.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import anyio
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server.stdio import stdio_server

URL_ENV = "SHORTLIST_MCP_URL"
CREDENTIAL_ENV = "SHORTLIST_MCP_CREDENTIAL"


def _loopback(hostname: str | None) -> bool:
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """Validated bridge inputs; the credential is excluded from representations."""

    url: str
    credential: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> BridgeSettings:
        source = os.environ if environment is None else environment
        url = source.get(URL_ENV, "").strip().rstrip("/")
        credential = source.get(CREDENTIAL_ENV, "").strip()
        if not url:
            raise ValueError(f"{URL_ENV} is required and must be the canonical Shortlist MCP URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{URL_ENV} must be an absolute HTTP URL")
        if parsed.username or parsed.password:
            raise ValueError(f"{URL_ENV} must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{URL_ENV} must not contain a query or fragment")
        if not parsed.path.endswith("/mcp"):
            raise ValueError(f"{URL_ENV} must end in /mcp")
        if parsed.scheme == "http" and not _loopback(parsed.hostname):
            raise ValueError(f"{URL_ENV} requires HTTPS outside loopback")
        if (
            not credential.startswith("shla_")
            or not 20 <= len(credential) <= 8192
            or any(not 33 <= ord(char) <= 126 for char in credential)
        ):
            raise ValueError(f"{CREDENTIAL_ENV} must be a named local assistant credential")
        return cls(url=url, credential=credential)

    def __repr__(self) -> str:
        return f"BridgeSettings(url={self.url!r}, credential=<redacted>)"


async def _pump(source: AsyncIterable[Any], target: Any, cancel_scope: anyio.CancelScope) -> None:
    try:
        async for message in source:
            await target.send(message)
    finally:
        cancel_scope.cancel()


async def relay_streams(local_read: Any, local_write: Any, remote_read: Any, remote_write: Any) -> None:
    """Relay opaque SDK session messages until either transport closes."""
    async with anyio.create_task_group() as group:
        group.start_soon(_pump, local_read, remote_write, group.cancel_scope)
        group.start_soon(_pump, remote_read, local_write, group.cancel_scope)


async def run_bridge(settings: BridgeSettings) -> None:
    """Serve local stdio while authenticating to Shortlist over Streamable HTTP."""
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {settings.credential}"})
    http_client.follow_redirects = False
    async with (
        http_client,
        streamable_http_client(
            settings.url,
            http_client=http_client,
            terminate_on_close=False,
        ) as (remote_read, remote_write),
        stdio_server() as (local_read, local_write),
    ):
        await relay_streams(local_read, local_write, remote_read, remote_write)


def main() -> None:
    """CLI entry point. Errors go to stderr; stdout belongs only to MCP."""
    try:
        settings = BridgeSettings.from_environment()
    except ValueError as error:
        print(f"shortlist-mcp-stdio: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        anyio.run(run_bridge, settings)
    except KeyboardInterrupt:
        return
    except Exception:
        # Transport exception strings may include HTTP headers. Never render the bearer value.
        print(
            "shortlist-mcp-stdio: connection failed; check the endpoint and named assistant credential", file=sys.stderr
        )
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover - exercised through the installed entry point
    main()
