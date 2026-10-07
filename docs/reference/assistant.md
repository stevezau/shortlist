---
title: "MCP assistant reference"
description: Shortlist's assistant tools, scoped permissions, reviewed changes, operation receipts and external-call limits.
heading: MCP assistant reference
updated: 2026-10-07
---

Shortlist serves MCP over Streamable HTTP at its configured canonical `/mcp` endpoint. It is
disabled until `SHORTLIST_MCP_URL` is set. The optional `shortlist-mcp-stdio` command forwards the
same protocol over a local process. See [Connect an assistant](../guides/assistant-access.md) for
local credentials, OAuth, proxy routes and client configuration.

## Discover the contract

The MCP tool list is the authoritative input schema. Inputs are strict: unknown fields and values
with the wrong JSON type are rejected. IDs come from discovery tools. Dynamic row and settings
fields come from their catalog, which supplies defaults, accepted values, descriptions and
effects. `shortlist_list_templates` also returns one top-level `field_definitions` object for row
values (including nested `poster`, `hub_anchor` and `ai_instructions`) plus `creation_defaults`;
the `items` entries remain the named editor starting points.

Tools return a human-readable `summary`, structured `data`, `warnings`, an optional `next_action`
and an `observed_at` timestamp. A successful plan describes a possible change; it does not mean
the change was applied or that an external service has completed its work.

Tools with an input object take that object under `request`. For example, a title lookup is:

```json
{
  "request": {
    "query": "Arrival",
    "media": "movie",
    "year": 2016,
    "limit": 5
  }
}
```

Use the title and TMDB ID returned by that lookup. Theme plans require every supplied pick to
have been resolved through `shortlist_search_titles`; a remembered ID from a chat is insufficient.

## Tool families

**Installation and guidance:** `shortlist_get_instance`, `shortlist_get_setup_status`,
`shortlist_get_guide` and `shortlist_diagnose`. Instance discovery includes current connection
permissions and the remaining lifetime allowance for assistant provider requests. The
`shortlist://guides/{topic}` resource provides the setup, rows, themes, permissions and
troubleshooting guides to clients that support resources.

**Configuration:** `shortlist_describe_settings`, `shortlist_get_configuration`,
`shortlist_get_choices` and `shortlist_plan_configuration`. The catalog describes settings without
revealing secret values. `shortlist_get_choices` pages foreign Plex placement anchors for one
permitted library, or quality profiles and root folders from one permitted saved Radarr or Sonarr
destination. Its external names and paths are descriptive input, not trusted configuration. Only
permitted groups can be read or changed. Secret entry uses `shortlist_start_connection` and
`shortlist_get_connection_status`, with credentials entered in the owner browser.
`shortlist_check_connection` runs supported non-generating probes against an authorized saved
destination; paid provider, search and webhook tests use browser handoffs.

**Rows and themes:** `shortlist_list_templates`, `shortlist_list_rows`, `shortlist_get_row`,
`shortlist_list_themes`, `shortlist_get_theme`, `shortlist_search_titles`, `shortlist_plan_theme`,
`shortlist_plan_row` and `shortlist_plan_setup`. Setup atomically saves one theme and its new,
disabled row with an empty schedule. Generic row creation retains the template or default cron
unless a schedule is supplied. A stored cron does not run while the row is disabled; activation
is a separate plan. Row plans can explicitly set `ai_paused: true` to prevent automatic theme
top-ups.

**People, libraries and seasons:** `shortlist_list_people`, `shortlist_list_libraries`,
`shortlist_get_person_row_settings`, `shortlist_list_seasons`, `shortlist_plan_people` and
`shortlist_plan_season`. Every current and future person is available to an approved connection;
row audiences and sharing rules still apply. The per-person settings read returns only a permitted
person's stored and effective row overrides, never history or picks. Row and library scope applies
to resolved effects, including indirect effects. `shortlist_plan_people` accepts at most 25 sparse
`row_overrides` entries for that one person: omit a field to preserve it, and use `null` for a
numeric override to restore row inheritance. Disabled people and per-person rows can be
preconfigured without enabling delivery. A legacy shared-row record returns `supported: false`,
its stored values and no effective values; it is read-only through MCP.

**Generation and execution:** `shortlist_generate_theme`, `shortlist_plan_schedule`,
`shortlist_preview_row` and `shortlist_plan_run`. Provider generation creates a draft through a
queued operation; it does not save a theme automatically. Preview uses dry-run. Run plans require
an explicit selection and explicit dry-run choice. Runs using AI, web search, AI posters or
automatic acquisition also need explicit usage allowances and the corresponding permissions.

