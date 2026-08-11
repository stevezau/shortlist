/** "How we picked for {name}" — the whole run for one person, as ONE connected flow you can read
 *  top to bottom, per library. A server can have several libraries (Movies, 4K Movies, TV Shows,
 *  custom-named), so real library NAMES are the tabs; picking a tab shows that library's run end to
 *  end: what they watched there → the seeds we pulled from it (and why each one mattered) → every
 *  place we searched, each title in and out with the reason it stayed or fell → what we finally put
 *  in the row and why. If the run failed for this person, the error leads. The trace blob is large,
 *  so it's fetched on demand for this page only. */
import {
  Ban,
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronRight,
  Clock,
  Globe,
  History,
  Filter,
  ListOrdered,
  Search,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { createContext, useContext, useMemo, useState } from "react";
import { useParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { provenanceLabel, sourceLabel } from "@/lib/pick-provenance";
import { Button } from "@/components/ui/button";
import { useBlockSeed, useRunUserTrace } from "@/lib/queries";
import {
  buildLibraries,
  fateLabel,
  mediaLabel,
  sourceRole,
  watchedSummary,
  webMechanism,
  type LibraryView,
} from "@/lib/trace";
import { useScrollSpy } from "@/lib/use-scroll-spy";
import type {
  Pick,
  RunLibraryBreakdown,
  RunUserTraceResponse,
  TraceRatings,
  TraceRequestOutcome,
  TraceReturn,
  TraceSeed,
  TraceSeedQuery,
  TraceSource,
  TraceWatch,
  TraceWeb,
  TraceSelection,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** What the request subsystem did with each wanted-but-missing title, keyed "<tmdb_id>:<media>".
 *  Page-scoped (one run, one user) so a deep return row can overlay "→ requested from Radarr" onto a
 *  "not in your libraries" drop without threading the map through every source/query component. */
const RequestsContext = createContext<Record<string, TraceRequestOutcome>>({});

function useRequestOutcome(
  tmdbId: number,
  media: string,
): TraceRequestOutcome | undefined {
  return useContext(RequestsContext)[`${tmdbId}:${media}`];
}

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
          {(data) => <TraceView data={data} userId={uid} />}
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

/** `userId` is optional so the trace renders standalone in tests and anywhere the id is unknown;
 *  without it the per-seed "don't seed" action simply isn't offered, since it has nobody to act on. */
export function TraceView({
  data,
  userId,
}: {
  data: RunUserTraceResponse;
  userId?: number;
}) {
  const name = data.display_name || data.username;
  const libraries = useMemo(() => buildLibraries(data), [data]);
  const [active, setActive] = useState(libraries[0]?.key ?? "");
  const current = libraries.find((l) => l.key === active) ?? libraries[0];

  return (
    <RequestsContext.Provider value={data.requests ?? {}}>
      <div className="space-y-6">
        <header className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">
            How we picked for {name}
          </h1>
          <p className="max-w-2xl text-sm text-muted-foreground">
            The whole run for this person, one library at a time — from what
            they watched all the way to what we put in their row.
          </p>
        </header>

        {data.error && <ErrorBanner error={data.error} />}
        {data.reason && !data.error && <SkipBanner reason={data.reason} />}

        {/* The banner above already says why there is nothing here. Blaming a legacy run for a
            deliberate skip would be a second, wrong explanation stacked on the right one. */}
        {libraries.length === 0 && !data.reason ? (
          <EmptyState
            title="No per-library detail for this run"
            hint="We recorded an outcome but not the per-library flow — this run predates library-level tracing."
          />
        ) : libraries.length === 0 ? null : (
          <>
            <LibraryTabs
              libraries={libraries}
              active={current?.key ?? ""}
              onSelect={setActive}
            />
            {current && (
              <LibraryFlow
                  lib={current}
                  userId={userId}
                  ratings={data.trace?.history?.ratings}
                  selection={(data.trace?.selection ?? []).filter(
                    (e) => e.library === current.label,
                  )}
                />
            )}
          </>
        )}
      </div>
    </RequestsContext.Provider>
  );
}

/** How many searches actually ran for this library, across every source.
 *
 *  From each source's `searched` count, never from `queries.length`: that list is a capped display
 *  sample, so counting it reported a 30-seed run as "searched 12". */
function searchesRun(lib: LibraryView): number {
  return lib.sources.reduce((n, src) => n + mediaSearches(src, lib.media), 0);
}

/** A tab's media is one string, and "both" means it covers every type — so sum the lot there. */
function mediaSearches(src: TraceSource, media: string): number {
  const per = src.searched ?? {};
  if (media && media !== "both" && per[media] != null) return per[media];
  return Object.values(per).reduce((n, v) => n + v, 0);
}

/** True when any source ran more searches than the trace kept a record of, so the step can say
 *  "showing 12 of 30" instead of quietly presenting the sample as the whole. */
function searchesSampled(lib: LibraryView): boolean {
  return lib.sources.some((src) => {
    const shown = (src.queries ?? []).length;
    return shown > 0 && mediaSearches(src, lib.media) > shown;
  });
}

/** What the release-date weight and the pool cap did to this library's shortlist. */
function shortlistBody(entries: TraceSelection[]): ReactNode {
  if (entries.length === 0) return null;
  return (
    <ul className="space-y-3">
      {entries.map((entry) => (
        <li key={entry.row} className="space-y-1 text-sm">
          <p>
            <span className="font-medium">{entry.row}</span>
            {entry.candidates != null && (
              <>
                {" — "}
                {entry.candidates} candidates survived filtering
                {entry.cut_cap
                  ? `, and the strongest ${entry.cut_cap} per media type were kept. Anything below that line could not reach the row.`
                  : "."}
              </>
            )}
          </p>
          <p className="text-muted-foreground">
            {entry.recency
              ? `Release date counted for ${Math.round(entry.recency * 100)}% here, so newer titles ranked above equally good older ones — and it applied to the cut itself, not just the order, so a newer title below the line could still get in.`
              : "Release date was ignored — a 1996 title and a 2024 one were judged the same."}
          </p>
        </li>
      ))}
    </ul>
  );
}

/** Whether the row was actually re-picked tonight, and what to do if it wasn't. This is the fact
 *  that was missing entirely: most nights a row is redelivered untouched and the page looked
 *  identical to a rebuild, so "I changed a setting and nothing moved" was unanswerable. */
function deliveryNote(entries: TraceSelection[]): ReactNode {
  if (entries.length === 0) return null;
  return (
    <ul className="mb-3 space-y-1.5 text-sm">
      {entries.map((entry) => (
        <li key={entry.row}>
          <span className="font-medium">{entry.row}</span>{" "}
          <span
            className={
              entry.decision === "carried_forward"
                ? "text-muted-foreground"
                : "text-foreground"
            }
          >
            {decisionLine(entry)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function decisionLine(entry: TraceSelection): string {
  const every = entry.rebuild_every_days;
  switch (entry.decision) {
    case "carried_forward":
      return `— not re-picked tonight; last run's titles were redelivered unchanged${
        every ? `. This row rebuilds about every ${every} days` : ""
      }. Raise Freshness, or change a setting that decides its titles, to rebuild it sooner.`;
    case "settings_changed":
      return "— rebuilt now because a setting that decides its titles changed.";
    case "refreshed":
      return "— refresh night: the strongest picks stayed, the weakest were swapped for new ones.";
    case "cold_start":
      return "— too little watch history, so it was filled from the server's top-rated titles.";
    default:
      return "— built fresh.";
  }
}

function ErrorBanner({ error }: { error: string }) {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4">
      <AlertTriangle
        className="mt-0.5 h-5 w-5 shrink-0 text-destructive-text"
        aria-hidden="true"
      />
      <div className="space-y-1">
        <p className="text-sm font-medium text-destructive-text">
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

function LibraryFlow({
  lib,
  userId,
  ratings,
  selection = [],
}: {
  lib: LibraryView;
  userId?: number;
  ratings?: TraceRatings;
  selection?: TraceSelection[];
}) {
  const searchNoun = mediaLabel(lib.media).toLowerCase();
  const hasWeb = Boolean(lib.web || lib.webSource);
  const placesSearched = lib.sources.length + (hasWeb ? 1 : 0);
  const totalWatched = lib.watchedMovies + lib.watchedShows;
  const deliveredCount = lib.delivered.reduce((n, b) => n + b.picks.length, 0);
  // Cold start pulls the highest-rated titles on the server, so there's no seed, no search fan-out, and
  // no taste-based ranking to explain — the flow says that plainly instead of implying a search that
  // never ran. Detected from the only source being `cold_start`.
  const isCold = lib.sources.some((s) => s.source === "cold_start");
  // Seeds are now pure-recency: the distinct titles someone watched most recently, newest first —
  // which is exactly what the old "what they watched" panel showed. So the two panels were identical
  // and are merged into one. Seeds are the richer object (they carry recency + drive the search), so
  // they lead; we fall back to the raw recent-watch sample only when nothing resolved to a seed.
  const recentBody = (
    <>
      {lib.seeds.length > 0 ? (
        <SeedList seeds={lib.seeds} userId={userId} />
      ) : lib.watched.length > 0 ? (
        <WatchList watched={lib.watched} />
      ) : (
        <Muted>
          {isCold
            ? "Too little watch history here to search from — so we fell back to what's popular on the server (below)."
            : "No recent watches recorded here — seeds may come from a shared media type."}
        </Muted>
      )}
      {/* A watch that is silently ABSENT from the seed list above is the hardest thing to explain
          about a run. When their own rating is the reason, say so here rather than leaving a gap. */}
      <RatedOutList watched={lib.watched} ratings={ratings} />
    </>
  );
  // Steps are numbered by position so the ranking step can be omitted for cold start without leaving a
  // gap in the sequence.
  const defs: Omit<FlowStepDef, "n">[] = [
    {
      id: `${lib.key}-watched`,
      icon: History,
      rail: "Watched recently",
      count: lib.seeds.length || totalWatched || lib.watched.length,
      title: `What they watched recently in ${lib.label}`,
      subtitle: isCold
        ? `${watchedSummary(lib) || "Not enough watched here yet"} — too little to recommend from, so this is a cold start.`
        : totalWatched > 0
          ? `${watchedSummary(lib)}. Their most recent are below — what someone reached for lately is the best signal of what to recommend tonight, so each becomes a search seed for the step below.`
          : "Their most recent watches, newest first — each becomes a search seed for the step below.",
      body: recentBody,
    },
    {
      id: `${lib.key}-searched`,
      icon: Search,
      rail: isCold ? "Popular titles" : "Searched",
      count: isCold ? deliveredCount : searchesRun(lib) || placesSearched,
      title: isCold
        ? `What we pulled for ${lib.label}`
        : "Where we searched, and every title in and out",
      subtitle: isCold
        ? "With too little history to search from, we pulled the highest-rated titles on this server."
        : lib.sharedSearch
          ? `Each title above fans out to every place we look for ${searchNoun}s. We search by taste, not by library, so these results are shared across your ${searchNoun} libraries — each title shows whether it made this library's shortlist or why it fell out.`
          : `Each title above fans out to every place we look. Below is each source, the exact queries we sent, and what came back — with whether each title made the shortlist or the reason it didn't.${
              searchesSampled(lib)
                ? " The per-search detail below is a sample; the count above is every search that ran."
                : ""
            }`,
      body: (
        <SourcesFlow
          sources={lib.sources}
          web={lib.web}
          webSource={lib.webSource}
          discoverGenres={lib.discoverGenres}
        />
      ),
    },
    // What SURVIVED, and what the release-date weight did to it. Between search and order because
    // that is where it happens: filtering and the pool cut decide what can be ordered at all.
    ...(isCold || selection.length === 0
      ? []
      : [
          {
            id: `${lib.key}-shortlisted`,
            icon: Filter,
            rail: "Shortlisted",
            count: selection[0]?.candidates,
            title: "What survived, and what release date did to it",
            subtitle:
              "Everything found above is filtered (already watched, wrong library, excluded genres) and then cut to the strongest few per media type. Release date is part of that cut, not applied after it.",
            body: shortlistBody(selection),
          },
        ]),
    // How the shortlist was ORDERED — the step that used to be missing entirely. Not shown for cold
    // start (no taste ranking runs; the picks are just the top-rated titles, in rating order).
    ...(isCold
      ? []
      : [
          {
            id: `${lib.key}-ranked`,
            icon: ListOrdered,
            rail: "Ordered",
            title: "How we ordered the shortlist",
            subtitle:
              "Everything that made the shortlist above is scored and ordered in plain code — no AI decides the order. Here's exactly how.",
            body: <RankingExplainer lib={lib} />,
          },
        ]),
    {
      id: `${lib.key}-delivered`,
      icon: ArrowRight,
      rail: "Delivered",
      count: deliveredCount,
      title: `What we put in ${lib.label}, and why`,
      body:
        lib.delivered.length > 0 ? (
          <>
            {deliveryNote(selection)}
            <DeliveredList delivered={lib.delivered} />
          </>
        ) : (
          <Muted>Nothing was delivered to this library this run.</Muted>
        ),
    },
  ];
  const steps: FlowStepDef[] = defs.map((def, i) => ({ ...def, n: i + 1 }));

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

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>;
}

// ── Stage 1: recent watches, newest first (the seeds we search from) ───────────

/** The recent watches we search from — newest first, each tagged with how long ago. Seed weight is
 *  now pure recency (frequency no longer scores), so there's no "influence" to rank: this is just the
 *  list, in recency order. A play-count bar or "watched N×" here would imply a weighting we no longer
 *  apply. Seeds arrive already sorted newest-first, so their order IS the recency order. */
/** The per-seed block action.
 *
 *  Its own component so the mutation hook only mounts when a user id is actually available — the
 *  trace renders standalone in places that have no QueryClient, and a hook cannot be called
 *  conditionally. */
function BlockSeedButton({
  seed,
  userId,
}: {
  seed: TraceSeed;
  userId: number;
}) {
  const block = useBlockSeed(userId);
  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-6 px-1.5 text-xs text-muted-foreground opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100"
      disabled={block.isPending}
      title={`Stop "${seed.title}" shaping this person's picks. It stays in their history — it just stops being a seed.`}
      onClick={() =>
        block.mutate({
          tmdbId: seed.tmdb_id,
          title: seed.title,
          mediaType: seed.media === "show" ? "show" : "movie",
        })
      }
    >
      <Ban className="h-3 w-3" aria-hidden />
      Don&rsquo;t seed
    </Button>
  );
}

function SeedList({ seeds, userId }: { seeds: TraceSeed[]; userId?: number }) {
  return (
    <ol className="space-y-1.5">
      {seeds.map((s) => (
        <li
          key={`${s.media}-${s.tmdb_id}`}
          className="group flex items-baseline justify-between gap-3"
        >
          <span className="truncate text-sm font-medium">{s.title}</span>
          <span className="flex shrink-0 items-baseline gap-2">
            {seedWhy(s) && (
              <span className="text-xs text-muted-foreground">
                {seedWhy(s)}
              </span>
            )}
            {/* This is where a bad seed is actually noticed — the page that says "these are the
                watches your picks came from". Blocking anywhere else means remembering a title and
                going to find it. */}
            {userId !== undefined && (
              <BlockSeedButton seed={s} userId={userId} />
            )}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** Plex's 0..10 scale as the stars the person actually clicked. */
function stars(rating: number): string {
  return `${rating / 2}★`;
}

/** What their Plex ratings did to this run, in one sentence.
 *
 *  Always says something when the run recorded a policy, because the three ways ratings can do
 *  nothing — off, nothing rated low, an account whose ratings are all tool-written — are invisible in
 *  the outcome and only one of them means the feature is working. The distrusted case is the one this
 *  exists for: it is a silent no-op that otherwise reads exactly like a healthy run.
 */
export function ratingsSummary(ratings: TraceRatings): string {
  if (!ratings.enabled)
    return "Plex ratings are off for this run, so nothing they rated changed these picks.";
  if (!ratings.trusted)
    return "Plex ratings weren’t used: another tool is writing ratings on this account, so none of them count as opinions.";
  const line = stars(ratings.threshold ?? 0);
  if (ratings.rated === 0)
    return "Plex ratings are on, but they haven’t rated anything — nothing was dropped.";
  if (ratings.rated_human === 0)
    return `Plex ratings are on, but none of their ${ratings.rated} ratings were typed in Plex — they carry decimals, so a tool wrote them and none were counted.`;
  // A partly tool-written account stays "trusted" (the account-level check tolerates a fifth of them),
  // so the skipped values have to be owned up to rather than folded into a clean-sounding total.
  const uncounted = ratings.rated - ratings.rated_human;
  const skipped =
    uncounted === 1
      ? " One more rating looks tool-written and wasn’t counted."
      : uncounted > 1
        ? ` ${uncounted} more ratings look tool-written and weren’t counted.`
        : "";
  // Both counts are account-wide, while the badge list under this line is only what's visible in ONE
  // library's recent sample. The sentence names its own scope so the two never read as one number
  // disagreeing with itself — a movie rated out years ago is counted here and shown nowhere.
  if (ratings.blocked === 0)
    return `Plex ratings are on. Across everything they’ve watched, none of their ${ratings.rated_human} ratings are ${line} or lower, so nothing was dropped.${skipped}`;
  // "can't be used as seeds", not "stopped being seeds": most of these were never in the running.
  // Seeds come from the most recent watches, so a title rated low years ago is excluded from a set it
  // would never have reached — claiming it was removed would overstate what the run did.
  const titles = ratings.blocked === 1 ? "title" : "titles";
  return `Plex ratings are on. Across everything they’ve watched, ${ratings.blocked} ${titles} they rated ${line} or lower can’t be used as seeds.${skipped}`;
}

/** The rating policy this run used, plus the watches it actually kept out of the seed list.
 *
 *  `ratings` is absent on runs recorded before the policy was traced; those still show the badge list
 *  alone, exactly as they always did, rather than inventing a policy they never recorded.
 */
function RatedOutList({
  watched,
  ratings,
}: {
  watched: TraceWatch[];
  ratings?: TraceRatings;
}) {
  const ratedOut = watched.filter((w) => w.rating_blocked);
  if (!ratings && ratedOut.length === 0) return null;
  return (
    <div className="mt-3 space-y-1.5 border-t pt-3">
      {ratings && (
        <p className="text-xs text-muted-foreground">
          {ratingsSummary(ratings)}
        </p>
      )}
      {ratedOut.length > 0 && (
        <>
          {/* The sentence above counts their WHOLE history; these are only the ones visible in this
              library's recent sample, so it says "shown here" rather than repeating the number. */}
          <p className="text-xs text-muted-foreground">
            {ratings
              ? "Dropped, among the watches shown here:"
              : "Not used as seeds — they rated these low in Plex:"}
          </p>
          <ul className="flex flex-wrap gap-1.5">
            {ratedOut.map((w, i) => (
              <li key={`${w.title}-${i}`}>
                <Badge
                  variant="secondary"
                  className="font-normal text-destructive-text"
                >
                  {w.title}
                  {w.year ? ` (${w.year})` : ""}
                  {w.rating != null ? ` · ${stars(w.rating)}` : ""}
                </Badge>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** Raw recent-watch fallback: shown only when nothing resolved to a seed (so the merged step still
 *  has content). Chips, matching the seed list's plain "these are the recent titles" read. */
function WatchList({ watched }: { watched: TraceWatch[] }) {
  return (
    <ul className="flex flex-wrap gap-1.5">
      {watched.map((w, i) => (
        <li key={`${w.title}-${i}`}>
          <Badge variant="secondary" className="font-normal">
            {w.title}
            {w.year ? ` (${w.year})` : ""}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

/** "3 days ago" from the recency ingredient — or "" on legacy runs that lack it. Frequency
 *  ("watched N×") is deliberately gone: watch count no longer scores a seed (recency alone does), so
 *  surfacing it here would imply a weighting we don't apply. */
function seedWhy(s: TraceSeed): string {
  if (s.recency_days === undefined) return "";
  return s.recency_days <= 0
    ? "watched most recently"
    : `${s.recency_days} day${s.recency_days === 1 ? "" : "s"} ago`;
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
        <div className="min-w-0 space-y-1.5">
          <p className="text-sm font-medium">{sourceLabel(src.source)}</p>
          {failed ? (
            <p className="text-xs text-destructive-text">
              Couldn’t reach it{src.detail ? ` — ${src.detail}` : ""}
            </p>
          ) : (
            <>
              {/* Say plainly what we handed this source and what it gave back — the reported
                  confusion was "what does 40 · 4 kept · 4 dropped even mean". */}
              <p className="text-xs text-muted-foreground">
                {sourceRole(src.source)} It added{" "}
                {src.contributed.toLocaleString()} title
                {src.contributed === 1 ? "" : "s"} to the pool.
              </p>
              {(kept > 0 || droppedCount > 0) && (
                <p className="flex items-center gap-2 text-xs text-muted-foreground">
                  <span className="inline-flex items-center gap-1 text-success">
                    <Check className="h-3 w-3" aria-hidden="true" />
                    {kept} made the shortlist
                  </span>
                  <span aria-hidden="true">·</span>
                  <span>{droppedCount} dropped (see below)</span>
                </p>
              )}
            </>
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
            The genres they watch most:{" "}
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
              <SeedQueryRow
                key={`${q.seed}-${i}`}
                query={q}
                source={src.source}
              />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function SeedQueryRow({
  query,
  source,
}: {
  query: TraceSeedQuery;
  source?: string;
}) {
  // tmdb_discover queries by GENRE, not by a watched title — so it reads "In your genres · Crime,
  // Comedy" rather than "Searched from <a title>". Every other source is seeded from a watch.
  const isGenre = source === "tmdb_discover";
  return (
    <li className="text-sm">
      <div className="flex items-center gap-1.5">
        <Search
          className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <span className="text-muted-foreground">
          {isGenre ? "In your genres" : "Searched from"}
        </span>
        <span className="font-medium">{query.seed}</span>
        <Badge variant="outline" className="shrink-0 font-normal">
          {mediaLabel(query.media)}
        </Badge>
      </div>
      {query.returned.length > 0 ? (
        <ReturnList
          returned={query.returned}
          total={query.total}
          media={query.media}
        />
      ) : (
        <p className="mt-0.5 pl-5 text-xs text-muted-foreground">
          nothing returned
        </p>
      )}
    </li>
  );
}

const _RETURN_PREVIEW = 6; // titles shown before "show the rest" — enough to read the mix at a glance

/** A source's returned titles: a short preview, an expander for the rest of what was recorded, and —
 *  only when the source genuinely returned more than we recorded — an honest "+N more not recorded"
 *  tail. The old dead "+N more not shown" line conflated "collapsed, click to see" with "beyond the
 *  recording cap"; this separates them so nothing that WAS recorded is hidden behind an unclickable line. */
function ReturnList({
  returned,
  total,
  media,
}: {
  returned: TraceReturn[];
  total: number;
  media: string;
}) {
  const preview = returned.slice(0, _RETURN_PREVIEW);
  const rest = returned.slice(_RETURN_PREVIEW);
  const beyondCap = Math.max(0, total - returned.length);
  return (
    <ul className="mt-1.5 space-y-1 pl-5">
      {preview.map((r, i) => (
        <ReturnRow key={`${r.tmdb_id}-${i}`} ret={r} media={media} />
      ))}
      {rest.length > 0 && (
        <li>
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
              <ChevronRight
                className="h-3 w-3 transition-transform group-open:rotate-90"
                aria-hidden="true"
              />
              Show {rest.length.toLocaleString()} more
            </summary>
            <ul className="mt-1 space-y-1">
              {rest.map((r, i) => (
                <ReturnRow key={`${r.tmdb_id}-${i}`} ret={r} media={media} />
              ))}
            </ul>
          </details>
        </li>
      )}
      {beyondCap > 0 && (
        <li className="text-xs text-muted-foreground/70">
          +{beyondCap.toLocaleString()} more returned (not recorded in the
          trace)
        </li>
      )}
    </ul>
  );
}

function ReturnRow({ ret, media }: { ret: TraceReturn; media: string }) {
  const kept = ret.fate === "kept";
  // A title we couldn't show because no library held it may still have been requested from
  // Sonarr/Radarr — overlay that outcome so the drop reads "→ requested from Radarr", not a dead end.
  const request = useRequestOutcome(ret.tmdb_id, media);
  const showRequest =
    ret.fate === "not_in_your_libraries" && request !== undefined;
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
        {/* The numbers the verdict was reached on. A fate alone says a title lost; these say why it
            lost — which is the question "it picked a 2003 film over a 2024 one" is really asking. */}
        {ret.year != null && (
          <span className="ml-1 text-muted-foreground tabular-nums">
            ({ret.year})
          </span>
        )}
      </span>
      {ret.rating != null && ret.rating > 0 && (
        <span className="shrink-0 tabular-nums text-muted-foreground/80">
          {ret.rating.toFixed(1)}
        </span>
      )}
      {/* Omitted at 1: "×1.0" is not information, it is the absence of it. */}
      {ret.age_weight != null && ret.age_weight < 1 && (
        <span
          className="shrink-0 tabular-nums text-muted-foreground/80"
          title={`Release date weighting scaled this title's score to ${Math.round(
            ret.age_weight * 100,
          )}% of an equivalent title from this year.`}
        >
          age ×{ret.age_weight.toFixed(2)}
        </span>
      )}
      {ret.fate !== undefined && !kept && (
        <span className="shrink-0 text-muted-foreground/80">
          {fateLabel(ret.fate)}
        </span>
      )}
      {showRequest && request && (
        <span className="shrink-0 text-muted-foreground/80" aria-hidden="true">
          →
        </span>
      )}
      {showRequest && request && <RequestOutcomeTag request={request} />}
    </li>
  );
}

/** The "→ requested from Radarr" tail on a not-in-your-libraries drop. Says what actually happened:
 *  sent = the request went to Sonarr/Radarr; pending = it's queued for the owner to approve;
 *  rejected = the owner dismissed it. `excluded` titles are flagged (approving is a no-op until the
 *  owner clears the arr's import-exclusion list). */
function RequestOutcomeTag({ request }: { request: TraceRequestOutcome }) {
  if (request.status === "sent") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-success">
        <Check className="h-3 w-3" aria-hidden="true" />
        requested from Sonarr/Radarr
      </span>
    );
  }
  if (request.status === "pending") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 text-muted-foreground">
        <Clock className="h-3 w-3" aria-hidden="true" />
        {request.excluded
          ? "queued — but on the arr’s exclusion list"
          : "queued for your approval"}
      </span>
    );
  }
  return (
    <span className="shrink-0 text-muted-foreground/80">
      you dismissed this request
    </span>
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
  // Each resolved proposal's fate (kept into the row, or why it fell out), keyed by the same label the
  // `proposed` list uses. Absent on legacy runs — then we fall back to the plain resolved/dropped read.
  const fateByTitle = new Map(
    (web?.proposals ?? []).map((p) => [p.title, p.fate]),
  );
  const kept =
    [...fateByTitle.values()].filter((f) => f === "kept").length ||
    (source?.disposition?.kept ?? 0);
  const mech = webMechanism(web?.mode ?? "", searches.length > 0, web?.provider);
  // Exa bills per search, so a title many users watched is searched once and reused from a shared
  // cache for the rest. Surface how many actually hit Exa vs came back cached — it's the difference
  // between a costly run and a cheap one.
  const cachedCount = searches.filter((s) => s.cached).length;
  const freshCount = searches.length - cachedCount;

  return (
    <div className="overflow-hidden rounded-lg border">
      <div className="flex items-start gap-2 p-3">
        <Globe
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1 space-y-1">
          <p className="text-sm font-medium">{sourceLabel("llm_web")}</p>
          <p className="text-xs text-muted-foreground">
            {mech}
            {source
              ? ` It added ${source.contributed.toLocaleString()} title${source.contributed === 1 ? "" : "s"} to the pool.`
              : ""}
          </p>
        </div>
        <Badge
          variant={failed ? "destructive" : "secondary"}
          className="shrink-0"
        >
          {failed ? "Failed" : proposed.length}
        </Badge>
      </div>

      {failed && source?.detail && (
        <p className="border-t px-3 py-2 text-xs text-destructive-text">
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
              Exa web searches — one per seed
            </p>
            <p className="text-xs text-muted-foreground">
              {freshCount > 0 && cachedCount > 0
                ? `${searches.length} searches — ${freshCount} new, ${cachedCount} reused from an earlier run’s cache (so only ${freshCount} were billed).`
                : cachedCount > 0
                  ? `${searches.length} searches, all reused from an earlier run’s cache — nothing billed.`
                  : `${searches.length} searches, all new this run.`}
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
                — a check made this library&rsquo;s shortlist; the rest show why
                they didn&rsquo;t
              </span>
            </p>
            <ul className="flex flex-wrap gap-1.5">
              {proposed.map((title, i) => {
                // No TMDB match at all (a likely hallucination): struck through, no fate to show.
                const hallucinated =
                  unresolved.has(title) && !resolved.has(title);
                // Resolved to a real title: its fate says whether it made the row or why it fell out.
                const fate = fateByTitle.get(title);
                const kept = fate === "kept";
                return (
                  <li key={`${title}-${i}`}>
                    <Badge
                      variant={kept ? "secondary" : "outline"}
                      className={cn(
                        "gap-1 font-normal",
                        hallucinated && "text-muted-foreground line-through",
                        !kept && !hallucinated && "text-muted-foreground",
                      )}
                    >
                      {kept && (
                        <Check
                          className="h-3 w-3 text-success"
                          aria-hidden="true"
                        />
                      )}
                      {title}
                      {!kept && !hallucinated && fate && (
                        <span className="text-muted-foreground/80">
                          · {fateLabel(fate)}
                        </span>
                      )}
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

// ── Stage 3.5: how the shortlist was ordered ──────────────────────────────────

/** Plain-English explanation of the ranking — the step that used to be missing. There is no AI in the
 *  ordering (the model is used only to FIND titles); it's `ranking.score` + two fair-share passes, so
 *  this says exactly that and grounds it in THIS library's picks: how many sources and how many
 *  different watched titles fed the row, which is what the fair-share passes actually produce. */
function RankingExplainer({ lib }: { lib: LibraryView }) {
  const picks = lib.delivered.flatMap((b) => b.picks);
  const sources = new Set<string>();
  for (const p of picks) for (const s of p.sources ?? []) sources.add(s);
  const seedTitles = new Set(
    picks.map((p) => p.seed_title).filter((s): s is string => Boolean(s)),
  );
  return (
    <div className="space-y-4 text-sm">
      <div className="space-y-2">
        <p className="font-medium">
          Each title gets a score from three things:
        </p>
        <ul className="space-y-1.5 text-muted-foreground">
          <li className="flex gap-2">
            <span className="text-foreground">·</span>
            <span>
              <span className="font-medium text-foreground">
                How recently you watched what suggested it.
              </span>{" "}
              A title suggested by something you watched last week outranks one
              from a watch a year ago.
            </span>
          </li>
          <li className="flex gap-2">
            <span className="text-foreground">·</span>
            <span>
              <span className="font-medium text-foreground">
                How closely it matches.
              </span>{" "}
              How near the top of the suggesting source&rsquo;s list it sat — a
              close match beats a loose one.
            </span>
          </li>
          <li className="flex gap-2">
            <span className="text-foreground">·</span>
            <span>
              <span className="font-medium text-foreground">Its rating.</span> A
              well-reviewed title edges out a poorly-reviewed one when
              everything else is equal.
            </span>
          </li>
        </ul>
      </div>
      <div className="space-y-1.5">
        <p className="font-medium">
          Then two fairness passes reshape the order:
        </p>
        <p className="text-muted-foreground">
          Best-scoring title always leads. After that, each{" "}
          <span className="font-medium text-foreground">source</span> gets a
          fair turn, then each{" "}
          <span className="font-medium text-foreground">title you watched</span>{" "}
          does — so a single heavily-watched favourite can&rsquo;t swallow the
          whole row, and the sources you paid for (like AI web search) always
          reach it.
        </p>
      </div>
      {picks.length > 0 && (sources.size > 0 || seedTitles.size > 0) && (
        <p className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
          In this row, {picks.length} pick{picks.length === 1 ? "" : "s"} came
          from{" "}
          <span className="font-medium text-foreground">
            {sources.size} source{sources.size === 1 ? "" : "s"}
          </span>
          {seedTitles.size > 0 && (
            <>
              {" "}
              and{" "}
              <span className="font-medium text-foreground">
                {seedTitles.size} different title
                {seedTitles.size === 1 ? "" : "s"} you watched
              </span>
            </>
          )}
          {" — "}spread across your tastes, not stacked on one.
        </p>
      )}
      <p className="text-xs text-muted-foreground/80">
        No AI decides this order — it&rsquo;s all plain, inspectable code.
      </p>
    </div>
  );
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
