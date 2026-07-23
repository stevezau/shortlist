/** "How we picked for {name}" — the full pipeline for one person in one run, laid out as a flow you
 *  can step through: what they watched → the titles we searched from → where we looked → what the AI
 *  searched and proposed. A dedicated page (not a dialog) so nothing clips and each stage is its own
 *  panel instead of one long scroll. The trace blob is large, so it's fetched on demand for this page
 *  only. */
import {
  Globe,
  History,
  type LucideIcon,
  Search,
  Sparkles,
} from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { sourceLabel } from "@/lib/pick-provenance";
import { useRunUserTrace } from "@/lib/queries";
import type {
  RunUserTrace,
  RunUserTraceResponse,
  TraceGather,
  TraceSeed,
  TraceSource,
  TraceWeb,
  TraceWebSearch,
} from "@/lib/types";
import { cn } from "@/lib/utils";

export function RunUserTracePage() {
  const { id, userId } = useParams();
  const runId = Number(id);
  const uid = Number(userId);
  const valid = Number.isFinite(runId) && Number.isFinite(uid);
  const query = useRunUserTrace(runId, uid, valid);

  return (
    <div className="space-y-6">
      <BackLink to={`/runs/${runId}`} label={`Back to run #${runId}`} />
      {!valid ? (
        <EmptyState
          title="That trace doesn’t exist"
          hint="The link may be wrong, or the run was removed."
        />
      ) : (
        <QueryBoundary
          query={query}
          skeleton={<TraceSkeleton />}
          isEmpty={(d) => !d.trace || Object.keys(d.trace).length === 0}
          empty={
            <EmptyState
              title="Nothing was recorded for this person"
              hint="This run happened before traces were added, or they were skipped before we gathered anything."
            />
          }
        >
          {(data) => <TraceView data={data} />}
        </QueryBoundary>
      )}
    </div>
  );
}

function TraceSkeleton() {
  return (
    <div className="grid gap-6 lg:grid-cols-[260px_minmax(0,1fr)]">
      <Skeleton className="h-72 w-full" />
      <Skeleton className="h-96 w-full" />
    </div>
  );
}

/** One clickable step in the pipeline flow. */
interface Stage {
  id: string;
  label: string;
  icon: LucideIcon;
  /** One-word count shown under the label in the nav, e.g. "42 watches". */
  summary: string;
  render: () => ReactNode;
}

function TraceView({ data }: { data: RunUserTraceResponse }) {
  const name = data.display_name || data.username;
  const stages = buildStages(data.trace);
  const [active, setActive] = useState(stages[0]?.id ?? "");
  const current = stages.find((s) => s.id === active) ?? stages[0];

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          How we picked for {name}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Every step tonight’s picks went through, in order. Click a step to see
          exactly what happened.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start">
        <StageFlow
          stages={stages}
          active={current?.id ?? ""}
          onSelect={setActive}
        />
        <div className="min-w-0">{current?.render()}</div>
      </div>
    </div>
  );
}

/** The pipeline as a connected, numbered flow — the left rail on desktop, a scrollable strip on
 *  mobile. Selecting a step swaps the panel on the right. */
function StageFlow({
  stages,
  active,
  onSelect,
}: {
  stages: Stage[];
  active: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav
      aria-label="Pipeline steps"
      className="flex gap-2 overflow-x-auto pb-2 lg:sticky lg:top-4 lg:flex-col lg:overflow-visible lg:pb-0"
    >
      {stages.map((stage, i) => {
        const Icon = stage.icon;
        const selected = stage.id === active;
        const last = i === stages.length - 1;
        return (
          <button
            key={stage.id}
            type="button"
            onClick={() => onSelect(stage.id)}
            aria-current={selected ? "step" : undefined}
            className={cn(
              "relative flex shrink-0 items-center gap-3 rounded-lg border p-3 text-left transition-colors lg:w-full",
              selected
                ? "border-primary/50 bg-primary/5"
                : "border-transparent hover:bg-muted/60",
            )}
          >
            {/* The connecting line down the flow (desktop only). */}
            {!last && (
              <span
                aria-hidden="true"
                className="absolute left-[27px] top-[46px] hidden h-[calc(100%-30px)] w-px bg-border lg:block"
              />
            )}
            <span
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
                selected
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden="true" />
            </span>
            <span className="min-w-0">
              <span
                className={cn(
                  "block truncate text-sm font-medium",
                  selected ? "text-foreground" : "text-foreground/80",
                )}
              >
                {stage.label}
              </span>
              <span className="block truncate text-xs text-muted-foreground">
                {stage.summary}
              </span>
            </span>
          </button>
        );
      })}
    </nav>
  );
}

/** Consistent heading + intro line for the panel on the right. */
function Panel({
  title,
  intro,
  children,
}: {
  title: string;
  intro: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-4 rounded-xl border p-5">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        <p className="max-w-2xl text-sm text-muted-foreground">{intro}</p>
      </div>
      {children}
    </section>
  );
}