**Requests and maintenance:** `shortlist_list_requests`, `shortlist_plan_requests` and
`shortlist_plan_maintenance`. Request actions are reject, restore, archive and send; each plan
has an explicit bounded set of candidate IDs. Maintenance supports named cache refresh and row
cleanup. Cleanup removes delivered Shortlist-owned collections and requires exact review.
Uninstall remains in the owner browser.

**Review and progress:** `shortlist_get_change`, `shortlist_apply_change`,
`shortlist_get_operation`, `shortlist_cancel_operation`, `shortlist_list_runs`,
`shortlist_get_run_report` and `shortlist_get_activity`. Results are filtered by current authority.
An operation receipt can show progress without retaining access to data removed from the grant.

## Plan, approve, apply

1. A plan resolves the requested change against the current database, determines its permissions
   and effects, and records dependency hashes. Preparation makes no provider or acquisition call.
2. The plan supplies a change ID and identifies any missing authority or exact owner review.
   Sensitive changes open a browser review page showing the concrete plan and external effects.
3. The owner approves that exact content and grant revision. A chat reply does not replace this
   approval. A changed dependency or plan requires a new review.
4. Apply reloads current ownership and permissions and validates the plan again. Local changes,
   audit entries, operation receipt and queued obligations commit together in one transaction.
5. Follow the operation until it completes. External work runs through Shortlist's existing jobs
   queue and is distinct from the local configuration transaction.

An apply call has this shape; replace the sample values with the returned change ID and a new
stable identifier:

```json
{
  "request": {
    "change_id": "returned-change-id",
    "idempotency_key": "setup-living-room-2026-10-05"
  }
}
```

Reuse the **same** change ID and idempotency key after a timeout. That retrieves the existing
operation instead of creating duplicate work. A stale plan is rejected before mutation. Inspect
the new state and prepare a fresh plan only after determining the original operation's outcome.

Cancellation is a request to stop cancellable work. It cannot undo a completed external call or
discard privacy and cleanup obligations already owed by a committed change.

## Permissions and costs

A named grant combines capabilities with row, library, settings-group and destination limits. An
approved connection can work with all current and future people; row audiences and sharing rules
continue to control who receives each row. A capability alone is insufficient for a settings-group,
library or external-service read: the corresponding selected resource must also be in the grant.
OAuth access is additionally restricted to the token's granted scopes. Permissions are
checked in discovery, preparation, apply and before deferred external work starts. Revocation,
expiry or an ownership change prevents new authorized work.

Reading watch-history details, exporting history-derived context, changing privacy, making
acquisition requests and invoking a paid provider are separate powers. A plan needing several
powers requires all of them. Assistant tools cannot read secrets, issue credentials, expand their
own grant or invoke arbitrary HTTP, SQL, shell commands or job types.

Theme generation and immediate runs reserve provider requests from a finite lifetime allowance
on the grant. Reservation and operation creation are atomic. This allowance counts outgoing API
requests; provider prices and their internal billable work determine the monetary cost. Output
tokens are also bounded. The allowance does not cap normal row automation approved by the owner.
An immediate run reserves its full `max_provider_calls` permanently, including unused slots and
cache hits. There is no automatic refund; choose a small allowance appropriate to the work.
The named theme-generation tool does not include personal watch history in its prompt. Runs can
use a person's history and send derived context to approved providers only with those separate
permissions.

### Budgeted immediate runs

`shortlist_plan_run` and `shortlist_preview_row` accept explicit limits. Provider and acquisition
allowances default to zero. Discover row and person IDs first; this example requests one run
with up to eight provider requests, one generated image and two acquisitions:

```json
{
  "request": {
    "row_ids": [1],
    "person_ids": [2],
    "dry_run": false,
    "include_shared": false,
    "max_provider_calls": 8,
    "max_output_tokens": 2048,
    "max_native_tool_uses": 2,
    "max_images": 1,
    "max_acquisitions": 2,
    "allow_provider_managed_search": false
  }
}
```

The plan resolves the selected rows, people, provider models, destinations and settings. The
worker rechecks that contract before each provider request. Changed authority or configuration
stops new paid work. Each external search request, completion, native-search request and image
request consumes a provider slot. An image request produces at most one image and also consumes
its separate image allowance. Cache hits do not make provider requests. Compatible model servers
need an explicitly configured model; a bounded run does not discover and choose one at runtime.

