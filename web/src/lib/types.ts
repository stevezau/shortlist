/**
 * The seam between the generated OpenAPI types and the UI.
 *
 * `.claude/rules/frontend.md`: request/response types are GENERATED, never hand-written. The API now
 * declares a Pydantic model on 65 of its routes (130 schemas), so that rule finally holds for nearly
 * everything here — "Generated" below derives every request body AND every declared response from
 * `api-schema.d.ts` (built by `pnpm -C web gen:api` from `openapi.snapshot.json`, itself guarded
 * against drift by `tests/unit/test_openapi_snapshot.py`). That includes the SSE event payloads
 * (`RunFinishedEvent`, `RunProgressEvent`, `UninstallProgressEvent`, `SyncProgressEvent`,
 * `SyncFinishedEvent`) and the four `/api/setup` shapes that used to be the handlers' bare `dict`
 * returns (`ProbeResult`, `LibrarySection`, `ProbeCheck`, `PlexServer`) — both families now have
 * real response models, even though `text/event-stream` itself has no OpenAPI content type.
 *
 * What is left hand-written is down to one kind, under "Hand-written":
 *
 *   - Engine-owned JSON blobs the server deliberately declares as an open map (`Run.stats`, a run
 *     user's `diff`/`breakdown`, the whole trace). Their keys vary by DATA, not by branch, they have
 *     changed repeatedly, and they are thinner on legacy rows — so a model would either invent the
 *     absent keys or go stale, and the UI's own account of them is the more useful one.
 *
 *   `RunUserStageEvent` sits in the same block but isn't really an exception: it's `RunLogEntry`
 *   (already generated) under its SSE-facing name, not a second declaration of it.
 *
 * Each carries a one-line reason. Treat every one as an ASSUMPTION about the API, not a guarantee
 * from it; a Pydantic response model on the Python side is what retires it.
 *
 * Third group: "Frontend-only" — unions the UI narrows on top of a server field typed bare `str`,
 * and the aliases components import. A value outside one of these is a UI bug, not an API change,
 * and the real fix for each is a `Literal[...]` on the Python side.
 */

import type { components, paths } from "./api-schema";

type Schemas = components["schemas"];

// ---------------------------------------------------------------------------
// Generated — derived from api-schema.d.ts. Do not hand-edit these shapes;
// change the Pydantic model and re-run `pnpm -C web gen:api`.
// ---------------------------------------------------------------------------

/*
 * Two conventions run through the derivations below.
 *
 * `Partial<>` / `Required<>`, on a REQUEST body. `gen:api` runs openapi-typescript with its default
 * `--default-non-nullable`, which marks any field carrying a default as REQUIRED — right for a
 * response (the server always sends it), wrong for a request body (the client never has to). Where
 * that matters the wrapper restores what the schema's own `required` list says.
 *
 * `{ narrowed } & Schemas[…]`, narrow side FIRST, on a RESPONSE. Every response model is
 * `extra="allow"`, so its generated type ends in `& { [key: string]: unknown }` — which makes
 * `keyof` collapse to `string`, and therefore makes `Omit<>` return the index signature ALONE,
 * silently dropping every declared field. Intersecting is the only override that survives it. The
 * order matters for array fields: on `A[] & B[]`, `.map()` resolves to the FIRST constituent's
 * signature, so the narrowed type has to lead or the callback parameter comes back as `unknown`.
 */

// --- Settings / connections ---

/** GET /api/settings — the settings store is genuinely free-form on the server, so this is the
 *  endpoint's declared shape (`SettingsOut`, an open map) rather than a stand-in for one. */
export type Settings =
  paths["/api/settings"]["get"]["responses"][200]["content"]["application/json"];

/** POST /api/settings/test/{service} response. */
export type ConnectionTestResult = Schemas["ConnectionTestOut"];

/** GET /api/settings/arr/{service}/options — dropdown data for a connected Sonarr/Radarr. */
export type ArrOptions = Schemas["ArrOptionsOut"];

// --- Rows / collections ---

/** Where a row sits in a library's Recommended shelf, keyed by library (section) key. A `top` entry
 *  means the very top; otherwise `anchor` places it after/before that collection.
 *
 *  The EDITOR's working shape: `Partial<>` because the control builds an entry a field at a time and
 *  every field is optional server-side (`HubAnchorIn` has no `required` list). What a saved row
 *  carries back is {@link Collection.hub_anchor}, whose values are the fully-defaulted
 *  `HubAnchorOut`. */
export type HubAnchorMap = Record<string, Partial<Schemas["HubAnchorIn"]>>;

