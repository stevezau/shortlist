---
title: Connect an assistant
description: Enable Shortlist's MCP endpoint, give an assistant a named and limited grant, and connect local or hosted clients without sharing the owner API token.
heading: Connect an assistant
updated: 2026-10-05
---

Assistant access is optional. It gives an MCP client a named connection to Shortlist, with its own
permissions, limits, expiry and revoke switch. The connection can inspect an installation, explain
settings, prepare changes and carry out the work its grant allows.

This is separate from **Settings → System → API access**. That API token has the owner's full power.
Never put it in an MCP client. Assistant credentials begin with `shla_` and work only at the MCP
endpoint.

## Before connecting

Shortlist must already be installed and claimed by its Plex server owner. A hosted chat cannot
install a container through a server that does not exist, and signing into an OAuth consent page
does not claim a new Shortlist installation. For a new install, complete the normal
[setup wizard](../getting-started.md) in a browser first.

Choose the one canonical address that clients will use. It must finish with `/mcp` and include the
base path if Shortlist has one:

```text
https://media.example.com/shortlist/mcp
```

Loopback HTTP is also accepted when the client and Shortlist run on the same machine:

```text
http://127.0.0.1:5959/mcp
```

Set that exact address as `SHORTLIST_MCP_URL` on the Shortlist container and restart it. Shortlist
does not infer this security-sensitive address from a request's `Host` or forwarding headers.
Plain HTTP on a LAN address is refused; use HTTPS when the address is not loopback.

Open **Settings → AI assistants** beside the settings search, or search Settings for **MCP**,
**ChatGPT**, **Claude** or **Codex**. The same entry is under **System → AI assistants**, where
the card should say **Enabled**. Follow **Manage connections**, then **New connection** to create a grant.

## Choose what the connection can do

Start with the smallest useful preset:

- **Inspect and propose** reads safe configuration and activity and can prepare changes for owner
  review.
- **Manage selected rows** can work with the rows, people and libraries you select.
- **Owner automation** can run approved administration within the limits you choose. It is still a
  named, revocable grant; it is not the owner API token.

Then narrow the grant. Select exact people, rows, libraries and settings groups. **Include future**
options are broader: they let the same connection reach objects added later. Destination values
bind history-derived data to an exact configured service or URL. Changing a service URL does not
silently carry the old approval to a new destination.

Extra permissions are explicit. For example, showing watch details to the assistant, sending
history-derived context to a provider, making acquisition requests and spending an AI provider call
are separate choices. A tool that needs several permissions needs all of them.

The provider-call allowance is a finite lifetime total for this named connection. `0` permits no
provider calls. A call whose outcome is unknown still consumes its reservation, because retrying it
could spend twice. This is a call count, not a dollar limit; the assistant host, search provider and
request services can have their own charges or quotas. This allowance covers the assistant's named
theme-generation tool and bounded immediate runs, including outgoing search and image requests.
An immediate run permanently reserves its whole requested maximum, even when it uses fewer calls.
It does not cap ordinary recurring row automation that an owner approves.

## Connect on the same machine

Press **New local credential** on the grant and copy the value before closing the dialog. Shortlist
stores only a verifier and cannot show it again. Keep it in the client's credential store when the
client has one. An environment variable is the fallback shown below; do not put the value in a URL,
command argument, chat message or log.

### Direct Streamable HTTP

A local MCP client that supports bearer credentials can connect straight to the canonical URL. For
Codex, export the one-time value in the environment that launches Codex, then add this to
`~/.codex/config.toml`:

```toml
[mcp_servers.shortlist]
url = "http://127.0.0.1:5959/mcp"
bearer_token_env_var = "SHORTLIST_MCP_CREDENTIAL"
```

```bash
read -rsp 'Shortlist assistant credential: ' SHORTLIST_MCP_CREDENTIAL
export SHORTLIST_MCP_CREDENTIAL
```

Codex reads the bearer value from the environment rather than the TOML file. Its
[MCP configuration reference](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) documents both
HTTP servers and environment-backed credentials.

### Optional stdio bridge

Some desktop clients launch a local process instead of making an HTTP MCP connection. Shortlist's
`shortlist-mcp-stdio` command is a transparent bridge: it forwards MCP messages to the same HTTP
endpoint. It has no second tool catalog, database access or authorization rules.