function buildStages(trace: RunUserTrace): Stage[] {
  const stages: Stage[] = [];
  const history = trace.history;
  const seeds = trace.seeds ?? [];
  const gathers = trace.gathers ?? [];
  const gathersWithSources = gathers.filter(
    (g) => (g.sources ?? []).length > 0,
  );
  const webGathers = gathers.filter((g) => g.web);

  if (history) {
    stages.push({
      id: "history",
      label: "What they watched",
      icon: History,
      summary: `${history.total.toLocaleString()} recent watches`,
      render: () => <HistoryPanel history={history} />,
    });
  }
  if (seeds.length > 0) {
    stages.push({
      id: "starting",
      label: "Titles we searched from",
      icon: Sparkles,
      summary: `${seeds.length} title${seeds.length === 1 ? "" : "s"}`,
      render: () => <SeedsPanel seeds={seeds} />,
    });
  }
  if (gathersWithSources.length > 0) {
    stages.push({
      id: "sources",
      label: "Where we looked",
      icon: Search,
      summary: `${countSources(gathersWithSources)} sources`,
      render: () => <SourcesPanel gathers={gathersWithSources} />,
    });
  }
  if (webGathers.length > 0) {
    stages.push({
      id: "web",
      label: "AI web search",
      icon: Globe,
      summary: `${countWebProposed(webGathers)} suggestions`,
      render: () => <WebPanel gathers={webGathers} />,
    });
  }
  return stages;
}

function countSources(gathers: TraceGather[]): number {
  const names = new Set<string>();
  for (const g of gathers) for (const s of g.sources ?? []) names.add(s.source);
  return names.size;
}

function countWebProposed(gathers: TraceGather[]): number {
  let n = 0;
  for (const g of gathers) {
    const web = g.web;
    if (!web) continue;
    n += new Set([...(web.native_proposed ?? []), ...(web.proposed ?? [])])
      .size;
  }
  return n;
}

// ── Panels ──────────────────────────────────────────────────────────────────