/** Poster fields sent on save (no image bytes — those go through the upload endpoint). */
export type PosterInput = Schemas["PosterIn"];

/** A row's custom collection poster as the API returns it — never the image bytes. */
export type Poster = Schemas["PosterOut"];

/**
 * Which of Plex's two surfaces one side of a row claims: the Home screen, the library's Recommended
 * shelf, both, or neither. Held once per audience (`placement` / `placement_friends`) because every
 * person gets their own Plex collection, so each flag is set per collection.
 */
export type Placement = NonNullable<Schemas["CollectionIn"]["placement"]>;

/** A poster mode. "" = Plex default, "upload" = your image, "text" = built-in renderer, "ai" = image
 *  model. "generate" is the legacy name for "ai", still returned for rows saved before the split. */
export type PosterMode = NonNullable<Schemas["PosterIn"]["mode"]>;

/**
 * The row editor's working state. `Required<>` because `blankInput()`/`toInput()` fill every field,
 * so the editor never has to reason about `undefined` — the API itself only requires `name`, which
 * is what {@link CollectionBody} expresses.
 *
 * The closed-set fields (`build`, `audience`, `media`, `placement`, `placement_friends`) carry their
 * unions FROM the schema — the server advertises each set's members, so these are a contract rather
 * than an assumption the UI restates.
 */
export type CollectionInput = Omit<
  Required<Schemas["CollectionIn"]>,
  "hub_anchor"
> & {
  hub_anchor: HubAnchorMap;
};

/** POST /api/collections and PATCH /api/collections/{id} body. Only `name` is required; every other
 *  field falls back to the row's stored value (PATCH) or the server default (POST). */
export type CollectionBody = Partial<CollectionInput> & { name: string };

/** A curated-row definition (GET/POST/PATCH /api/collections).
 *
 *  `hub_anchor` needs no override: its dynamic keys are Plex section keys, which the schema already
 *  expresses as an index signature, and its VALUES are modelled (`HubAnchorOut`). */
export type Collection = Schemas["CollectionOut"];
export type RowEffectiveness = Schemas["RowEffectivenessOut"];

/** A Plex library on the server (GET /api/system/libraries). */
export type PlexLibrary = Schemas["LibraryOut"];

/** One shortlist-labelled collection found on Plex by the cleanup audit. */
export type OwnedCollection = Schemas["OwnedCollectionOut"];

/** GET /api/system/owned-collections — the cleanup audit result. */
export type OwnedCollectionsAudit = Schemas["OwnedCollectionsOut"];

/** POST /api/collections/{id}/cleanup — remove a row's collections from Plex. */
export type CleanupResult = Schemas["CleanupOut"];

/** Whether the configured AI provider can generate images (GET /api/system/image-provider). */
export type ImageProviderStatus = Schemas["ImageProviderOut"];

// --- Users ---

/**
 * PATCH /api/users/{id} — per-user overrides.
 *
 * Bare ints in `blocked_seeds` are the original storage shape and stay valid for ever; dict entries
 * carry the title so the UI can show a name instead of "tmdb 346648". Read it with
 * {@link blockedSeeds}.
 */
export type UserPrefs = Schemas["UserPrefs"];

export type UserPatch = Omit<Schemas["UserPatch"], "prefs"> & {
  prefs?: UserPrefs | null;
};

/** One blocked seed: a title that must never shape this person's recommendations. Both the list and
 *  the block/unblock endpoints return it normalised to a record, whichever way prefs are stored. */
export type BlockedSeed = Schemas["BlockedSeedOut"];

/** GET /api/users/search/titles — TMDB's own best guess for a title search. NOT a {@link BlockedSeed}:
 *  TMDB can answer without an id, so `tmdb_id` is nullable and such a match cannot be blocked. */
export type TitleMatch = Schemas["TitleMatchOut"];

/** Owner / shared / managed — `UserOut.user_type`, a named schema component. */
export type UserType = Schemas["UserType"];

/** GET /api/users — one row per Plex user Shortlist knows about.
 *
 *  `prefs` is narrowed to what a client may WRITE; what is STORED is an open map, so read
 *  `blocked_seeds` through {@link blockedSeeds} rather than trusting its declared shape. */
export type User = {
  prefs: UserPrefs;
} & Schemas["UserOut"];

/** GET /api/users/{id}/rows — one row this user gets, with their override and latest picks. */
export type UserRow = Schemas["UserRowOut"];

/** PUT /api/users/{id}/rows/{collection_id} body. */
export type RowOverridePatch = Schemas["RowOverridePatch"];

