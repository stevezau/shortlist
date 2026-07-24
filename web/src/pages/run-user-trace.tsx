/** "How we picked for {name}" — the whole run for one person, as ONE connected flow you can read
 *  top to bottom, per library. A server can have several libraries (Movies, 4K Movies, TV Shows,
 *  custom-named), so real library NAMES are the tabs; picking a tab shows that library's run end to
 *  end: what they watched there → the seeds we pulled from it (and why each one mattered) → every
 *  place we searched, each title in and out with the reason it stayed or fell → what we finally put
 *  in the row and why. If the run failed for this person, the error leads. The trace blob is large,
 *  so it's fetched on demand for this page only. */
import {
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronRight,
  Globe,
  History,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { provenanceLabel, sourceLabel } from "@/lib/pick-provenance";
import { useRunUserTrace } from "@/lib/queries";
import type {
  Pick,
  RunLibraryBreakdown,
  RunUserTrace,
  RunUserTraceResponse,
  TraceFate,
  TraceReturn,
  TraceSeed,
  TraceSeedQuery,
  TraceSource,
  TraceWatch,
  TraceWeb,
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
          isEmpty={(d) => isEmptyTrace(d)}
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

/** A trace is worth showing if it has ANY stage, an error to explain, or a delivered ending. */
function isEmptyTrace(d: RunUserTraceResponse): boolean {
  const t = d.trace ?? {};
  const hasStages = Boolean(
    t.history || (t.seeds ?? []).length || (t.gathers ?? []).length,
  );
  return !hasStages && !d.error && (d.breakdown ?? []).length === 0;
}

function TraceSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-10 w-96" />
      <Skeleton className="h-[28rem] w-full" />
    </div>
  );
}

export function TraceView({ data }: { data: RunUserTraceResponse }) {
  const name = data.display_name || data.username;
  const libraries = useMemo(() => buildLibraries(data), [data]);
  const [active, setActive] = useState(libraries[0]?.key ?? "");
  const current = libraries.find((l) => l.key === active) ?? libraries[0];

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          How we picked for {name}
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          The whole run for this person, one library at a time — from what they
          watched all the way to what we put in their row.
        </p>
      </header>

      {data.error && <ErrorBanner error={data.error} />}
      {data.reason && !data.error && <SkipBanner reason={data.reason} />}

      {libraries.length === 0 ? (
        <EmptyState
          title="No per-library detail for this run"
          hint="We recorded an outcome but not the per-library flow — this run predates library-level tracing."
        />
      ) : (
        <>
          <LibraryTabs
            libraries={libraries}
            active={current?.key ?? ""}
            onSelect={setActive}
          />
          {current && <LibraryFlow lib={current} />}
        </>
      )}
    </div>
  );
}

function ErrorBanner({ error }: { error: string }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4">
      <AlertTriangle
        className="mt-0.5 h-5 w-5 shrink-0 text-destructive"
        aria-hidden="true"
      />
      <div className="space-y-1">
        <p className="text-sm font-medium text-destructive">
          This run failed for this person
        </p>
        <p className="text-sm text-muted-foreground">
          The stages below show how far we got before it stopped.
        </p>
        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-muted-foreground">
          {error}
        </pre>
      </div>
    </div>
  );
}

function SkipBanner({ reason }: { reason: string }) {
  return (
    <div className="rounded-lg border bg-muted/40 p-4 text-sm">
      <span className="font-medium">Skipped this person — </span>
      <span className="text-muted-foreground">{reason}</span>
    </div>
  );
}

// ── Per-library model ─────────────────────────────────────────────────────────
// The engine trace is keyed by stage (history/seeds/gathers) with a library NAME on each watch and
// seed, and its search sources work per media TYPE (movies vs shows), not per named library. So a
// library tab is assembled here: its watches/seeds by name, the sources for its media type, and the
// delivered picks whose breakdown targets it.

interface LibraryView {
  key: string;
  label: string;
  /** movie | show | both — how this library's candidate SEARCH is scoped (search is per-type). */
  media: string;
  /** The most-recent watches recorded (a bounded sample — the true totals are the counts below). */
  watched: TraceWatch[];
  /** True distinct-title watch totals for this tab's media type(s), NOT the sample length: the
   *  recent sample is time-ordered and can be all TV, so a movie tab may sample only a handful. */
  watchedMovies: number;
  watchedShows: number;
  seeds: TraceSeed[];
  /** Every source EXCEPT llm_web, which is rendered on its own from `web`/`webSource` (it has a
   *  richer story than a contribution count — the web searches and the AI's proposals). */
  sources: TraceSource[];
  web: TraceWeb | null;
  /** The llm_web source row (its status/contribution), paired with `web` in the AI-search card. */
  webSource: TraceSource | null;
  discoverGenres: Record<string, string[]>;
  delivered: RunLibraryBreakdown[];
  /** True when search is shared with other libraries of the same media type (be honest about it). */
  sharedSearch: boolean;
}