function HistoryPanel({
  history,
}: {
  history: NonNullable<RunUserTrace["history"]>;
}) {
  return (
    <Panel
      title="What they watched"
      intro="We start from what this person has actually been watching on your server. These are their most recent plays."
    >
      <div className="flex flex-wrap gap-3">
        <Stat value={history.total} label="recent watches" />
        <Stat value={history.watched_movies} label="movies finished" />
        <Stat value={history.watched_shows} label="shows played" />
      </div>
      {history.recent.length > 0 && (
        <div className="space-y-2">
          <p className="text-sm font-medium">Most recently watched</p>
          <ul className="flex flex-wrap gap-1.5">
            {history.recent.map((w, i) => (
              <li key={`${w.title}-${i}`}>
                <Badge variant="secondary" className="font-normal">
                  {w.title}
                  {w.year ? ` (${w.year})` : ""}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="rounded-lg border bg-muted/30 px-4 py-3">
      <p className="text-2xl font-semibold tabular-nums">
        {value.toLocaleString()}
      </p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

function SeedsPanel({ seeds }: { seeds: TraceSeed[] }) {
  const max = Math.max(...seeds.map((s) => s.weight), 0.0001);
  return (
    <Panel
      title="Titles we searched from"
      intro="From their history we pick the titles that best represent their taste — favouring what they watched recently and often — then find things like them. The bar shows how much each one shaped tonight’s picks."
    >
      <ul className="space-y-2">
        {seeds.map((s) => (
          <li key={`${s.media}-${s.tmdb_id}`} className="space-y-1">
            <div className="flex items-center justify-between gap-3">
              <span className="min-w-0 truncate text-sm">{s.title}</span>
              <Badge variant="outline" className="shrink-0 font-normal">
                {mediaLabel(s.media)}
              </Badge>
            </div>
            <div
              className="h-1.5 overflow-hidden rounded-full bg-muted"
              role="img"
              aria-label={`Influence: ${Math.round((s.weight / max) * 100)}%`}
            >
              <div
                className="h-full rounded-full bg-primary/70"
                style={{ width: `${Math.max(6, (s.weight / max) * 100)}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function SourcesPanel({ gathers }: { gathers: TraceGather[] }) {
  return (
    <Panel
      title="Where we looked"
      intro="Each place we searched for candidates, and how many titles it turned up. If one failed, the reason is shown so you can tell an empty result from an outage."
    >
      <div className="space-y-5">
        {gathers.map((gather, i) => (
          <div key={gather.pool || i} className="space-y-3">
            {gathers.length > 1 && (
              <p className="text-sm font-medium">{poolTitle(gather.pool)}</p>
            )}
            <ul className="space-y-2">
              {(gather.sources ?? []).map((src) => (
                <SourceRow key={src.source} src={src} />
              ))}
            </ul>
            {gather.discover_genres &&
              Object.keys(gather.discover_genres).length > 0 && (
                <p className="text-sm text-muted-foreground">
                  We also pulled titles popular in the genres they watch most:{" "}
                  {Object.entries(gather.discover_genres)
                    .map(
                      ([media, genres]) =>
                        `${mediaLabel(media)} — ${genres.join(", ") || "none"}`,
                    )
                    .join("; ")}
                  .
                </p>
              )}
          </div>
        ))}
      </div>
    </Panel>
  );
}

function SourceRow({ src }: { src: TraceSource }) {
  const failed = src.status === "failed";
  return (
    <li className="flex items-start justify-between gap-3 rounded-lg border p-3">
      <div className="min-w-0 space-y-0.5">
        <p className="text-sm font-medium">{sourceLabel(src.source)}</p>
        {failed ? (
          <p className="text-xs text-destructive">
            Couldn’t reach it{src.detail ? ` — ${src.detail}` : ""}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            {src.contributed > 0
              ? `Found ${src.contributed.toLocaleString()} candidate${
                  src.contributed === 1 ? "" : "s"
                }`
              : "Nothing new this time"}
          </p>
        )}
      </div>
      <Badge
        variant={failed ? "destructive" : "secondary"}
        className="shrink-0"
      >
        {failed ? "Failed" : src.contributed.toLocaleString()}
      </Badge>
    </li>
  );
}

function WebPanel({ gathers }: { gathers: TraceGather[] }) {
  return (
    <Panel
      title="AI web search"
      intro="For a broader net, the AI searches the web using their taste, then proposes titles. We check each one against TMDB — anything with no real match is dropped as a likely invention."
    >
      <div className="space-y-6">
        {gathers.map((gather, i) => (
          <WebGather key={gather.pool || i} web={gather.web!} />
        ))}
      </div>
    </Panel>
  );
}

function WebGather({ web }: { web: TraceWeb }) {
  const proposed = [
    ...new Set([...(web.native_proposed ?? []), ...(web.proposed ?? [])]),
  ];
  const resolved = new Set(web.resolved ?? []);
  const unresolved = new Set(web.unresolved ?? []);
  return (
    <div className="space-y-5">
      {web.searches && web.searches.length > 0 && (
        <div className="space-y-2">
          <p className="text-sm font-medium">What it searched for</p>
          <div className="space-y-2">
            {web.searches.map((s, i) => (
              <WebSearchRow key={i} search={s} />
            ))}
          </div>
        </div>
      )}

      {proposed.length > 0 && (
        <div className="space-y-2">
          <p className="text-sm font-medium">
            Titles the AI suggested{" "}
            <span className="font-normal text-muted-foreground">
              — struck-through ones had no real match and were dropped
            </span>
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {proposed.map((title, i) => {
              const dropped = unresolved.has(title) && !resolved.has(title);
              return (
                <li key={`${title}-${i}`}>
                  <Badge
                    variant={dropped ? "outline" : "secondary"}
                    className={cn(
                      "font-normal",
                      dropped && "text-muted-foreground line-through",
                    )}
                  >
                    {title}
                  </Badge>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {web.rag_user && (
        <details className="rounded-lg border bg-muted/20 p-3 text-sm">
          <summary className="cursor-pointer font-medium text-muted-foreground hover:text-foreground">
            See the exact instructions the AI was given
          </summary>
          {web.rag_system && (
            <pre className="mt-3 whitespace-pre-wrap rounded bg-background/70 p-3 font-mono text-[11px] leading-relaxed">
              {web.rag_system}
            </pre>
          )}
          <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap rounded bg-background/70 p-3 font-mono text-[11px] leading-relaxed">
            {web.rag_user}
          </pre>
        </details>
      )}
    </div>
  );
}

function WebSearchRow({ search }: { search: TraceWebSearch }) {
  return (
    <div className="rounded-lg border p-3">
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Search
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="italic">“{search.query}”</span>
        {search.cached && (
          <Badge variant="secondary" className="shrink-0 text-[10px]">
            reused an earlier search
          </Badge>
        )}
      </p>
      {search.returned.length > 0 && (
        <p className="mt-1.5 text-xs text-muted-foreground">
          Turned up: {search.returned.join(", ")}
        </p>
      )}
    </div>
  );
}

// ── Plain-English helpers ─────────────────────────────────────────────────────

/** "movie" → "Movie", "show" → "Show", "both" → "Movies & shows". */
function mediaLabel(media: string): string {
  if (media === "movie") return "Movie";
  if (media === "show") return "Show";
  if (media === "both") return "Movies & shows";
  return media;
}

/** The pool label the engine emits is "{media} · {source ids}"; only the media part is worth
 *  showing as a heading — the sources are listed in full below it. */
function poolTitle(pool: string): string {
  const media = pool.split(" · ")[0]?.trim() ?? pool;
  return mediaLabel(media);
}