/** GET /api/users/{id}/history — one recent watch. */
export type WatchItem = Schemas["WatchItemOut"];

/** One title from the cached watched set (GET /api/users/{id}/watched) — the set recommendations
 *  are filtered against, which is a DIFFERENT source from `WatchItem`'s live Plex read. */
export type WatchedTitle = Schemas["WatchedTitleOut"];
export type WatchedPage = Schemas["WatchedPageOut"];

/** A Plex Home user the owner could move their watching to (GET /api/watching-account/candidates). */
export type HomeUserCandidate = Schemas["HomeUserOut"];
export type TransferResult = Schemas["TransferOut"];

/** What the watch-history panel is filtering by. "" = every type. */
export type WatchedFilters = {
  q: string;
  mediaType: "" | "movie" | "show";
  limit: number;
};

/** `prefs.blocked_seeds` as records, whatever shape it is stored in. Mirrors the server's
 *  `blocked_entries` — an old install's bare-int list is valid data and keeps working. */
export function blockedSeeds(prefs: UserPrefs | undefined): BlockedSeed[] {
  return (prefs?.blocked_seeds ?? []).map((entry) =>
    typeof entry === "number"
      ? { tmdb_id: entry, title: "", media_type: "", year: null }
      : // `year` is optional on the wire but always present here, so a caller never has to tell
        // "no year recorded" apart from "field absent" — they mean the same thing.
        { ...entry, year: entry.year ?? null },
  );
}

// --- Runs ---

/**
 * One delivered recommendation.
 *
 * Two server models feed this one renderer: the run detail sends `PickOut`, while the USER pages
 * send `UserPickOut`, which adds four placement fields. They are optional here so `PickList` and the
 * run panel can render either. NB: this name shadows TypeScript's built-in `Pick<T, K>` inside this
 * module — use an explicit object literal here rather than the utility type.
 */
export type Pick = Schemas["PickOut"] & {
  /** Present on breakdown picks (delivery stamps it); absent on the flat `picks` list. Used for the
   *  look-it-up links, which are omitted rather than broken when it is missing. */
  tmdb_id?: number;
  /** Which row this pick belongs to (Collection slug). */
  collection_slug?: Schemas["UserPickOut"]["collection_slug"];
  library?: Schemas["UserPickOut"]["library"];
  media_type?: Schemas["UserPickOut"]["media_type"];
  section_key?: Schemas["UserPickOut"]["section_key"];
};

/** POST /api/runs body — every field is optional (`RunRequest` has no `required` list). */
export type RunRequest = Partial<Schemas["RunRequest"]>;

/** How a run started — schedule fired it, an operator clicked Run, or the wizard's first-run step. */
export type RunTrigger = Schemas["RunSummaryOut"]["trigger"];

/** GET /api/runs — one row per pipeline run. */
export type Run = {
  stats: RunStats;
} & Schemas["RunSummaryOut"];

/** Per-user slice of GET /api/runs/{id}. `duration_ms` is null while a user is still pending. */
export type RunUserResult = {
  diff: RunDiff;
  breakdown: RunLibraryBreakdown[];
} & Schemas["RunUserOut"];

/** GET /api/runs/{id} — the run plus its per-user results. */
export type RunDetail = {
  stats: RunStats;
  users: RunUserResult[];
} & Schemas["RunDetailOut"];

/** POST /api/runs — the queued run's id. */
export type RunCreated = Schemas["RunCreatedOut"];

/** GET /api/runs/summary — totals for the Runs page header. */
export type RunsSummary = Schemas["RunsSummaryOut"];

/** GET /api/users/{id}/runs — one of this user's recent run results. */
export type UserRunSummary = { diff: RunDiff } & Schemas["UserRunOut"];

/** GET /api/users/{id}/runs/summary — how many runs included this person, of how many exist. */
export type UserRunsCount = Schemas["UserRunsSummaryOut"];

/** One line of a run's activity log (GET /api/runs/{id}/log + the SSE stage stream).
 *
 *  `counts` is an engine-owned tally the server declares as an open map — see {@link RunStats} for
 *  why those stay described here. `seq` is the dedup key: several lines land in the same
 *  millisecond, so a timestamp alone collapsed "1/5" and "2/5" into one entry. */
export type RunLogEntry = {
  counts?: Record<string, number | string>;
} & Schemas["RunLogLineOut"];

/** What the request subsystem did with a wanted-but-missing title (Sonarr/Radarr). Overlaid onto a
 *  "not in your libraries" fate so a drop reads "→ requested from Radarr" instead of a dead end.
 *  pending = queued for the owner's approval; sent = asked of Sonarr/Radarr; rejected = dismissed. */