function buildLibraries(data: RunUserTraceResponse): LibraryView[] {
  const trace: RunUserTrace = data.trace ?? {};
  const watches = trace.history?.recent ?? [];
  // The true distinct-title totals across ALL history, not the length of the recent sample. The
  // sample is time-ordered and bounded, so a heavy TV watcher's movie tab sees only a few recent
  // movie titles — these are what "watched N movies / M shows" should actually report.
  const totalMovies = trace.history?.watched_movies ?? 0;
  const totalShows = trace.history?.watched_shows ?? 0;
  // Exact per-library totals (split by media) when the run recorded them — this distinguishes two
  // libraries of the same media type, which the server-wide per-media total above cannot.
  const byLibrary = trace.history?.watched_by_library;
  const seeds = trace.seeds ?? [];
  const gathers = trace.gathers ?? [];
  const breakdown = data.breakdown ?? [];

  // Every library name we know about, in a stable first-seen order: delivered rows first (the
  // outcome), then any library that only shows up in watches/seeds.
  const order: string[] = [];
  const seen = new Set<string>();
  const remember = (label: string) => {
    if (label && !seen.has(label)) {
      seen.add(label);
      order.push(label);
    }
  };
  for (const b of breakdown) remember(b.library_title);
  for (const w of watches) remember(w.library || mediaGroupLabel(w.media));
  for (const s of seeds) remember(s.library || mediaGroupLabel(s.media));

  // Which media type each library holds — inferred from its own watches/seeds, so we can attach the
  // right (per-type) search sources. A library with only movie watches is a movie library.
  const mediaOf = new Map<string, Set<string>>();
  const note = (label: string, media: string) => {
    if (!label) return;
    const set = mediaOf.get(label) ?? new Set<string>();
    set.add(media);
    mediaOf.set(label, set);
  };
  for (const w of watches) note(w.library || mediaGroupLabel(w.media), w.media);
  for (const s of seeds) note(s.library || mediaGroupLabel(s.media), s.media);
  for (const b of breakdown) {
    for (const p of b.picks) note(b.library_title, p.media_type ?? "");
  }

  // How many named libraries share each media type — for the honest "search shared across your movie
  // libraries" note.
  const libsPerMedia = new Map<string, number>();
  for (const [, medias] of mediaOf) {
    for (const m of medias) libsPerMedia.set(m, (libsPerMedia.get(m) ?? 0) + 1);
  }

  return order.map((label) => {
    const medias = mediaOf.get(label) ?? new Set<string>();
    const primaryMedia =
      medias.size === 1 ? ([...medias][0] ?? "both") : "both";
    const libWatches = watches.filter(
      (w) => (w.library || mediaGroupLabel(w.media)) === label,
    );
    const libSeeds = seeds.filter(
      (s) => (s.library || mediaGroupLabel(s.media)) === label,
    );
    // A gather is relevant to this library if its pool covers this library's media type. Union the
    // sources across those gathers, keeping only the per-return rows for this library's media.
    const relevant = gathers.filter((g) => poolCoversMedia(g.pool, medias));
    const merged = mergeSourcesForMedia(
      relevant.flatMap((g) => g.sources ?? []),
      medias,
    );
    // llm_web is pulled OUT of the generic source list: it has its own rich card (the web searches
    // it ran + the titles the AI proposed), so showing it twice — once as a bare "Contributed N"
    // row and once as the detailed card — is the confusing duplication we're removing.
    const sources = merged.filter((s) => s.source !== "llm_web");
    const webSource = merged.find((s) => s.source === "llm_web") ?? null;
    const web = relevant.map((g) => g.web).find(Boolean) ?? null;
    const discoverGenres: Record<string, string[]> = {};
    for (const g of relevant) {
      for (const [m, names] of Object.entries(g.discover_genres ?? {})) {
        if (medias.size === 0 || medias.has(m)) discoverGenres[m] = names;
      }
    }
    const sharedSearch = [...medias].some(
      (m) => (libsPerMedia.get(m) ?? 0) > 1,
    );
    // The true watched totals belong to this tab's media type(s). A movie-only tab reports the
    // movie total; a "both" tab reports both. These come from the full-history counts, not the
    // bounded recent sample, so a TV-heavy watcher's movie tab no longer reads "4 watched". Prefer
    // the exact per-library split (which distinguishes two same-type libraries); fall back to the
    // server-wide per-media total for runs recorded before per-library totals existed.
    const libTotals = byLibrary?.[label];
    const watchedMovies = libTotals
      ? libTotals.movie
      : medias.size === 0 || medias.has("movie")
        ? totalMovies
        : 0;
    const watchedShows = libTotals
      ? libTotals.show
      : medias.size === 0 || medias.has("show")
        ? totalShows
        : 0;
    return {
      key: label,
      label,
      media: primaryMedia,
      watched: libWatches,
      watchedMovies,
      watchedShows,
      seeds: libSeeds,
      sources,
      web,
      webSource,
      discoverGenres,
      delivered: breakdown.filter((b) => b.library_title === label),
      sharedSearch,
    };
  });
}

