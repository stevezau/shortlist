---
title: "MCP assistant reference"
description: Shortlist's assistant tools, View only and Manage Shortlist access, reviewed changes and configured runs.
heading: MCP assistant reference
updated: 2026-10-08
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
`shortlist_get_guide` and `shortlist_diagnose`. Instance discovery includes current effective
permissions and historical provider-call allowance where one exists. The
`shortlist://guides/{topic}` resource provides the setup, rows, themes, permissions and
troubleshooting guides to clients that support resources. Setup status separately reports
credentials, library discovery, roster discovery and wizard completion. A denied check returns
`ready: null` with `blocked_by: "missing_permission"`; aggregate readiness is also null when its
inputs are unavailable. This asks for access review, not repeated setup. A claimed installation can
prepare the named owner-reviewed `people.sync` maintenance step to reconcile its roster, then the
validated `setup.complete` step; it cannot set wizard flags through generic configuration. The
installation timezone is deployment-managed and read-only to MCP.

**Configuration:** `shortlist_describe_settings`, `shortlist_get_configuration`,
`shortlist_get_choices` and `shortlist_plan_configuration`. The catalog describes settings without
revealing secret values. `shortlist_get_choices` pages foreign Plex placement anchors for one
permitted library, or quality profiles and root folders from one permitted saved Radarr or Sonarr
destination. It also pages model IDs from the already saved AI provider; a provider that is disabled,
does not list models, or is unavailable is reported distinctly from a successful empty list. Its
external names, paths and model IDs are descriptive input, not trusted configuration. Only permitted
groups can be read or changed. Secret entry uses `shortlist_start_connection` and
`shortlist_get_connection_status`, with credentials entered in the owner browser.
`shortlist_check_connection` runs supported non-generating probes against an authorized saved
destination; paid provider, search and webhook tests use browser handoffs.

**Rows and themes:** `shortlist_list_templates`, `shortlist_list_rows`, `shortlist_get_row`,
`shortlist_list_themes`, `shortlist_get_theme`, `shortlist_search_titles`, `shortlist_plan_theme`,
`shortlist_plan_row` and `shortlist_plan_setup`. Setup atomically saves one theme and its new,
disabled row with an empty schedule. Generic row creation retains the template or default cron
unless a schedule is supplied. A stored cron does not run while the row is disabled; activation
is a separate plan. `ai_paused: true` prevents automatic theme top-ups only for a row with a saved
or trusted supplied theme: use `shortlist_plan_setup` or give `shortlist_plan_row` a valid
`theme_id`; omit it for a non-themed row.

Theme drafts also accept up to 20 TMDB keyword `tags` (`id`, `name`) and 10 Plex `collections`
(`section_key`, `section_title`, `title`), using the owner editor's source fields. Collection sources
require their libraries in the connection's scope; an exact approval does not grant ongoing read
access to them. `shortlist_get_theme` omits collections outside that scope. On a theme update,
omitting `tags` or `collections` preserves those sources; an explicit empty list clears them.

Each saved row ID identifies one row and is never reassigned after deletion. Existing historical
IDs remain reserved during upgrades, and retained history or pending cleanup also reserves the
row's slug. Discover a replacement row again and prepare a new plan for its own ID.

For an existing row's `media` or `library_keys` change, Shortlist verifies current Plex libraries
when preparing and applying the plan; clients supply no internal library snapshot. A changed
snapshot makes the plan stale. Narrowing can remove collections from former libraries, so the
authorization covers both old and new library scope. Missing authority requires exact owner review;
the plan's `future_scope` still describes only the intended resulting row.

**People, libraries and seasons:** `shortlist_list_people`, `shortlist_list_libraries`,
`shortlist_get_person_row_settings`, `shortlist_list_seasons`, `shortlist_plan_people` and
`shortlist_plan_season`. Every current and future person is available to an approved connection;
row audiences and sharing rules still apply. The per-person settings read returns a permitted
person's stored and effective row overrides. Queued Explore theme metadata also needs configuration
read, history export and access to that theme's source libraries; otherwise it is omitted with a
redaction explanation. The read returns no watch records or picks. Row and library scope applies
to resolved effects, including indirect effects. `shortlist_plan_people` accepts at most 25 sparse
`row_overrides` entries for that one person: omit a field to preserve it, and use `null` for a
numeric override to restore row inheritance. Disabled people and per-person rows can be
preconfigured without enabling delivery. A legacy shared-row record returns `supported: false`,
its stored values and no effective values; it is read-only through MCP.