export type TraceRequestOutcome = Schemas["TraceRequestOut"];

/** GET /api/runs/{id}/users/{uid}/trace response. */
export type RunUserTraceResponse = {
  trace: RunUserTrace;
  breakdown: RunLibraryBreakdown[];
  /** Keyed "<tmdb_id>:<media_type>" — the trace overlays it onto "not in your libraries" drops. */
  requests: Record<string, TraceRequestOutcome>;
} & Schemas["RunUserTraceOut"];

// --- Requests inbox (Sonarr/Radarr) ---

/** GET /api/requests — one wanted-but-missing title in the Sonarr/Radarr approval inbox. */
export type RequestCandidate = Schemas["RequestCandidateOut"];

/** One reason a missing title is in the inbox: a person, the row that wanted it, and what suggested it. */
export type RequestWhy = Schemas["RequestWhyOut"];

/** One title's result from POST /api/requests/send. */
export type RequestSendOutcome = Schemas["SendOutcomeOut"];

/** POST /api/requests/send response. */
export type RequestSendResult = Schemas["SendOut"];

// --- Setup wizard / auth ---

/** GET /api/setup/servers — a server plex.tv says this account can reach, with every advertised
 *  address already tried from where Shortlist actually runs — only the owner's network knows which
 *  one works. `machine_id` is null only if plex.tv ever omits `clientIdentifier` on the resource. */
export type PlexServer = Schemas["PlexServerOut"];

/** POST /api/setup/probe body. */
export type ProbeRequest = Schemas["ProbeRequest"];

/** One line of the wizard's step-1 checklist (`ProbeResult.checks`): did it pass, and what to say
 *  about it. */
export type ProbeCheck = Schemas["ProbeCheckOut"];

/** One movie/show library the probe found (`ProbeResult.libraries`). `key` is a genuine number —
 *  plexapi casts a Plex section's key to an int, and the probe passes it through unstringified,
 *  unlike {@link PlexLibrary} (`/api/system/libraries`), which calls `str()` on its own. */
export type LibrarySection = Schemas["LibrarySectionOut"];

/** POST /api/setup/probe response. */
export type ProbeResult = Schemas["ProbeResultOut"];

/** POST /api/setup/link body. */
export type LinkRequest = Schemas["LinkRequest"];

/** GET/PUT /api/setup/state — wizard progress, persisted per step change. */
export type SetupState = Schemas["WizardState"];

/** POST /api/auth/pin. */
export type PinCreated = Schemas["PinOut"];

/**
 * GET /api/auth/pin/{id}. The Plex token is deliberately NOT here: the backend holds it
 * server-side for the setup session, so an XSS anywhere in this UI cannot steal it.
 */
export type PinStatus = Schemas["PinStatusOut"];

/** GET /api/auth/session. `login_required` answers "does this instance have anything worth
 *  protecting yet" — if not, the wizard opens without a login and connecting Plex claims it. */
export type Session = Schemas["SessionOut"];

/** Owner API-token status. The token is revealable (stored encrypted at rest), so the owner-gated
 *  endpoint returns it in plaintext for the owner to unhide/copy — like Sonarr/Radarr's key. */
export type ApiTokenStatus = Schemas["ApiTokenStatusOut"];

/** The response to generating a token. */
export type ApiTokenCreated = Schemas["ApiTokenCreatedOut"];

// --- Dashboard / report ---

/** One alert in the bell menu (GET /api/notifications). */
export type AppNotification = Schemas["NotificationOut"];

/** `GET /api/notifications` — the firing alerts, plus every id already dismissed. The second list
 *  exists because the owner-shelf warning also renders inline on the Users page: both surfaces
 *  report one fact, so they dismiss as one. */
export type NotificationsPage = Schemas["NotificationsOut"];

/** Report windows, in days. "all" is lifetime. */
export type ReportWindow = Schemas["EffectivenessReportOut"]["window"];

/**
 * The dashboard effectiveness report — did delivered picks get watched?
 *
 * `overall.watched` counts picks WATCHED in the window, which is deliberately not the same set as
 * `overall.delivered`: a pick delivered last month and watched this week is a watch this week. The
 * one surviving ratio is `overall.landing`, measured over a MATURED cohort so its numerator and
 * denominator describe the same picks. `per_user` carries counts and no rate — at these sample sizes
 * a percentage is noise dressed as precision, and sorting by it put 1/31 above 3/103.
 */