/** A gather pool is labelled "{media} · {sources}"; it covers a library if its media part overlaps
 *  the library's media types (or is "both"). Blank/legacy pools are treated as covering everything. */
function poolCoversMedia(pool: string, medias: Set<string>): boolean {
  const media = pool.split(" · ")[0]?.trim() ?? "";
  if (!media || media === "both" || medias.size === 0) return true;
  return medias.has(media);
}

/** Union the same source across gathers into one row, keeping only the per-return rows whose media
 *  belongs to this library, and re-tallying disposition from those rows so the counts match. */
function mergeSourcesForMedia(
  sources: TraceSource[],
  medias: Set<string>,
): TraceSource[] {
  const byName = new Map<string, TraceSource>();
  for (const src of sources) {
    const queries = (src.queries ?? [])
      .filter((q) => medias.size === 0 || medias.has(q.media))
      .map((q) => ({ ...q }));
    const existing = byName.get(src.source);
    if (existing) {
      existing.queries = [...(existing.queries ?? []), ...queries];
      existing.contributed += src.contributed;
    } else {
      byName.set(src.source, { ...src, queries });
    }
  }
  // Re-tally disposition from the (possibly filtered) queries so per-tab counts are truthful.
  for (const src of byName.values()) {
    const tally: Record<string, number> = {};
    for (const q of src.queries ?? []) {
      for (const r of q.returned)
        if (r.fate) tally[r.fate] = (tally[r.fate] ?? 0) + 1;
    }
    if (Object.keys(tally).length > 0) src.disposition = tally;
  }
  return [...byName.values()];
}

// ── Tabs ────────────────────────────────────────────────────────────────────

function LibraryTabs({
  libraries,
  active,
  onSelect,
}: {
  libraries: LibraryView[];
  active: string;
  onSelect: (key: string) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Libraries"
      className="flex flex-wrap gap-2 border-b pb-px"
    >
      {libraries.map((lib) => {
        const selected = lib.key === active;
        return (
          <button
            key={lib.key}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onSelect(lib.key)}
            className={cn(
              "-mb-px flex items-center gap-2 rounded-t-md border-b-2 px-4 py-2.5 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              selected
                ? "border-primary text-foreground"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {lib.label}
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-xs font-medium tabular-nums",
                selected
                  ? "bg-primary/10 text-primary"
                  : "bg-muted text-muted-foreground",
              )}
            >
              {lib.delivered.reduce((n, b) => n + b.picks.length, 0) || "—"}
            </span>
          </button>
        );
      })}
    </div>
  );
}

// ── One library's whole flow ──────────────────────────────────────────────────

interface FlowStepDef {
  /** Stable per-library id, used for the anchor and scroll-spy. */
  id: string;
  n: number;
  icon: LucideIcon;
  /** Short label for the left rail. */
  rail: string;
  /** A count shown as a chip next to the rail label and step title (omit to hide). */
  count?: number;
  title: string;
  subtitle?: string;
  body: ReactNode;
}

