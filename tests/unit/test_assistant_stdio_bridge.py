from __future__ import annotations

import contextlib

import anyio
import pytest

from shortlist.server.assistant.stdio_bridge import BridgeSettings, relay_streams


def test_bridge_settings_require_named_assistant_credential_and_canonical_endpoint() -> None:
    settings = BridgeSettings.from_environment(
        {
            "SHORTLIST_MCP_URL": "http://127.0.0.1:5959/mcp",
            "SHORTLIST_MCP_CREDENTIAL": "shla_named-secret-long-enough",
        }
    )

    assert settings.url == "http://127.0.0.1:5959/mcp"
    assert repr(settings) == "BridgeSettings(url='http://127.0.0.1:5959/mcp', credential=<redacted>)"

    invalid = (
        (
            {
                "SHORTLIST_MCP_URL": "http://server.lan:5959/mcp",
                "SHORTLIST_MCP_CREDENTIAL": "shla_long-enough-credential",
            },
            "HTTPS",
        ),
        (
            {
                "SHORTLIST_MCP_URL": "https://example.test/mcp?token=x",
                "SHORTLIST_MCP_CREDENTIAL": "shla_long-enough-credential",
            },
            "query",
        ),
        ({"SHORTLIST_MCP_URL": "https://example.test/mcp", "SHORTLIST_MCP_CREDENTIAL": "shl_owner"}, "named"),
    )
    for environment, message in invalid:
        with pytest.raises(ValueError, match=message):
            BridgeSettings.from_environment(environment)


def test_relay_forwards_both_directions_without_interpreting_messages() -> None:
    async def exercise() -> None:
        async with contextlib.AsyncExitStack() as streams:

            async def pair() -> tuple:
                send, receive = anyio.create_memory_object_stream[object](1)
                return await streams.enter_async_context(send), await streams.enter_async_context(receive)

            local_in_send, local_in_receive = await pair()
            local_out_send, local_out_receive = await pair()
            remote_in_send, remote_in_receive = await pair()
            remote_out_send, remote_out_receive = await pair()
            async with anyio.create_task_group() as group:
                group.start_soon(
                    relay_streams,
                    local_in_receive,
                    local_out_send,
                    remote_in_receive,
                    remote_out_send,
                )
                request = object()
                response = object()
                await local_in_send.send(request)
                assert await remote_out_receive.receive() is request
                await remote_in_send.send(response)
                assert await local_out_receive.receive() is response
                await local_in_send.aclose()

    anyio.run(exercise)


@pytest.mark.parametrize("credential", [None, "ordinary-owner-token-must-never-be-used"])
def test_cli_rejects_missing_or_owner_credentials_without_protocol_output(credential):
    import os
    import subprocess
    import sys

    environment = {**os.environ, "SHORTLIST_MCP_URL": "http://127.0.0.1:1/mcp"}
    environment.pop("SHORTLIST_MCP_CREDENTIAL", None)
    if credential is not None:
        environment["SHORTLIST_MCP_CREDENTIAL"] = credential
    result = subprocess.run(
        [sys.executable, "-m", "shortlist.server.assistant.stdio_bridge"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "named local assistant credential" in result.stderr
    assert credential is None or credential not in result.stderr