export type EffectivenessReport = Schemas["EffectivenessReportOut"];

/** GET /api/report/deleted-rows — pick history left behind by a row that no longer exists. */
export type DeletedRowHistory = Schemas["DeletedRowOut"];

// --- System (logs, backups, jobs, schedule) ---

/** One parsed line from the rotating log file (GET /api/system/logs). A traceback is folded into
 *  the entry it belongs to, so `message` can span several lines. */
export type LogLine = Schemas["LogLineOut"];

export type LogPage = Schemas["LogsOut"];

/** Sync schedule info for the Tools page — when each sync last ran and next fires. */
export type SyncsInfo = Schemas["SyncsOut"];

export type Backup = Schemas["BackupOut"];

export type VersionInfo = Schemas["VersionOut"];

/** A job's lifecycle state, shared by `JobOut` and `JobRunOut` (`JobStatus` on the server — a
 *  Python-side `Literal` alias, not itself a named schema component, so this derives from whichever
 *  of the two fields it happens to read). */
export type JobStatus = Schemas["JobOut"]["status"];

/** One background maintenance job (GET /api/system/jobs). Runs are NOT jobs — they have their own
 *  page. `queued` after a failure means it will be retried. */
export type Job = Schemas["JobOut"];

/** GET /api/system/jobs/catalog — one entry per job kind Shortlist knows how to run.
 *
 *  The Jobs page is organised by JOB, not by chronology: "is the roster sync healthy?" can't be
 *  answered from a flat list of the last 25 rows with every kind mixed together. */
export type JobCatalogEntry = {
  last: Job | null;
} & Schemas["JobCatalogEntryOut"];

/** POST /api/system/jobs — the job as it stood after the inline drain. `fixed`/`orphans` are the
 *  sync.check preview lists (and empty for every other kind); `orphans` is kept apart from `fixed`
 *  because deleting a departed user's collection is the one action that cannot be undone. */
export type JobResult = Schemas["JobRunOut"];

/** GET /api/schedule — one recurring thing, discriminated by `type`: a job, or the group of rows
 *  sharing one cron (ONE trigger builds all of them, not one entry each). */
export type ScheduleEntry =
  Schemas["ScheduleJobOut"] | Schemas["ScheduleRowsOut"];

export type ScheduleResponse = Schemas["ScheduleOut"];

/** POST /api/system/uninstall response (also returned for dry-run previews). `rows_disabled` counts
 *  rows switched off so the next scheduled run can't rebuild what uninstall removed. */
export type UninstallResult = Schemas["UninstallOut"];

// --- Live events (GET /api/events, text/event-stream) ---
//
// The stream itself has no OpenAPI content type, but each event's payload is still a modelled
// schema (`RunUserStageEvent` aside — see the file header — it reuses RunLogEntry/RunLogLineOut).

/** Which Tools-page sync a `sync.*` event belongs to. */
export type SyncKind = Schemas["SyncProgressEvent"]["kind"];

/** Event `run.finished` — a run reached a terminal state. `aborted` is a cancel that still completed
 *  its privacy merge and promotion, so it is an outcome, not a failure — a consumer that folds it
 *  into "failed" (as `status !== "ok"`) is telling the owner their cancelled run broke. */
export type RunFinishedEvent = Schemas["RunFinishedEvent"];

/** Event `run.progress` — a run entered a non-terminal state. `cancelling` publishes the moment
 *  POST /api/runs/{id}/cancel is accepted; the run keeps going until the person it's on finishes. */
export type RunProgressEvent = Schemas["RunProgressEvent"];

/** One live step streamed while a real uninstall runs (SSE `uninstall.progress`). */
export type UninstallProgressEvent = Schemas["UninstallProgressEvent"];

/**
 * Live progress for a Tools-page sync (SSE `sync.progress`).
 *
 * The watched sync is one determinate loop (`done`/`total` users). The users sync has two phases:
 * an indeterminate `fetch` (the opaque plex.tv round-trip), then a determinate `save` bar.
 */
export type SyncProgressEvent = Schemas["SyncProgressEvent"];

/** A Tools-page sync finished (SSE `sync.finished`). */
export type SyncFinishedEvent = Schemas["SyncFinishedEvent"];

// ---------------------------------------------------------------------------
// Hand-written — the shapes the schema genuinely cannot describe.
//
// One kind only: engine-owned JSON blobs the server deliberately declares as an open map (see the
// file header). `RunUserStageEvent` below is not a second kind — it's an alias to a Generated type
// under its SSE-facing name. Everything else is generated above.
// ---------------------------------------------------------------------------