For an eligible person on an Explore row, a `row_overrides` entry can set `up_next_theme_id` to a
saved theme. This replaces the queued selection without promoting it, calling a provider or writing
to Plex. Omit the field to leave the selection unchanged; `null` does not clear it. The plan validates
the audience, theme and title conflicts, and rejects changed targets or sources before applying.
Selection needs the target person's row write permissions, theme write and access to the selected
theme's source libraries. It does not grant permission to read personal theme details. Paid
**Regenerate up next** remains an owner-browser action.

**Schedules and runs:** `shortlist_plan_schedule`, `shortlist_preview_row` and
`shortlist_plan_run`. A Manage Shortlist connection can preview or run selected saved rows and
people with the configured provider, search, poster and acquisition settings. Preview uses dry-run.
Each run requires explicit saved row and person IDs and an explicit dry-run choice. Standalone theme
generation is available in the owner browser, not through MCP.

**Requests and maintenance:** `shortlist_list_requests`, `shortlist_plan_requests` and
`shortlist_plan_maintenance`. Request actions are reject, restore, archive and send; each plan
has an explicit bounded set of candidate IDs. Maintenance supports named cache refresh and row
cleanup, plus owner-reviewed `people.sync` and `setup.complete`. `people.sync` queues only the
durable roster/privacy reconciliation job. `setup.complete` revalidates ownership, metadata,
libraries and roster before finishing the wizard. Cleanup removes delivered Shortlist-owned
collections and requires exact review. Uninstall remains in the owner browser.

**Review and progress:** `shortlist_get_change`, `shortlist_apply_change`,
`shortlist_get_operation`, `shortlist_cancel_operation`, `shortlist_list_runs`,
`shortlist_get_run_report` and `shortlist_get_activity`. Results are filtered by current authority.
An operation receipt can show progress without retaining access to data removed from the grant.
For an assistant's own run, `selected_row_ids` identifies the requested builds;
`affected_row_ids` identifies the broader authorization footprint for privacy, retirement and
shelf-ordering work. The legacy `row_ids` field retains that footprint. Older records without a
saved intent return `selected_row_ids: null`.

Shared `pick_count` counts selected candidates in the saved result, not confirmed Plex deliveries.
The parent report's `dry_run` flag distinguishes a preview, which writes nothing to Plex. An `ok`
shared result with zero candidates reports `reason_code: no_picks` and generic guidance to review
libraries, filters, the active season and audience. Confirm actual delivery separately.

Shared outcomes expose a closed `reason_code` and safe guidance for recognized missing common
history, insufficient active audience, or unsupported shared templates. Unknown reasons and errors
require owner inspection in Runs; raw error text, viewing history and audience counts are omitted.
An own-run `privacy_warnings` entry means Plex could not apply all requested hiding filters. Inspect
Runs and Privacy before relying on personal visibility; a completed operation alone is not proof
that every account can be isolated by Plex.


## Plan, approve, apply

1. A plan resolves the requested change against the current database, determines its permissions
   and effects, and records dependency hashes. Preparation makes no provider or acquisition call.
2. The plan supplies a change ID and identifies any missing authority or exact owner review.
   Sensitive changes open a browser review page showing the concrete plan and external effects.
   A connection allowed to propose but not read the resolved content receives only `review_preview`:
   closed change/action/effect categories, explicit requested usage limits and the review flag.
   It never contains targets, names, setting values, prompts, destinations, effect payloads or
   global counts.
3. The owner approves that exact content and grant revision. A chat reply does not replace this
   approval. Approval authorizes that one apply; it does not add read capability or disclose the
   full plan to an inspecting connection. A changed dependency or plan requires a new review.
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