Output-token limits include reasoning/thinking tokens. OpenAI native search also sets
[`max_tool_calls`](https://developers.openai.com/api/reference/resources/responses/methods/create);
Anthropic sets the web-search tool's
[`max_uses`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool).
Google native Search offers no
search-count ceiling: one API request may generate multiple billable internal searches. Using
that path requires `allow_provider_managed_search: true` and exact owner approval in the browser.
The approval explains the internal-search and monetary uncertainty. Google with an external
search provider uses the ordinary request limits. See Google's
[Search Grounding](https://ai.google.dev/gemini-api/docs/google-search/) and
[thinking-token limits](https://ai.google.dev/gemini-api/docs/generate-content/thinking) documentation.

A preview can spend on AI selection or search. It skips image rendering and acquisition sends,
so its image and acquisition allowances can remain zero. A run never silently switches off a
configured paid feature to fit a missing allowance. Provider errors are not automatically retried
under a bounded run, including schema fallback calls. An uncertain outcome consumes its slot
and stops further paid work in that run; inspect the operation and run report before starting
another operation.

Recurring configuration deserves separate review. An enabled themed row can be topped up by
Shortlist's provider even when its saved theme was authored externally. `ai_paused: true` prevents
that top-up. Schedule, provider, prompt, poster and acquisition changes can alter recurring
effects. Disabling or revoking an assistant grant does not disable schedules it already created.

## External outcomes and recovery

Provider calls and acquisition sends record a durable checkpoint before dispatch. A crash or
timeout after that checkpoint can leave the outcome unknown. Shortlist preserves the reservation
and reports uncertainty rather than automatically spending or sending again.

An acquisition plan freezes each title, destination and request configuration. Before each title
starts, the worker rechecks the owner, grant, saved settings and candidate. A changed destination
or credential prevents dispatch under the old plan. An uncertain title remains claimed across
new plans and candidate archival; inspect the destination before taking recovery action.

Manual inbox sends and scheduled acquisition use the same durable claim checks. In **Requests →
Acquisition checks needing review**, the owner can inspect a terminal outcome and choose **Allow
retry** after checking the actual service. The browser submits the exact reviewed claim and
status; active claims cannot be released. Release removes the retry restriction and does not send
anything. This recovery authority is not exposed through MCP.

Committed privacy convergence still runs when a grant is later revoked. This closes work already
owed by a local change; it does not authorize a new top-level operation. Operation results and
activity are scoped summaries, not raw server logs or vendor exception bodies.

## Scope of setup

MCP configures an existing, owner-claimed Shortlist installation. Installing the container,
claiming ownership and entering secrets remain local or browser steps. A local coding assistant
can help with installation using its own separately authorized machine tools.

Shortlist's HTTP and stdio protocol paths have automated SDK integration tests. Hosted ChatGPT
and Claude connection screens still require release validation against those actual clients;
protocol tests alone do not establish current account-plan or connector compatibility.

## Browser and OAuth endpoints

The browser management API is separate from the assistant's MCP credential. Grant administration
and exact approvals require the owner browser session; mutations also require its CSRF token.
The legacy owner API bearer and an assistant bearer cannot substitute for that session.

- `GET /api/assistant/status` reports enablement, canonical resource, issuer and available presets.
- `GET /assistant/grants` lists named grants; `POST /assistant/grants` creates one.
- `POST /assistant/grants/{grant_id}/credentials` creates a one-time local credential;
  `POST /assistant/grants/{grant_id}/revoke` revokes the grant.
- `GET /api/assistant/changes/{change_id}` returns an owner's concrete review;
  `POST /api/assistant/changes/{change_id}/approve` records its exact approval.
- `POST /assistant/oauth/register` registers a public PKCE client with validated redirect URIs.
- `GET /assistant/oauth/authorize` opens browser consent; its `POST` form starts a bound consent
  flow. `POST /assistant/oauth/consent` finishes the owner's decision and returns the callback URL.
- `POST /assistant/oauth/token` exchanges authorization codes or refresh tokens;
  `POST /assistant/oauth/revoke` revokes supported OAuth credentials.

All application routes include `APP_BASE_PATH` when configured. RFC discovery documents also
have origin-root aliases. Their exact paths are documented in the
[reverse proxy guide](../guides/assistant-access.md#reverse-proxy-routes); forwarding only the
application prefix is insufficient for clients using those aliases.