// --- Engine-owned blobs (declared `dict` server-side, on purpose) ---

/** `Run.stats`. An open map on the server: which keys exist varies by DATA (Exa counts only when
 *  web search ran) and by AGE (legacy runs predate the token totals), so a model would either
 *  invent the absent ones or go stale. */
export interface RunStats {
  users_ok: number;
  users_error: number;
  /** Built nothing, but nothing went wrong (no row was due for them). Absent on legacy runs. */
  users_skipped?: number;
  /** Titles added to rows across all users this run. */
  titles_added?: number;
  /** Titles rotated out of rows across all users this run. */
  titles_removed?: number;
  /** Titles requested from Sonarr/Radarr this run (0 when requests are off). */
  titles_requested?: number;
  /** Warnings about incomplete Arr config (e.g. missing quality profile or root folder). */
  requests_warnings?: string[];
  /** Total AI tokens this run cost (curate + the AI candidate sources). Absent on legacy runs. */
  llm_tokens?: number;
  /** That total split by where it went: { curate, llm_web, llm_library }. */
  llm_tokens_by_step?: Record<string, number>;
  /** Exa web searches run this run (billed per search, not per token — shown separately). */
  exa_searches?: number;
  /** Searches served from the shared 14-day cache instead of billed — "1 searched · N from cache". */
  exa_cache_hits?: number;
}

/**
 * A user's collection diff (`RunUserOut.diff` / `UserRunOut.diff`, an open map server-side).
 *
 * Every field is optional: the API returns `{}` for a user the run left alone (no picks produced, so
 * no row was touched), not a diff of three empty lists.
 */
export interface RunDiff {
  added?: string[];
  removed?: string[];
  kept?: string[];
  /** Rows deleted because Plex could not hide them (wrong type for their library). */
  deleted?: string[];
}

/** One (row, library) slice of a user's run result: what changed in that library + its own picks.
 *  An element of `RunUserOut.breakdown`, which the server declares as a list of open maps — the
 *  engine owns the blob and it is empty on legacy runs. */
export interface RunLibraryBreakdown {
  row_slug: string;
  row_title: string;
  library_key: string;
  library_title: string;
  added: string[];
  removed: string[];
  kept: string[];
  deleted: string[];
  created: boolean;
  picks: Pick[];
  /** AI tokens the curate call for this (row, library) cost. Absent on legacy runs. */
  llm_tokens?: number;
}

/** One recent watch shown in a trace. */
export interface TraceWatch {
  title: string;
  media: string;
  /** Display name of the Plex library it lives in ("" when unknown — fall back to a media label). */
  library: string;
  year: number | null;
  watched_at: string | null;
  /** What they rated it in Plex, 0..10, or null/absent (a run before ratings were read). */
  rating?: number | null;
  /** Whether that rating is why it isn't a seed — narrower than `rating`, which is just shown. */
  rating_blocked?: boolean;
}

/** The rating policy one run used, as recorded at run time — not what Settings says today. */
export interface TraceRatings {
  /** "Respect Plex ratings" was on for this run. */
  enabled: boolean;
  /** Rating (0..10) at or below which a title stopped seeding; null when the feature was off. */
  threshold: number | null;
  /** False when the account's ratings look tool-written, so every one of them was ignored. */
  trusted: boolean;
  /** How many titles the rating actually dropped, across their whole history. */
  blocked: number;
  /** How many of their watches carry any rating at all — tells "on but never rated" from "on but fine". */
  rated: number;
  /** ...of which look typed by a person. Lower than `rated` on an account a tool has PARTLY written:
   *  the account stays trusted while the fractional values are skipped one by one, so any "none of
   *  their N are low" sentence has to count off this, not `rated`, or it speaks for uncounted values. */
  rated_human: number;
}

/** A seed derived from history — a history title used to find candidates. */
export interface TraceSeed {
  title: string;
  media: string;
  /** Display name of the Plex library it lives in ("" when unknown — fall back to a media label). */
  library: string;
  tmdb_id: number;
  weight: number;
  /** The two ingredients behind `weight` — so the influence bar reads "watched 4×, 3 days ago". */
  watch_count?: number;
  recency_days?: number;
}

/** One title a source returned for a seed, tagged with its fate through selection. */
export interface TraceReturn {
  tmdb_id: number;
  title: string;
  /** Kept/dropped verdict (absent on legacy runs recorded before disposition tracking). */
  fate?: TraceFate;
}