The owner selects one app-wide role for a new named connection. **View only** can read settings,
rows, people and activity, including permitted history projections. It cannot prepare or apply
changes, run or preview rows, cancel jobs, set up connections or dispatch requests. An earlier exact
owner approval does not let a View only connection perform a later mutation. **Manage Shortlist**
adds supported configuration and operations, including normal runs of selected saved rows. It
cannot read secrets or administer grants. Owner-only credential and URL setup remains in the
browser. Some maintenance and recurring-spend configuration still needs exact owner review.

Role capabilities are an upper bound. OAuth access is further limited to scopes issued to that
token; selecting Manage later does not widen an existing token. Reconnect to request more scopes.
Shortlist rechecks the current owner, grant, role, token ceiling, resource scope and saved effects
when preparing and applying work, before queued work starts, and before each new external effect.
Revocation, expiry, role downgrade or a changed destination stops new authorized work. Assistant
tools cannot read secrets, issue credentials, expand their own grant or invoke arbitrary HTTP, SQL,
shell commands or job types.

Manage Shortlist authorizes configured runs without a grant lifetime provider-call allowance.
These normal runs can recur and may incur charges from the owner's saved providers, search and
image services. Shortlist has no app-wide monetary cap for them. The engine's configured request
bounds, timeouts, cancellation and acquisition cap still apply. The run contract pins selected
rows and people, settings and service destinations; provider and acquisition calls keep durable
started/outcome records. An uncertain outcome stops new effects and is not automatically retried.

Existing connections keep their actual permissions and lifetime allowance until the owner
explicitly selects View only or Manage Shortlist. Their existing run rules and provider-call
reservations remain in force in the meantime. Selecting a role preserves historical counters and
usage; it does not turn old quota into new run authority. Standalone MCP theme generation is no
longer offered, including to older connections.

### Configured runs

`shortlist_plan_run` and `shortlist_preview_row` accept saved selectors for a Manage Shortlist
connection. Discover row and person IDs first. A run plan looks like this:

```json
{
  "request": {
    "row_ids": [1],
    "person_ids": [2],
    "dry_run": false,
    "include_shared": false
  }
}
```

The caller cannot supply a prompt, model, provider, URL, output-token cap, image allowance, search
setting or paid budget override. The plan resolves the saved provider models, destinations and
settings, then the worker checks that same contract before each external effect. A preview can
spend on AI selection or search; it skips image rendering and acquisition sends. Google native
Search can perform multiple billable internal searches inside one provider request, with no
Shortlist monetary cap. Inspect the operation and run report before starting another run after an
uncertain result.

Older unconverted connections continue to use their existing bounded run inputs and lifetime
provider-call allowance. Their `max_provider_calls` reservation is atomic and is not refunded when
unused; it counts requests, not currency. That allowance does not cap normal scheduled row
automation. The new configured-run selectors do not inherit or reset it.

Recurring configuration deserves separate review. An enabled themed row can be topped up by
Shortlist's provider even when its saved theme was authored externally. `ai_paused: true` prevents
that top-up. Schedule, provider, prompt, poster and acquisition changes can alter recurring
effects. Disabling or revoking an assistant grant does not disable schedules it already created.

## External outcomes and recovery

Provider calls and acquisition sends record a durable checkpoint before dispatch. A crash or
timeout after that checkpoint can leave the outcome unknown. Shortlist preserves the started
outcome record and reports uncertainty rather than automatically spending or sending again. Older
bounded runs also retain the provider-call reservation.

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
- `GET /assistant/grants` lists named grants, effective access and historical provider-call usage;
  `POST /assistant/grants` creates one with `access_role: "view"` or `"manage"`.
  `PATCH /assistant/grants/{grant_id}` changes the role under the grant revision guard. Older
  granular grants retain their literal constraints until that explicit role selection.
- `GET /assistant/destinations` remains available for older granular grants. Manage grants
  resolve only currently configured, supported, credential-free service endpoints in the current
  authorization transaction. URL and credential setup remain owner-only; a prepared plan pins its
  destination and fails if the configured endpoint changes before dispatch.
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
