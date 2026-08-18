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
  const pool = s.requests_pool ?? 0;
  if (pool === 0) return "nothing cleared the demand or year limits";
  const examined = s.requests_examined ?? 0;
  if (examined < pool)
    return `rated ${examined} of ${pool} wanted — none good enough`;
  return `rated all ${pool} wanted — none cleared the rating limit`;
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