/** One seed's query against a source: what it searched for and a sample of what came back. */
export interface TraceSeedQuery {
  seed: string;
  media: string;
  returned: TraceReturn[];
  /** Total returned before the `returned` sample was capped — so the UI can say "+N more". */
  total: number;
}

/** One candidate source's contribution in a gather. */
export interface TraceSource {
  source: string;
  status: "ok" | "failed";
  contributed: number;
  detail: string;
  /** Per-seed query sample (seeded TMDB/Trakt sources only; empty for discover/llm_web). */
  queries?: TraceSeedQuery[];
  /** Fate tally across this source's returned sample: {kept, already_watched, ...} counts. */
  disposition?: Record<string, number>;
}

/** One Exa search: the query sent for a seed and the titles it returned. */
export interface TraceWebSearch {
  seed: string;
  query: string;
  cached: boolean;
  returned: string[];
}

/** One title the AI proposed from the web search, resolved to a real TMDB id, tagged with whether it
 *  made the library's shortlist (kept) or the reason it fell out — the same fate a TMDB/Trakt return
 *  carries. Hallucinations (no TMDB match) never reach here; they stay in `unresolved`. */
export interface TraceWebProposal {
  title: string;
  tmdb_id: number;
  media: string;
  fate?: TraceFate;
}

/** The web-search (llm_web) detail of a gather: what was searched and what the LLM proposed. */
export interface TraceWeb {
  mode: string;
  searches?: TraceWebSearch[];
  rag_system?: string;
  rag_user?: string;
  proposed?: string[];
  native_proposed?: string[];
  resolved?: string[];
  unresolved?: string[];
  /** Resolved proposals with their fate through selection — kept into the row, or why they dropped. */
  proposals?: TraceWebProposal[];
}

/** One candidate pool a user's rows gathered (usually one, shared across rows). */
export interface TraceGather {
  pool: string;
  sources?: TraceSource[];
  discover_genres?: Record<string, string[]>;
  web?: TraceWeb;
}

/** The full pipeline trace for one user in one run (`RunUserTraceOut.trace`, an open map).
 *
 *  The engine owns this blob outright: stages have been added several times and every field is
 *  absent on runs recorded before it existed, which is exactly why the server does not model it. */
export interface RunUserTrace {
  history?: {
    total: number;
    recent: TraceWatch[];
    watched_movies: number;
    watched_shows: number;
    /** True distinct-title watched totals per library NAME, split by media type — exact per library
     *  even when several libraries share a media type. Absent on runs recorded before this was added. */
    watched_by_library?: Record<string, { movie: number; show: number }>;
    /** What Plex ratings did to this person's seeds on THIS run — recorded because the outcome can't
     *  be read back from the watch list: no dropped titles means the setting was off, or nothing was
     *  rated low, or the account's ratings were tool-written and disbelieved. Absent on older runs. */
    ratings?: TraceRatings;
  };
  seeds?: TraceSeed[];
  gathers?: TraceGather[];
  /** What happened to each (row, library) tonight and the settings that decided it. Absent on runs
   *  recorded before this was added, which the UI renders as nothing rather than as "rebuilt". */
  selection?: TraceSelection[];
}

/** One row+library's outcome for a run: which branch the engine took, and the settings behind it. */
export interface TraceSelection {
  row: string;
  library: string;
  /** `rebuilt` (built fresh) · `carried_forward` (redelivered untouched — not its refresh night) ·
   *  `refreshed` (kept the strongest two-thirds, swapped the rest) · `settings_changed` (rebuilt
   *  early because a setting that decides contents was edited) · `cold_start`. */
  decision:
    | "rebuilt"
    | "carried_forward"
    | "refreshed"
    | "settings_changed"
    | "cold_start";
  size: number;
  delivered: number;
  candidates?: number;
  cut_cap?: number;
  carried?: number;
  new?: number;
  freshness?: number;
  refresh_night?: boolean;
  rebuild_every_days?: number | null;
  recency?: number;
  watched_pct?: number;
  pick_order?: string;
  rewatch?: boolean;
  unstarted_only?: boolean;
}

// --- SSE payloads (GET /api/events) ---

/** Event `run.user.stage` — the SAME object the activity log carries. The buffer's sink stamps `seq`
 *  on the entry in place before the bus publishes it, so this is that type rather than a second,
 *  drifting declaration of it. */
export type RunUserStageEvent = RunLogEntry;

// ---------------------------------------------------------------------------
// Frontend-only — unions and aliases the UI invents.
//
// The server types every one of these fields as a bare `str`, so the schema cannot narrow them:
// these are the UI's own vocabulary for the values it knows the engine produces, and a value
// outside a union here is a UI bug, not an API change. Each could be retired by a `Literal[...]`
// on the Python side.
// ---------------------------------------------------------------------------

