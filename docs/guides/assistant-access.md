---
title: Connect an assistant
description: Enable Shortlist's MCP endpoint, give an assistant a named and limited grant, and connect local or hosted clients without sharing the owner API token.
heading: Connect an assistant
updated: 2026-10-07
---

Assistant access is optional. It gives an MCP client a named connection to Shortlist, with its own
permissions, limits, expiry and revoke switch. The connection can inspect an installation, explain
settings, prepare changes and carry out the work its grant allows.

This is separate from **Settings → System → API access**. That API token has the owner's full power.
Never put it in an MCP client. Assistant credentials begin with `shla_` and work only at the MCP
endpoint.

## What an assistant can manage

MCP supports assisted setup after Plex ownership is linked, the advertised non-secret settings,
people and their row overrides, saved themes, rows, audiences, seasons, schedules, recommendation
previews, authorized runs, requests and supported maintenance. Use the current tool schemas and
catalogs to discover the available fields and operations. A configured integration still needs a
successful service test; a saved row still needs a successful run and delivery check.

For an existing per-person Explore row, `shortlist_plan_people` can select a saved
`up_next_theme_id` in `row_overrides`. This replaces the queued theme without generating a new
theme or changing Plex immediately. Inspect it with `shortlist_get_person_row_settings` when
the connection also permits the personal theme details; a redacted read does not mean the
selection failed. Saving an assistant-authored theme and asking Shortlist to generate one are
separate workflows, with separate provider-call permission for the latter.

MCP does not expose every owner API or browser action. Installation, deployment timezone and URL,
Plex ownership, secret entry, connection permissions and exact approvals require the owner or
deployment operator. Custom poster file uploads, backup and restore, watching-account transfer
and undo, notification test sends, owner support diagnostics and uninstall also remain owner or
deployment actions. Releasing an uncertain acquisition for retry requires owner inspection because
a retry could repeat a request.

An assistant can explain those steps and continue after the owner completes them. It must not
claim to have performed an unsupported action, use the unrestricted owner API token as a fallback,
or treat one approved change as authority for unrelated actions. These boundaries apply even to
the **Owner automation** preset.

## Before connecting

Shortlist must already be installed and claimed by its Plex server owner. A hosted chat cannot
install a container through a server that does not exist, and signing into an OAuth consent page
does not claim a new Shortlist installation. Plex ownership is therefore a browser setup step.

After an owner is linked, an assistant can help complete the validated setup workflow. Start with
`shortlist_get_setup_status`; it separately reports credentials, library discovery, roster discovery
and wizard completion. A check with `ready: null` and `blocked_by: "missing_permission"` is
unavailable to this connection, not a failed setup step. Review the connection's access instead of
repeating Plex login or setup based on that result. If TMDB metadata is missing, `shortlist_start_connection` for `tmdb` opens
the canonical browser setup step for entering the key. It never accepts a key in MCP. When people
are missing, prepare `shortlist_plan_maintenance` with `task: "people.sync"`; the owner reviews the
exact roster/privacy reconciliation and the resulting operation queues Shortlist's existing durable
sync. Once ownership, metadata, a usable movie or show library and a non-removed roster are ready,
prepare `task: "setup.complete"` for owner review. That task validates the prerequisites before it
marks the wizard complete. Do not try to write `setup.completed` through generic configuration.

The setup result also names the installation timezone. It is deployment-managed, so an assistant
can use it when planning cron schedules but cannot change it through MCP.

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

If the setup wizard is still open, begin the OAuth connection from your MCP client. Its owner
consent page is available after Plex ownership is linked, before the wizard is complete. Allowing
the connection can create a grant for the requested permissions without opening Settings first.
The initial browser grant includes future rows and libraries, but has no selected setting groups
or destinations and permits zero provider calls. Some reads therefore remain unavailable. Use the
documented setup handoffs and exact owner reviews; those reviews do not expand standing read access
or provider quotas. After setup completes, the owner can adjust resources and limits in the
connections page below.

Open **Settings → AI assistants** to reach the connections page, then choose **New connection**.
You can also search Settings for **MCP**, **ChatGPT**, **Claude** or **Codex**. Under
**System → AI assistants**, the card should say **Enabled**; its **Manage connections** button
opens the same page.

## Choose what the connection can do

Start with the smallest useful preset:

- **Inspect and propose** reads safe configuration and activity and can prepare changes for owner
  review.
- **Manage selected rows** can work with the rows and libraries you select.
- **Owner automation** can run approved administration within the limits you choose. It is still a
  named, revocable grant; it is not the owner API token.