function LibraryFlow({ lib }: { lib: LibraryView }) {
  const searchNoun = mediaLabel(lib.media).toLowerCase();
  const hasWeb = Boolean(lib.web || lib.webSource);
  const placesSearched = lib.sources.length + (hasWeb ? 1 : 0);
  const totalWatched = lib.watchedMovies + lib.watchedShows;
  const deliveredCount = lib.delivered.reduce((n, b) => n + b.picks.length, 0);
  const steps: FlowStepDef[] = [
    {
      id: `${lib.key}-watched`,
      n: 1,
      icon: History,
      rail: "Watched",
      count: totalWatched || lib.watched.length,
      title: `What they watched in ${lib.label}`,
      subtitle:
        totalWatched > lib.watched.length
          ? `${watchedSummary(lib)} — their most recent are below (we keep tonight's picks anchored to what they've watched lately).`
          : undefined,
      body:
        lib.watched.length > 0 ? (
          <ul className="flex flex-wrap gap-1.5">
            {lib.watched.map((w, i) => (
              <li key={`${w.title}-${i}`}>
                <Badge variant="secondary" className="font-normal">
                  {w.title}
                  {w.year ? ` (${w.year})` : ""}
                </Badge>
              </li>
            ))}
          </ul>
        ) : (
          <Muted>
            No recent watches recorded here — seeds may come from a shared media
            type.
          </Muted>
        ),
    },
    {
      id: `${lib.key}-seeds`,
      n: 2,
      icon: Sparkles,
      rail: "Seeds",
      count: lib.seeds.length,
      title: "Titles we searched from",
      subtitle:
        "The watches that best represent their taste — watched most, and most recently. Each one becomes a search seed.",
      body:
        lib.seeds.length > 0 ? (
          <SeedList seeds={lib.seeds} />
        ) : (
          <Muted>No seeds derived for this library.</Muted>
        ),
    },
    {
      id: `${lib.key}-searched`,
      n: 3,
      icon: Search,
      rail: "Searched",
      count: placesSearched,
      title: "Where we searched, and every title in and out",
      subtitle: lib.sharedSearch
        ? `Each seed above fans out to every place we look for ${searchNoun}s. We search by taste, not by library, so these results are shared across your ${searchNoun} libraries — each title shows whether it made this library's shortlist or why it fell out.`
        : `Each seed above fans out to every place we look. Below is each one, the exact queries we sent, and what came back — with whether each title made the shortlist or the reason it didn't.`,
      body: (
        <SourcesFlow
          sources={lib.sources}
          web={lib.web}
          webSource={lib.webSource}
          discoverGenres={lib.discoverGenres}
        />
      ),
    },
    {
      id: `${lib.key}-delivered`,
      n: 4,
      icon: ArrowRight,
      rail: "Delivered",
      count: deliveredCount,
      title: `What we put in ${lib.label}, and why`,
      body:
        lib.delivered.length > 0 ? (
          <DeliveredList delivered={lib.delivered} />
        ) : (
          <Muted>Nothing was delivered to this library this run.</Muted>
        ),
    },
  ];

  const active = useScrollSpy(steps.map((s) => s.id));

  return (
    <div className="flex gap-6">
      <StepRail steps={steps} active={active} />
      <div className="min-w-0 flex-1 space-y-4">
        {steps.map((step) => (
          <FlowStep key={step.id} step={step} />
        ))}
      </div>
    </div>
  );
}

/** The left "what step are we at" rail — a connected vertical stepper: numbered dots joined by a
 *  spine, sticky, click-to-jump, highlighting the step currently in view. */
function StepRail({ steps, active }: { steps: FlowStepDef[]; active: string }) {
  const activeIndex = steps.findIndex((s) => s.id === active);
  return (
    <nav
      aria-label="Steps"
      className="sticky top-6 hidden h-fit w-44 shrink-0 flex-col md:flex"
    >
      {steps.map((step, i) => {
        const on = step.id === active;
        const done = i < activeIndex;
        const last = i === steps.length - 1;
        return (
          <a
            key={step.id}
            href={`#${step.id}`}
            aria-current={on ? "true" : undefined}
            className="group relative flex gap-3 rounded-md py-1 pl-1 pr-2 focus-visible:outline-none"
          >
            {/* The spine + numbered dot. The connector reaches from this dot to the next. */}
            <div className="relative flex w-7 shrink-0 flex-col items-center">
              {!last && (
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute left-1/2 top-7 h-[calc(100%-1.25rem)] w-px -translate-x-1/2",
                    done ? "bg-primary/40" : "bg-border",
                  )}
                />
              )}
              <span
                className={cn(
                  "z-10 flex h-7 w-7 items-center justify-center rounded-full border text-xs font-semibold transition-colors",
                  on
                    ? "border-primary bg-primary text-primary-foreground"
                    : done
                      ? "border-primary/40 bg-primary/10 text-primary"
                      : "border-border bg-background text-muted-foreground group-hover:border-primary/40 group-hover:text-foreground",
                )}
              >
                {step.n}
              </span>
            </div>
            <div className="min-w-0 flex-1 py-1">
              <span
                className={cn(
                  "flex items-center gap-1.5 text-sm transition-colors",
                  on
                    ? "font-medium text-foreground"
                    : "text-muted-foreground group-hover:text-foreground",
                )}
              >
                {step.rail}
                {step.count !== undefined && step.count > 0 && (
                  <span className="text-xs text-muted-foreground">
                    {step.count}
                  </span>
                )}
              </span>
            </div>
          </a>
        );
      })}
    </nav>
  );
}

