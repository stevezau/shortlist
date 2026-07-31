import {
  Clock,
  Download,
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
export function RunStatTiles({ run }: { run: RunDetail }) {
  const s = run.stats;
  const elapsed = runElapsedMs(run.started_at, run.finished_at);
  const failed = s.users_error ?? 0;
  // Skipped is neither a success nor a failure — a run where everyone was skipped used to read
  // "3 · all succeeded" above three rows badged "Skipped".
  const skipped = s.users_skipped ?? 0;
  const requested = s.titles_requested ?? 0;
  const tokens = s.llm_tokens ?? 0;
  const exa = s.exa_searches ?? 0;
  const exaCacheHits = s.exa_cache_hits ?? 0;
  // "web search 467,463 · final picks 52,625 tokens" — the trailing unit makes clear these are token
  // counts, not the (separate) Exa search count shown in its own tile below.
  const stepInline = tokenStepBreakdown(s.llm_tokens_by_step);
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <StatTile
        icon={Clock}
        label="Duration"
        value={elapsed != null ? formatDuration(elapsed) : "—"}
        hint="start → finish"
      />
      <StatTile
        icon={Users}
        label="People"
        value={s.users_ok ?? 0}
        hint={
          failed > 0
            ? `${failed} failed${skipped > 0 ? `, ${skipped} skipped` : ""}`
            : skipped > 0
              ? `${skipped} skipped, built nothing`
              : // Everyone can succeed while the RUN fails (a refused share filter belongs to no
                // person) — "all succeeded" under a "Failed" badge is how that looked before.
                run.status === "error"
                ? "built, but not promoted"
                : "all succeeded"
        }
        tone={
          failed > 0
            ? "destructive"
            : skipped > 0 || run.status === "error"
              ? "warning"
              : "success"
        }
      />
      <StatTile
        icon={Shuffle}
        label="Titles changed"
        value={`+${s.titles_added ?? 0}/−${s.titles_removed ?? 0}`}
        hint="added / rotated out"
      />
      <StatTile
        icon={Download}
        label="Requested"
        value={requested}
        hint={
          s.requests_warnings?.length
            ? s.requests_warnings.join("; ")
            : "to Sonarr / Radarr"
        }
        tone={s.requests_warnings?.length ? "warning" : undefined}
      />
      {tokens > 0 && (
        <StatTile
          icon={Sparkles}
          label="AI tokens"
          value={tokens.toLocaleString()}
          hint={stepInline ? `${stepInline} tokens` : "curate + AI sources"}
          title="Total AI tokens this run cost, split by what the AI did. Turn AI sources off in Settings → Recommendations to lower it."
        />
      )}
      {(exa > 0 || exaCacheHits > 0) && (
        <StatTile
          icon={Search}
          label="Exa searches"
          value={exa}
          // A warm cache means most lookups aren't billed — showing only the billed "1" made a
          // fully-cached run look like the source did nothing. The hint names what the cache served.
          hint={
            exaCacheHits > 0
              ? `billed · ${exaCacheHits.toLocaleString()} from cache`
              : "web lookups · billed per search"
          }
          title="Billable Exa web-search requests this run made — a count, not tokens. Exa bills per search; results are cached for two weeks and shared across everyone, so most lookups are served from cache and cost nothing."
        />
      )}
    </div>
  );
}