When Shortlist runs in Docker, the client can launch the command inside the existing container.
Replace `<container>` with its name:

```toml
[mcp_servers.shortlist]
command = "docker"
args = ["exec", "-i", "-e", "SHORTLIST_MCP_URL", "-e", "SHORTLIST_MCP_CREDENTIAL", "<container>", "shortlist-mcp-stdio"]
env_vars = ["SHORTLIST_MCP_URL", "SHORTLIST_MCP_CREDENTIAL"]
```

Export both values before launching the client. `SHORTLIST_MCP_URL` must be the same canonical URL
configured on the server. The bridge refuses URL credentials, a legacy `shl_` owner token, and
plain HTTP outside loopback. It writes protocol messages only to stdout and sends a redacted startup
error to stderr.

The canonical URL must also be reachable from inside the container. With a different host port or
an address reachable only from the host, run the bridge from a local Shortlist installation instead,
or use the canonical HTTPS address that both environments can reach.

Claude Code supports [environment references in `.mcp.json`](https://code.claude.com/docs/en/mcp).
Export the values in the shell that launches Claude Code:

```bash
export SHORTLIST_MCP_URL='http://127.0.0.1:5959/mcp'
read -rsp 'Shortlist assistant credential: ' SHORTLIST_MCP_CREDENTIAL
export SHORTLIST_MCP_CREDENTIAL
```

Then add this server to `.mcp.json`, preserving the literal `${...}` references:

```json
{
  "mcpServers": {
    "shortlist": {
      "command": "docker",
      "args": ["exec", "-i", "-e", "SHORTLIST_MCP_URL", "-e", "SHORTLIST_MCP_CREDENTIAL", "<container>", "shortlist-mcp-stdio"],
      "env": {
        "SHORTLIST_MCP_URL": "${SHORTLIST_MCP_URL}",
        "SHORTLIST_MCP_CREDENTIAL": "${SHORTLIST_MCP_CREDENTIAL}"
      }
    }
  }
}
```

The credential travels in the child process environment, without being stored in this JSON or
expanded into a command argument. Review project MCP configuration before trusting it.

## Connect a hosted assistant

A hosted client uses the same canonical `/mcp` address and Shortlist's OAuth flow. Do not create a
local credential for it. Adding the connection opens Shortlist's owner login and a consent screen
that names the client, resource and requested permissions. Shortlist uses authorization code with
S256 PKCE and rotates refresh credentials; revoking the grant stops both current and refreshed
access.

The network path depends on the host:

- ChatGPT developer connections need public HTTPS or a supported
  [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). Tunnel access
  depends on OpenAI account, workspace and organization permissions. The MCP path can travel through
  a tunnel while the browser-facing OAuth server may still need its own reachable route.
- Claude custom connectors connect from Anthropic's cloud, including when configured in Claude
  Desktop. Anthropic's [remote connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
  says the endpoint must be reachable from its published IP ranges. A local Claude Desktop stdio
  server is a different connection mode.

Enter the canonical `/mcp` URL in the host's custom MCP connection screen and choose OAuth when it
offers authentication choices. Account plan and workspace policy can control whether that screen is
available.

Shortlist's protocol, discovery, registration and consent flows have automated and SDK-level tests.
The complete current ChatGPT and Claude hosted connection screens have not yet been validated
end-to-end against a released Shortlist image. Treat those two recipes as compatibility testing until
the release notes name a verified host and version.

## Reverse proxy routes

With `APP_BASE_PATH=/shortlist` and this resource URL:

```text
https://media.example.com/shortlist/mcp
```

the proxy must forward `/shortlist/` as usual. OAuth clients also look up two RFC discovery URLs at
the origin root, so forward these to the same Shortlist container too:

```text
/.well-known/oauth-protected-resource/shortlist/mcp
/.well-known/oauth-authorization-server/shortlist/assistant/oauth
```

Those paths deliberately sit outside `/shortlist/`. Test the MCP endpoint, both discovery documents,
the owner login/consent page and the client's callback through the public address. Do not rewrite the
canonical resource or issuer to an internal container address. See
[Serving from a subpath](../reference/environment.md#serving-from-a-subpath) for the normal proxy
configuration.

## Secrets and owner handoffs

Assistant tools never accept or return Plex tokens, API keys or passwords. When a connection needs
one, Shortlist returns an opaque, grant-bound handoff and opens the exact card under **Settings →
Connections**. Enter the secret there in the owner browser. The available cards cover Plex, TMDB,
AI providers and web search (including Exa and SearXNG), Tautulli, Trakt, MDBList, Overseerr,
Radarr, Sonarr and notification webhooks.

A handoff status says only that it is waiting, changed, configured or expired. **Configured does not
prove that the provider accepted the credential or that it is reachable.** Use the card's safe Test
action where available. Some tests make a real external request and can use a provider quota; a
notification test sends a real webhook.

For a change outside a grant's standing authority, Shortlist gives the assistant a link such as
`/assistant/changes/<id>`. The owner page shows the resolved plan, required authority and external
effects. Approval covers that content hash and grant revision once; changing the plan invalidates it.

## Build rows with an assistant

Start by asking the assistant to inspect the installation, available libraries, people, row
templates and settings catalog. Those responses include field descriptions, allowed values,
constraints, side effects and the connection's current permissions. The assistant should use the
returned IDs and schemas rather than inventing them.

A complete workflow is:

1. Describe the rows, audience and schedule you want. Let the assistant discover the current
   installation and resolve title names to TMDB IDs.
2. Ask it to prepare a theme and an inactive row together with `shortlist_plan_setup`. The theme
   can contain picks written by ChatGPT or Claude. Saving those picks makes no Shortlist provider
   call. Include `ai_paused: true` to keep Shortlist from later topping up that theme automatically.
3. Review the resolved plan. Apply it using its change ID and an idempotency key. Both objects
   are saved together, or neither is saved.
4. Configure audience, libraries and automation in a separate row plan. Enabling paid recurring
   work requires the owner's exact browser approval. Fixed themes also receive AI top-ups unless
   AI is paused; a saved list alone does not imply that automation is free.
5. Preview a run with dry-run enabled, then apply a live run and follow its operation receipt and
   run report. Include explicit provider-request and output-token limits for paid AI or search,
   an image allowance for AI artwork, and an acquisition allowance for automatic requests.
   These allowances default to zero. A preview can still spend on AI selection and search; it
   skips image rendering and acquisition sends.

Immediate runs check the approved provider, model, destination and current permission before each
paid request. They disable automatic retries and record uncertain outcomes. Google native Search
needs exact browser approval because Google does not expose a limit on its internal billable
searches. The [budgeted-run reference](../reference/assistant.md#budgeted-immediate-runs) gives the
input fields and explains which limits Shortlist can enforce.

`shortlist_generate_theme` is an alternative when you want Shortlist's configured provider to
write the draft. It reserves one provider call, limits output tokens and returns a draft through
the operation receipt. Saving that draft is a separate change. It does not send personal watch
history to the provider.

Other tools manage people, seasons, non-secret settings and request candidates within the grant's
scope. Sending acquisition requests uses a fixed, reviewed batch and rechecks access before each
title. An uncertain external result is not sent again automatically, even through a new plan.
Open **Requests → Acquisition checks needing review** and inspect the actual destination service.
After checking whether the title arrived, use **Allow retry** only if another send is appropriate.
Releasing a claim does not send the title. Active claims cannot be released. This recovery is an
owner-browser action; an assistant cannot override its own uncertain outcome.

Maintenance tools name their effects: a cache refresh is separate from removing delivered rows.
Row cleanup removes Shortlist-owned collections and needs exact owner approval; an enabled row can
create them again on its next run. Uninstall remains an owner-browser action.

For the tool families, plan lifecycle and permission boundaries, see the
[assistant reference](../reference/assistant.md).

## Revoke or stop ongoing work

Return to **AI assistants** and revoke the named grant. Local credentials, OAuth access and
refresh credentials tied to it stop working. The legacy owner API token is unaffected because it is
a separate credential.

Revocation does not silently delete or disable work the connection already created. In particular,
a schedule can continue after its grant expires or is revoked. Review **Activity → Jobs**, disable
the schedule or row if needed, and use Shortlist's normal undo or repair flow for completed changes.