/** What happened to a candidate a source returned: kept into the pool, or dropped and why. */
export type TraceFate =
  | "kept"
  | "already_watched"
  | "not_in_your_libraries"
  | "excluded_genre"
  | "lost_ranking_cutoff"
  | "not_returned";

/** The services POST /api/settings/test/{service} accepts (a path parameter typed `str`). */
export type TestableService =
  | "plex"
  | "tautulli"
  | "tmdb"
  | "llm"
  | "radarr"
  | "sonarr"
  | "mdblist"
  | "trakt"
  | "exa";

/** Alias kept short for the components that render one line of this. */
export type UserRun = UserRunSummary;

// --- Support Mode -------------------------------------------------------------------------
//
// Every tool response carries `text`: the fixed-width block the "Copy for support" button puts on
// the clipboard. It is rendered by the SERVER, not assembled here, so the format a maintainer reads
// is decided and unit-tested in one place and cannot drift between the API and the UI.

export interface SupportStatus {
  enabled: boolean;
  expires_at: string | null;
  seconds_remaining: number;
}

export interface SupportHealthCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface SupportHealth {
  checks: SupportHealthCheck[];
  text: string;
}

export interface SupportTitleDelivery {
  row: string;
  rank: number;
  library: string;
}

export interface SupportTitleRow {
  user: string;
  /** One row per person PER TITLE — a substring query can match a whole franchise, so the verdict is
   *  meaningless without knowing which title it is about. */
  title: string;
  /** Null when Plex never matched the title to a TMDB id — reported as absent, not as id 0. */
  tmdb_id: number | null;
  watched_record: boolean;
  media_type: string;
  viewed_leaf_count: number | null;
  leaf_count: number | null;
  counts_as_watched: boolean;
  cap_pct: number;
  cap_source: string;
  delivered: SupportTitleDelivery[];
  /** Delivered to this person, not counted as watched, and their cap is 0% — the reported bug. */
  problem: boolean;
}

export interface SupportTitleLookup {
  query: string;
  rows: SupportTitleRow[];
  /** Usernames with at least one problem row. */
  flagged: string[];
  /** The same, as "user (title)" — what the copy block names. */
  flagged_detail: string[];
  /** True when the search hit its row cap, so the result is not the whole picture. */
  capped: boolean;
  text: string;
}

export interface SupportPersonLibrary {
  section_key: string;
  /** The library's display name from Plex; "" when Plex could not be listed. */
  library: string;
  titles_known: number;
  last_full_at: string | null;
  last_incremental_at: string | null;
  /** False means the library has NEVER been read for this person — the signature of a refused
   *  token, which looks identical to "watches nothing" everywhere else in the app. */
  ever_read: boolean;
}

export interface SupportPerson {
  slug: string;
  display_name: string;
  user_type: string;
  enabled: boolean;
  cold_start: boolean;
  restricted: boolean;
  restriction_profile: string;
  watched_movies: number;
  watched_shows: number;
  libraries: SupportPersonLibrary[];
  never_read: string[];
  muted_rows: number[];
  text: string;
}

export interface SupportLibrary {
  key: string;
  title: string;
  type: string;
  items: number;
}

export interface SupportLibraries {
  libraries: SupportLibrary[];
  error: string | null;
  text: string;
}

export interface SupportRowSetting {
  slug: string;
  name: string;
  enabled: boolean;
  media: string;
  size: number;
  watched_pct: number;
  watched_pct_source: string;
  freshness: number;
  freshness_source: string;
  rewatch: boolean;
  unstarted_only: boolean;
}

export interface SupportRows {
  rows: SupportRowSetting[];
  global_watched_pct: number;
  text: string;
}

/**
 * Any support check's response.
 *
 * Every check returns its own fields PLUS `text` — the fixed-width block the copy button puts on
 * the clipboard, rendered server-side. The page reads them all through one generic panel and only
 * ever touches `text` and a handful of per-check flags (`flagged`, `never_read`, `problems`, …),
 * which `verdictFor()` looks up by id. Declaring nineteen near-identical interfaces would add no
 * safety over that, since the panel is generic by design.
 */
export interface SupportResult {
  text: string;
  [key: string]: unknown;
}

/** Type-ahead data for the "Have an issue?" checks that take a person or a title. */
export interface SupportSuggestions {
  people: { slug: string; display_name: string; enabled: boolean }[];
  titles: string[];
}
