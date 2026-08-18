import {
  Clock,
  Download,
  Layers,
  Search,
  Shuffle,
  Sparkles,
  Users,
} from "lucide-react";

import { StatTile } from "@/components/stat-tile";
import { formatDuration, runElapsedMs } from "@/lib/format";
import { tokenStepBreakdown } from "@/lib/run-format";
import type { RunDetail } from "@/lib/types";

/** The finished-run stats as at-a-glance tiles (Dashboard style) rather than one dense text line. */

/** What "0 requested" was arrived at from.
 *
 * A bare zero reads identically whether nothing was wanted, the floors emptied the pool, or the
 * rating gate ran out of lookups before reaching anything good — and only the last is something the
 * owner can act on. It took reading the container log by hand to tell them apart (2026-08-18).
 */
function requestHint(s: RunDetail["stats"]): string {
  const requested = s.titles_requested ?? 0;
  if (requested > 0) return "to Sonarr / Radarr";
  // Queued FIRST, and before any talk of the floors: a run that put five titles in the inbox worked
  // exactly as configured, and "none good enough" would send the owner hunting a rating problem that
  // does not exist. Caught on a real run whose auto_min_demand had just been raised (2026-08-18).
  const queued = s.requests_queued;
  if (queued) return `${queued} waiting for you to approve in Requests`;
  // ABSENT is not zero. A run recorded before this key existed cannot tell us whether titles are
  // waiting, so claiming "none were good enough" asserts something the data does not support — and
  // sends the reader at the rating floor when the auto-send bar may be what held them. Same trap as
  // `wanted` in the notification builder.
  if (queued === undefined) return "see Requests for anything waiting";
  const wanted = s.requests_wanted;
  // A healthy run on a complete library also lands on pool === 0, so blaming the floors there would
  // report a fault where there is none. `wanted` is what tells the two apart.
  // "new" is doing real work: `wanted` is net of titles already sent or rejected, so a run whose
  // whole inbox was actioned also lands here. Titles ARE missing; they have all been dealt with.
  if (wanted === 0) return "nothing new was missing";
  const pool = s.requests_pool ?? 0;
  if (pool === 0) return "nothing cleared the demand or year limits";
  const examined = s.requests_examined ?? 0;
  // Neither number is a count of TITLES once a run has several rows: both are sums of per-row
  // checks, so a title two rows want is counted twice — while `requests_wanted` above is distinct.
  // Printing "of 3000 wanted" beside "1000 wanted" made the two disagree on the same card, so the
  // word does not appear here at all (release review 2026-08-18).
  if (examined < pool) return `rated ${examined} of ${pool} — none good enough`;
  return `rated all ${pool} — none cleared the rating limit`;
}