/** One numbered stage in the vertical flow. Its `id` anchors the rail's scroll-spy + jump links. */
function FlowStep({ step }: { step: FlowStepDef }) {
  const Icon = step.icon;
  return (
    <section
      id={step.id}
      className="scroll-mt-6 rounded-xl border bg-card p-5 shadow-sm transition-shadow target:ring-2 target:ring-primary/40 hover:shadow-md"
    >
      <div className="mb-4 flex items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary ring-1 ring-inset ring-primary/20">
          <Icon className="h-4 w-4" aria-hidden={true} />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <h2 className="flex items-center gap-2 text-base font-semibold tracking-tight">
            {step.title}
            {step.count !== undefined && step.count > 0 && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium tabular-nums text-muted-foreground">
                {step.count}
              </span>
            )}
          </h2>
          {step.subtitle && (
            <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
              {step.subtitle}
            </p>
          )}
        </div>
      </div>
      <div className="sm:pl-12">{step.body}</div>
    </section>
  );
}

/** Track which step section is currently in view, so the rail can highlight it. Returns the id of
 *  the topmost section whose heading has scrolled into (or above) the top of the viewport. */
function useScrollSpy(ids: string[]): string {
  const key = ids.join(",");
  const [active, setActive] = useState(ids[0] ?? "");
  useEffect(() => {
    // Guard for environments without the API (jsdom under vitest, very old browsers): the rail
    // still renders and jump-links work; it just won't auto-highlight the step in view.
    if (typeof IntersectionObserver === "undefined") return;
    const sections = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (sections.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        // The step whose top is nearest just below the rail offset wins. Prefer intersecting
        // sections; among them, the one closest to the top of the viewport.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.id);
      },
      // A band near the top of the viewport: a section is "active" once its top passes into the
      // top ~30% and until it leaves — so the rail tracks the heading you're reading.
      { rootMargin: "-10% 0px -70% 0px", threshold: 0 },
    );
    sections.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
    // Re-bind when the set of step ids changes (i.e. a different library tab).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return active;
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

// ── Stage 2: seeds, with the "why" spelled out ────────────────────────────────

function SeedList({ seeds }: { seeds: TraceSeed[] }) {
  const max = Math.max(...seeds.map((s) => s.weight), 0.0001);
  return (
    <div className="space-y-3.5">
      <p className="flex items-center gap-2 text-xs text-muted-foreground">
        <span
          className="h-2 w-8 rounded-full bg-gradient-to-r from-primary/40 to-primary"
          aria-hidden="true"
        />
        Longer bar = a stronger influence on tonight’s picks (watched more often
        and more recently).
      </p>
      <ul className="space-y-3">
        {seeds.map((s) => {
          const pct = Math.round((s.weight / max) * 100);
          return (
            <li key={`${s.media}-${s.tmdb_id}`} className="space-y-1.5">
              <div className="flex items-baseline justify-between gap-3">
                <span className="truncate text-sm font-medium">{s.title}</span>
                {seedWhy(s) && (
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {seedWhy(s)}
                  </span>
                )}
              </div>
              <div
                className="h-2 overflow-hidden rounded-full bg-muted"
                role="img"
                aria-label={`Influence relative to top title: ${pct}%`}
              >
                <div
                  className="h-full rounded-full bg-gradient-to-r from-primary/50 to-primary transition-all"
                  style={{ width: `${Math.max(4, pct)}%` }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** "watched 4×, 3 days ago" from the weight ingredients — or "" on legacy runs that lack them. */
function seedWhy(s: TraceSeed): string {
  if (s.watch_count === undefined && s.recency_days === undefined) return "";
  const times = s.watch_count === undefined ? "" : `watched ${s.watch_count}×`;
  const when =
    s.recency_days === undefined
      ? ""
      : s.recency_days <= 0
        ? "most recently"
        : `${s.recency_days} day${s.recency_days === 1 ? "" : "s"} ago`;
  return [times, when].filter(Boolean).join(", ");
}

// ── Stage 3: sources, each title in and out ───────────────────────────────────

/** The search step rendered as a branch: one "seeds" node at the top, then a card per place we
 *  looked, each hung off a short connector so it reads as a fan-out rather than a flat list. */
function SourcesFlow({
  sources,
  web,
  webSource,
  discoverGenres,
}: {
  sources: TraceSource[];
  web: TraceWeb | null;
  webSource: TraceSource | null;
  discoverGenres: Record<string, string[]>;
}) {
  const branchCount = sources.length + (web || webSource ? 1 : 0);
  if (branchCount === 0)
    return <Muted>No candidate sources ran for this library.</Muted>;
  return (
    <div>
      <div className="mb-1 flex items-center gap-2 text-xs font-medium text-muted-foreground">
        <span className="flex h-6 items-center rounded-full bg-muted px-2.5 tabular-nums">
          Your seeds
        </span>
        <span aria-hidden="true">fanned out to</span>
        <span className="tabular-nums">
          {branchCount} {branchCount === 1 ? "place" : "places"}
        </span>
      </div>
      {/* The branch: a vertical spine down the left with each place tee'd off it. */}
      <ul className="relative space-y-3 border-l-2 border-dashed border-border pl-5">
        {sources.map((src) => (
          <li key={src.source} className="relative">
            <BranchConnector />
            <SourceCard src={src} discoverGenres={discoverGenres} />
          </li>
        ))}
        {(web || webSource) && (
          <li className="relative">
            <BranchConnector />
            <WebSourceCard web={web} source={webSource} />
          </li>
        )}
      </ul>
    </div>
  );
}

/** The short horizontal elbow that ties a branch card back to the spine on its left. */
function BranchConnector() {
  return (
    <span
      aria-hidden="true"
      className="absolute -left-5 top-6 h-px w-5 bg-border"
    />
  );
}

function SourceCard({
  src,
  discoverGenres,
}: {
  src: TraceSource;
  discoverGenres: Record<string, string[]>;
}) {
  const failed = src.status === "failed";
  const queries = src.queries ?? [];
  const disp = src.disposition ?? {};
  const kept = disp.kept ?? 0;
  const droppedCount = Object.entries(disp)
    .filter(([fate]) => fate !== "kept")
    .reduce((n, [, c]) => n + c, 0);

  return (
    <div className="overflow-hidden rounded-lg border bg-background">
      <div className="flex items-start justify-between gap-3 p-3">
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-medium">{sourceLabel(src.source)}</p>
          {failed ? (
            <p className="text-xs text-destructive">
              Couldn’t reach it{src.detail ? ` — ${src.detail}` : ""}
            </p>
          ) : kept > 0 || droppedCount > 0 ? (
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1 text-success">
                <Check className="h-3 w-3" aria-hidden="true" />
                {kept} kept
              </span>
              <span aria-hidden="true">·</span>
              <span>{droppedCount} dropped</span>
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Contributed {src.contributed.toLocaleString()}
            </p>
          )}
        </div>
        <Badge
          variant={failed ? "destructive" : "secondary"}
          className="shrink-0"
        >
          {failed ? "Failed" : src.contributed.toLocaleString()}
        </Badge>
      </div>

      {src.source === "tmdb_discover" &&
        Object.keys(discoverGenres).length > 0 && (
          <p className="border-t px-3 py-2 text-xs text-muted-foreground">
            Popular titles in the genres they watch most:{" "}
            {Object.entries(discoverGenres)
              .map(([m, gs]) => `${mediaLabel(m)} — ${gs.join(", ") || "none"}`)
              .join("; ")}
            .
          </p>
        )}

      {queries.length > 0 && (
        <details className="group border-t">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
            <ChevronRight
              className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
              aria-hidden="true"
            />
            Follow it title by title
          </summary>
          <ul className="space-y-3 border-t px-3 py-3">
            {queries.map((q, i) => (
              <SeedQueryRow key={`${q.seed}-${i}`} query={q} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function SeedQueryRow({ query }: { query: TraceSeedQuery }) {
  return (
    <li className="text-sm">
      <div className="flex items-center gap-1.5">
        <Search
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="text-muted-foreground">Searched from</span>
        <span className="font-medium">{query.seed}</span>
        <Badge variant="outline" className="shrink-0 font-normal">
          {mediaLabel(query.media)}
        </Badge>
      </div>
      {query.returned.length > 0 ? (
        <ul className="mt-1.5 space-y-1 pl-5">
          {query.returned.map((r, i) => (
            <ReturnRow key={`${r.tmdb_id}-${i}`} ret={r} />
          ))}
          {query.total > query.returned.length && (
            <li className="text-xs text-muted-foreground">
              +{(query.total - query.returned.length).toLocaleString()} more not
              shown
            </li>
          )}
        </ul>
      ) : (
        <p className="mt-0.5 pl-5 text-xs text-muted-foreground">
          nothing returned
        </p>
      )}
    </li>
  );
}

function ReturnRow({ ret }: { ret: TraceReturn }) {
  const kept = ret.fate === "kept";
  return (
    <li className="flex items-center gap-2 text-xs">
      {ret.fate === undefined ? (
        <span className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      ) : kept ? (
        <Check
          className="h-3.5 w-3.5 shrink-0 text-success"
          aria-hidden="true"
        />
      ) : (
        <X
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      )}
      <span
        className={cn(
          "truncate",
          !kept && ret.fate !== undefined && "text-muted-foreground",
        )}
      >
        {ret.title}
      </span>
      {ret.fate !== undefined && !kept && (
        <span className="shrink-0 text-muted-foreground/80">
          {fateLabel(ret.fate)}
        </span>
      )}
    </li>
  );
}

/** The AI web-search branch. This is a TWO-step source and the UI must say so, because the two
 *  steps look alike but aren't: (A) real web searches run — one per seed, via Exa and/or the model's
 *  own built-in search — and (B) the AI then READS all those results and proposes titles to watch.
 *  So the many search queries are NOT the one prompt at the bottom: the queries are step A (Exa), the
 *  prompt is step B (what the model was handed). Conflating them was the reported confusion. */
function WebSourceCard({
  web,
  source,
}: {
  web: TraceWeb | null;
  source: TraceSource | null;
}) {
  const proposed = [
    ...new Set([...(web?.native_proposed ?? []), ...(web?.proposed ?? [])]),
  ];
  const resolved = new Set(web?.resolved ?? []);
  const unresolved = new Set(web?.unresolved ?? []);
  const searches = web?.searches ?? [];
  const failed = source?.status === "failed";
  const kept = source?.disposition?.kept ?? 0;
  const mech = webMechanism(web?.mode ?? "", searches.length > 0);

  return (
    <div className="overflow-hidden rounded-lg border">
      <div className="flex items-start gap-2 p-3">
        <Globe
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-medium">{sourceLabel("llm_web")}</p>
          <p className="text-xs text-muted-foreground">{mech}</p>
        </div>
        <Badge
          variant={failed ? "destructive" : "secondary"}
          className="shrink-0"
        >
          {failed ? "Failed" : proposed.length}
        </Badge>
      </div>

      {failed && source?.detail && (
        <p className="border-t px-3 py-2 text-xs text-destructive">
          Couldn’t reach it — {source.detail}
        </p>
      )}

      <div className="space-y-4 border-t p-3">
        {searches.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium">
              <span className="mr-1.5 rounded bg-muted px-1.5 py-0.5 font-semibold tabular-nums">
                Step 1
              </span>
              Web searches we ran — one per seed
            </p>
            <ul className="space-y-1.5">
              {searches.map((s, i) => (
                <li key={i} className="text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Search
                      className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
                      aria-hidden="true"
                    />
                    <span className="italic">“{s.query}”</span>
                    {s.cached && (
                      <Badge
                        variant="secondary"
                        className="shrink-0 text-[10px]"
                      >
                        reused an earlier search
                      </Badge>
                    )}
                  </div>
                  {s.returned.length > 0 && (
                    <span className="mt-0.5 block pl-5 text-xs text-muted-foreground">
                      Turned up: {s.returned.join(", ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {proposed.length > 0 && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium">
              {searches.length > 0 && (
                <span className="mr-1.5 rounded bg-muted px-1.5 py-0.5 font-semibold tabular-nums">
                  Step 2
                </span>
              )}
              Titles the AI then suggested
              <span className="ml-1 font-normal text-muted-foreground">
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
            {kept > 0 && (
              <p className="text-xs text-muted-foreground">
                {kept} of these made this library’s shortlist.
              </p>
            )}
          </div>
        )}

        {web?.rag_user && (
          <details className="rounded-lg border bg-muted/20 p-3 text-sm">
            <summary className="cursor-pointer font-medium text-muted-foreground hover:text-foreground">
              {searches.length > 0
                ? "See the exact prompt the AI got in step 2"
                : "See the exact prompt the AI was given"}
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
    </div>
  );
}

/** Plain-English description of HOW the AI web search ran, from the mode + whether Exa searches were
 *  recorded. The three engine modes (candidates.py): native = the model's own built-in web search;
 *  exa = the external Exa search, then the model ranks; auto = both, unioned. */
function webMechanism(mode: string, hasSearches: boolean): string {
  if (mode === "native")
    return "The AI model’s own built-in web search proposed titles directly.";
  if (mode === "exa" || (hasSearches && mode !== "auto"))
    return "We searched the web with Exa, then the AI read the results and proposed titles.";
  if (mode === "auto")
    return hasSearches
      ? "The AI model’s built-in web search AND an Exa web search, combined — the AI proposed titles from both."
      : "The AI model’s own built-in web search proposed titles directly.";
  return "The AI proposed titles to watch next from a web search.";
}

// ── Stage 4: delivered picks, with reasons ────────────────────────────────────

function DeliveredList({ delivered }: { delivered: RunLibraryBreakdown[] }) {
  return (
    <div className="space-y-4">
      {delivered.map((b, i) => (
        <div key={`${b.row_slug}-${i}`} className="space-y-2">
          {delivered.length > 1 && (
            <p className="text-sm font-medium">{b.row_title}</p>
          )}
          <ol className="divide-y rounded-lg border bg-background">
            {b.picks.map((p) => (
              <DeliveredPick key={p.rank} pick={p} />
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}

function DeliveredPick({ pick }: { pick: Pick }) {
  const prov = provenanceLabel(pick);
  return (
    <li className="flex items-start gap-3 p-3 text-sm">
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold tabular-nums text-primary">
        {pick.rank}
      </span>
      <div className="min-w-0 flex-1 space-y-0.5">
        <p className="font-medium leading-tight">{pick.title}</p>
        {pick.reason && <p className="text-muted-foreground">{pick.reason}</p>}
        {prov && (
          <p className="truncate text-xs text-muted-foreground/80">{prov}</p>
        )}
      </div>
    </li>
  );
}

// ── Plain-English helpers ─────────────────────────────────────────────────────

/** "Watched 598 movies and 40 shows" from the true totals — so a tab whose recent sample is all TV
 *  doesn't read as "4 watched". Only names the media type(s) this tab actually holds. */
function watchedSummary(lib: LibraryView): string {
  const parts: string[] = [];
  if (lib.watchedMovies > 0)
    parts.push(
      `${lib.watchedMovies.toLocaleString()} movie${lib.watchedMovies === 1 ? "" : "s"}`,
    );
  if (lib.watchedShows > 0)
    parts.push(
      `${lib.watchedShows.toLocaleString()} show${lib.watchedShows === 1 ? "" : "s"}`,
    );
  return parts.length > 0 ? `Watched ${parts.join(" and ")} here` : "";
}

/** "movie" → "Movie", "show" → "Show", "both" → "Movies & shows". */
function mediaLabel(media: string): string {
  if (media === "movie") return "Movie";
  if (media === "show") return "Show";
  if (media === "both") return "Movies & shows";
  return media;
}

/** Media-type heading when a real library name is unknown (legacy runs). Never wins over a name. */
function mediaGroupLabel(media: string): string {
  if (media === "movie") return "Movies";
  if (media === "show") return "TV Shows";
  return media || "Other";
}

/** Why a returned title didn't make the shortlist, in plain words. */
function fateLabel(fate: TraceFate): string {
  switch (fate) {
    case "already_watched":
      return "already watched";
    case "not_in_your_libraries":
      return "not in your libraries";
    case "excluded_genre":
      return "excluded genre";
    case "lost_ranking_cutoff":
      return "lost the ranking cut";
    case "not_returned":
      return "found by another source";
    default:
      return "";
  }
}