Every new connection can work with all current and future people. Row audiences and existing Plex
sharing rules still decide who can see each row. You can narrow rows, libraries and settings groups;
**Include future** options let the same connection reach objects added later. Destination values
bind history-derived data to an exact configured service or URL. Changing a service URL does not
silently carry the old approval to a new destination.

Capabilities do not override those resource limits. For example, an assistant with `config.read`
still needs the selected settings group or library, and `connections.manage` still needs the exact
saved destination, before Shortlist reads its configured service. `shortlist_get_choices` can page
foreign Plex placement anchors for an allowed library or Arr quality profiles and root folders for
an allowed saved destination. It can also page model IDs from the already saved AI provider. It
never accepts a caller-provided URL or credential; a disabled, unsupported or unavailable provider
is reported separately from a provider whose model list is simply empty.

Extra permissions are explicit. For example, showing watch details to the assistant, sending
history-derived context to a provider, making acquisition requests and spending an AI provider call
are separate choices. A tool that needs several permissions needs all of them.

The same rule applies to setting groups and destinations. An assistant may configure permitted
non-secret settings, but it cannot enter provider, Arr, Plex or webhook credentials. It may set a
configured provider and model when its recommendation group is allowed, while the owner browser
still owns credential entry and a real provider test. It may set non-secret notification enablement,
event selection and header name when the notifications group is allowed; the webhook URL and
authentication value stay in the browser, and the real notification test sends a message there.

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

For ChatGPT, open **Plugins → + → Add custom MCP server**, enter the canonical URL and choose
OAuth. Shortlist advertises dynamic client registration (DCR); select DCR if the client asks for
the registration method. Complete Shortlist's owner consent, then install the resulting plugin
and select it in a conversation. Ask it to call `shortlist_get_instance` and
`shortlist_get_setup_status` before making changes. See OpenAI's
[custom MCP connection guide](https://developers.openai.com/api/docs/guides/custom-mcp-server)
for the current client controls.

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
templates and settings catalog. The template response includes one shared row-field definition map
with accepted types, defaults, nested shapes and important effects, plus the MCP creation defaults.
The assistant should use those returned IDs and schemas rather than inventing them. New assistant
rows are inactive until explicitly enabled; their stored cron is not active while disabled.

A complete workflow is:

1. Describe the rows, audience and schedule you want. Let the assistant discover the current
   installation and resolve title names to TMDB IDs.
2. Ask it to prepare a theme and an inactive row together with `shortlist_plan_setup`. The theme
   can contain picks written by ChatGPT or Claude. Saving those picks makes no Shortlist provider
   call. That trusted supplied theme makes `ai_paused: true` valid when you want to prevent later
   top-ups; omit `ai_paused` for a non-themed row.
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

Read the report's `selected_row_ids` as the requested builds and `affected_row_ids` as its wider
privacy and shelf-management footprint (`row_ids` remains a compatibility alias for that footprint).
Shared `pick_count` counts selected candidates, not confirmed deliveries; a report with `dry_run:
true` is a preview that writes nothing to Plex. An `ok` row with zero candidates returns `no_picks`.
A shared row can complete no delivery because its audience lacks common viewing. Its safe
`reason_code` and guidance explain recognized cases; unknown cases require the owner to inspect
Runs. Review `privacy_warnings` too: Plex restriction profiles can prevent hiding other people's
rows even when the operation completed. The report omits private history and raw provider errors.

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
`people.sync` is the named, owner-reviewed roster/privacy reconciliation step; `setup.complete`
is the named, owner-reviewed validation that finishes an eligible wizard. Row cleanup removes
Shortlist-owned collections and needs exact owner approval; an enabled row can create them again
on its next run. Uninstall remains an owner-browser action.

For the tool families, plan lifecycle and permission boundaries, see the
[assistant reference](../reference/assistant.md).

## Revoke or stop ongoing work

Return to **AI assistants** and revoke the named grant. Local credentials, OAuth access and
refresh credentials tied to it stop working. The legacy owner API token is unaffected because it is
a separate credential.

Revocation does not silently delete or disable work the connection already created. In particular,
a schedule can continue after its grant expires or is revoked. Review **Activity → Jobs**, disable
the schedule or row if needed, and use Shortlist's normal undo or repair flow for completed changes.

After revocation, **Remove** clears the old connection record and its credentials from the connections
list. It does not erase Shortlist's activity history or audit record.