export function RunStatTiles({ run }: { run: RunDetail }) {
  const s = run.stats;
  const elapsed = runElapsedMs(run.began_at, run.finished_at);
  const failed = s.users_error ?? 0;
  // Skipped is neither a success nor a failure — a run where everyone was skipped used to read
  // "3 · all succeeded" above three rows badged "Skipped".
  const skipped = s.users_skipped ?? 0;
  const requested = s.titles_requested ?? 0;
  const tokens = s.llm_tokens ?? 0;
  const exa = s.exa_searches ?? 0;
  const exaCacheHits = s.exa_cache_hits ?? 0;
  // Shared rows belong to nobody, so the people counters cannot see them — and a run whose only
  // work was a shared row therefore reported "0 · 46 skipped, built nothing" directly above the row
  // that had just placed 40 picks. Rows built is the honest headline for what a run DID.
  const sharedRows = run.shared_rows ?? [];
  const sharedBuilt = sharedRows.filter((row) => row.status === "ok").length;
  const perPersonRows = new Set<string>();
  for (const user of run.users) {
    for (const [slug, decision] of Object.entries(user.rows_considered ?? {})) {
      if (decision === "due") perPersonRows.add(slug);
    }
    for (const entry of user.breakdown ?? []) {
      if (entry.row_slug) perPersonRows.add(entry.row_slug);
    }
  }
  const rowsBuilt = perPersonRows.size + sharedBuilt;
  const rowsHint = [
    perPersonRows.size > 0 ? `${perPersonRows.size} per-person` : "",
    sharedBuilt > 0 ? `${sharedBuilt} shared` : "",
  ]
    .filter(Boolean)
    .join(", ");
  // "web search 467,463 · final picks 52,625 tokens" — the trailing unit makes clear these are token
  // counts, not the (separate) Exa search count shown in its own tile below.
  const stepInline = tokenStepBreakdown(s.llm_tokens_by_step);
  // The two AI tiles are conditional, so the track count has to be too. Hard-coding six left a
  // no-AI run's four tiles filling two-thirds of the row with a third of it blank, which reads as
  // something that failed to load. Full class strings — Tailwind cannot see an interpolated one.
  const showTokens = tokens > 0;
  const showExa = exa > 0 || exaCacheHits > 0;
  const tiles = 5 + (showTokens ? 1 : 0) + (showExa ? 1 : 0);
  const columns =
    tiles === 7
      ? "sm:grid-cols-3 lg:grid-cols-7"
      : tiles === 6
        ? "sm:grid-cols-3 lg:grid-cols-6"
        : "sm:grid-cols-3 lg:grid-cols-5";
  return (
    <div className={`grid grid-cols-2 gap-3 ${columns}`}>
      <StatTile
        icon={Clock}
        label="Duration"
        value={elapsed != null ? formatDuration(elapsed) : "—"}
        hint="start → finish"
      />
      <StatTile
        icon={Layers}
        label="Rows built"
        value={rowsBuilt}
        hint={rowsHint || "nothing was due"}
        tone={rowsBuilt > 0 ? "success" : undefined}
      />
      <StatTile
        icon={Users}
        label="People"
        value={s.users_ok ?? 0}
        hint={
          failed > 0
            ? `${failed} failed${skipped > 0 ? `, ${skipped} skipped` : ""}`
            : skipped > 0
              ? // Only a WARNING when the run built nothing at all. A shared-row run skips every
                // person by design, and flagging that amber said "something went wrong" about the
                // normal outcome of the thing the operator asked for.
                sharedBuilt > 0
                ? `${skipped} skipped — no per-person row was due`
                : `${skipped} skipped, built nothing`
              : // Everyone can succeed while the RUN fails (a refused share filter belongs to no
                // person) — "all succeeded" under a "Failed" badge is how that looked before.
                run.status === "error"
                ? "built, but not promoted"
                : "all succeeded"
        }
        tone={
          failed > 0
            ? "destructive"
            : (skipped > 0 && sharedBuilt === 0) || run.status === "error"
              ? "warning"
              : skipped > 0
                ? undefined
                : "success"
        }
      />
      <StatTile
        icon={Shuffle}
        label="Titles changed"
        value={`+${s.titles_added ?? 0} / −${s.titles_removed ?? 0}`}
        hint="added / rotated out"
      />
      <StatTile
        icon={Download}
        label="Requested"
        value={requested}
        hint={
          s.requests_warnings?.length
            ? s.requests_warnings.join("; ")
            : requestHint(s)
        }
        tone={s.requests_warnings?.length ? "warning" : undefined}
      />
      {showTokens && (
        <StatTile
          icon={Sparkles}
          label="AI tokens"
          value={tokens.toLocaleString()}
          hint={stepInline ? `${stepInline} tokens` : "curate + AI sources"}
          title="Total AI tokens this run cost, split by what the AI did. Turn AI sources off in Settings → Finding titles to lower it."
        />
      )}
      {showExa && (
        <StatTile
          icon={Search}
          label="Web searches"
          value={exa}
          // A warm cache means most lookups never hit the backend — showing only the "1" that did
          // made a fully-cached run look like the source did nothing. The hint names what it served.
          hint={
            exaCacheHits > 0
              ? `searched · ${exaCacheHits.toLocaleString()} from cache`
              : "web lookups · one per recent watch"
          }
          // Vendor-neutral: the same counter serves Exa and a self-hosted SearXNG.
          title="External web-search requests this run actually made — a count, not tokens. Exa bills per request and SearXNG rate-limits per request, so it is tracked apart from token spend. Results are cached for two weeks and shared across everyone, so most lookups are served from cache and cost nothing."
        />
      )}
    </div>
  );
}
