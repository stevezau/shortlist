import {
  Clapperboard,
  ExternalLink,
  Inbox,
  RotateCcw,
  Send,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";

import {
  ImdbGlyph,
  RadarrGlyph,
  SonarrGlyph,
  TmdbGlyph,
  TraktGlyph,
} from "@/components/brand-glyphs";
import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Link, useSearchParams } from "react-router-dom";

import { apiErrorMessage } from "@/lib/api";
import { formatDate, settingBool, settingString } from "@/lib/format";
import {
  useClearRequests,
  useDeleteRequests,
  useRejectRequests,
  useRequests,
  useRestoreRequests,
  useSendRequests,
  useSettings,
} from "@/lib/queries";
import { sourceShortLabel } from "@/lib/sources";
import type { RequestCandidate } from "@/lib/types";

function RequestsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 5 }, (_, i) => (
        <Skeleton key={i} className="h-16 w-full" />
      ))}
    </div>
  );
}

function TypeBadge({
  mediaType,
}: {
  mediaType: RequestCandidate["media_type"];
}) {
  return (
    <Badge variant="outline" className="gap-1">
      <Clapperboard className="h-3 w-3" aria-hidden="true" />
      {mediaType === "movie" ? "Movie" : "Show"}
    </Badge>
  );
}

/** "Wanted by …" — the actual names when a run recorded them, up to three then "+N more"; falls
 *  back to the bare count for rows queued before who-wanted-it was tracked. */
function wantedByLabel(item: RequestCandidate): string {
  const names = item.wanters ?? [];
  if (names.length === 0) {
    return `wanted by ${item.demand} ${item.demand === 1 ? "person" : "people"}`;
  }
  if (names.length <= 3) return `Wanted by ${names.join(", ")}`;
  return `Wanted by ${names.slice(0, 3).join(", ")} +${names.length - 3} more`;
}

type QuickLink = {
  label: string;
  icon: ReactNode;
  href: string;
  strong?: boolean;
};

/** Quick look-it-up links: TMDB and Trakt jump straight to the title by its TMDB id; IMDb is a
 *  title search (Shortlist doesn't store an IMDb id). `lead` prepends extra links (e.g. the sent
 *  log's "Open in Sonarr/Radarr") so they sit in the same row. All open in a new tab. */
function ExternalLinks({
  item,
  lead = [],
}: {
  item: RequestCandidate;
  lead?: QuickLink[];
}) {
  const tmdbPath = item.media_type === "movie" ? "movie" : "tv";
  const traktType = item.media_type === "movie" ? "movie" : "show";
  const links: QuickLink[] = [
    ...lead,
    {
      label: "TMDB",
      icon: <TmdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
      href: `https://www.themoviedb.org/${tmdbPath}/${item.tmdb_id}`,
    },
    {
      label: "IMDb",
      icon: <ImdbGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
      // Deep-link straight to the title when we resolved its id; otherwise fall back to a search.
      href: item.imdb_id
        ? `https://www.imdb.com/title/${item.imdb_id}/`
        : `https://www.imdb.com/find/?q=${encodeURIComponent(item.title)}&s=tt`,
    },
    {
      label: "Trakt",
      icon: <TraktGlyph className="h-3.5 w-3.5" />,
      href: `https://trakt.tv/search/tmdb/${item.tmdb_id}?id_type=${traktType}`,
    },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      {links.map((link) => (
        <a
          key={link.label}
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className={
            link.strong
              ? "inline-flex items-center gap-1 font-medium text-foreground hover:underline focus-visible:underline"
              : "inline-flex items-center gap-1 text-muted-foreground hover:text-foreground hover:underline focus-visible:text-foreground"
          }
        >
          {link.icon}
          {link.label}
          <ExternalLink className="h-3 w-3" aria-hidden="true" />
        </a>
      ))}
    </div>
  );
}

/**
 * The provenance behind a request: one line per (person, row) that wanted it, with the reason —
 * the seed ("because they watched …") or, for a seedless source, how it was suggested. This is the
 * answer to "where did this come from and why", not just a count.
 */
function WhyBreakdown({ why }: { why: RequestCandidate["why"] }) {
  const [expanded, setExpanded] = useState(false);
  if (!why || why.length === 0) return null;
  // A popular title can have dozens of wanters — showing every reason is a wall. Show a few, then
  // let the owner expand the rest on demand.
  const LIMIT = 3;
  const shown = expanded ? why : why.slice(0, LIMIT);
  const hidden = why.length - shown.length;
  return (
    <ul className="space-y-0.5 border-l-2 border-muted pl-3 text-xs text-muted-foreground">
      {shown.map((w, i) => (
        <li key={`${w.user}-${w.row}-${i}`}>
          <span className="font-medium text-foreground/80">{w.user}</span> ·{" "}
          <span>{w.row}</span>
          {w.seed ? (
            <span> · because they watched {w.seed}</span>
          ) : w.source ? (
            <span> · via {sourceShortLabel(w.source)}</span>
          ) : null}
        </li>
      ))}
      {why.length > LIMIT && (
        <li>
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className="text-primary underline-offset-4 hover:underline"
          >
            {expanded
              ? "Show fewer"
              : `+${hidden} more ${hidden === 1 ? "reason" : "reasons"}`}
          </button>
        </li>
      )}
    </ul>
  );
}

/** The facts that let the owner judge a title at a glance: type, rating, and who wanted it. */
function TitleMeta({
  item,
  globalTag,
}: {
  item: RequestCandidate;
  globalTag: string;
}) {
  // The global tag is applied at send time and never stored on the candidate, so add it here to
  // show the full set of tags this title will actually get (deduped against the per-user/row tags).
  const tags = [...new Set([...(globalTag ? [globalTag] : []), ...item.tags])];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
      <TypeBadge mediaType={item.media_type} />
      {item.year ? <span>{item.year}</span> : null}
      <span className="inline-flex items-center gap-1">
        <Star
          className="h-3.5 w-3.5 fill-current text-amber-500"
          aria-hidden="true"
        />
        {item.rating.toFixed(1)}
      </span>
      <span title={(item.wanters ?? []).join(", ") || undefined}>
        {wantedByLabel(item)}
      </span>
      {tags.map((tag) => (
        <Badge key={tag} variant="secondary" className="font-normal">
          {tag}
        </Badge>
      ))}
    </div>
  );
}

/**
 * Requests are off, but titles queued before that are still on file. The inbox stays readable —
 * hiding it would lose them — but nothing here can be acted on, and it has to say so: the live
 * "Send to Sonarr/Radarr" button used to render exactly as it does when the feature is on.
 */
function RequestsOffBanner() {
  return (
    <div className="space-y-2 rounded-lg border border-dashed bg-muted/30 p-4">
      <p className="text-sm font-medium">Requests are off</p>
      <p className="text-sm text-muted-foreground">
        These titles were found before you turned requests off. Shortlist
        isn&rsquo;t asking Sonarr or Radarr for anything, and nothing here can
        be sent or rejected until you turn requests back on.
      </p>
      <Button asChild variant="outline" size="sm">
        <Link to="/settings">Enable in Settings</Link>
      </Button>
    </div>
  );
}

function PendingRow({
  item,
  checked,
  onToggle,
  globalTag,
  disabled,
}: {
  item: RequestCandidate;
  checked: boolean;
  onToggle: (id: number) => void;
  globalTag: string;
  /** Requests are off — the row is still readable, but it cannot be selected for sending. */
  disabled: boolean;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={() => onToggle(item.id)}
        className="mt-1 h-4 w-4 shrink-0 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
      />
      <div className="min-w-0 space-y-1.5">
        <p className="font-medium">{item.title}</p>
        <TitleMeta item={item} globalTag={globalTag} />
        <WhyBreakdown why={item.why} />
        <ExternalLinks item={item} />
        {item.excluded ? (
          <p className="text-xs text-warning">
            On {item.media_type === "movie" ? "Radarr" : "Sonarr"}’s exclusion
            list (from a past delete) — remove it there first, or approving
            won’t add it.
          </p>
        ) : null}
        {item.detail ? (
          <p className="text-xs text-muted-foreground">
            Last attempt: {item.detail}
          </p>
        ) : null}
      </div>
    </label>
  );
}

/** The send log: a title that went to Sonarr or Radarr — which app, when it went, the app's answer,
 *  a link straight into that app, and why it was wanted. */
function SentRow({
  item,
  radarrUrl,
  sonarrUrl,
  onClear,
  clearing,
}: {
  item: RequestCandidate;
  radarrUrl: string;
  sonarrUrl: string;
  onClear: (id: number) => void;
  clearing: boolean;
}) {
  const isMovie = item.media_type === "movie";
  const app = isMovie ? "Radarr" : "Sonarr";
  const ArrGlyph = isMovie ? RadarrGlyph : SonarrGlyph;
  const base = (isMovie ? radarrUrl : sonarrUrl).replace(/\/+$/, "");
  // Deep-link straight to the title's arr page. Radarr accepts its TMDB id; Sonarr has NO id URL —
  // only /series/<titleSlug> — so it needs the slug captured at send time. Without a slug (a title
  // sent before we recorded it) fall back to the app's home page rather than a dead link.
  const arrPath = isMovie
    ? `movie/${item.arr_slug ?? item.tmdb_id}`
    : item.arr_slug
      ? `series/${item.arr_slug}`
      : "";
  const arrLink = base ? `${base}/${arrPath}` : "";
  const lead = arrLink
    ? [
        {
          label: `Open in ${app}`,
          icon: <ArrGlyph className="h-3.5 w-3.5 rounded-[2px]" />,
          href: arrLink,
          strong: true,
        },
      ]
    : [];
  return (
    <div className="space-y-1.5 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">{item.title}</p>
        <div className="flex items-center gap-2">
          <Badge variant="success" className="gap-1">
            <ArrGlyph className="h-3.5 w-3.5 rounded-[2px]" />
            Sent to {app}
          </Badge>
          <Button
            variant="ghost"
            size="sm"
            disabled={clearing}
            onClick={() => onClear(item.id)}
            title={`Remove from the send log. ${item.title} stays in ${app} — this only clears the entry here, and it won't be re-requested.`}
          >
            <X aria-hidden="true" />
            Clear
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <TypeBadge mediaType={item.media_type} />
        {item.year ? <span>{item.year}</span> : null}
        {item.updated_at ? (
          <span>Sent {formatDate(item.updated_at)}</span>
        ) : null}
        {item.detail ? <span>· {item.detail}</span> : null}
      </div>
      <WhyBreakdown why={item.why} />
      {/* The "Open in Sonarr/Radarr" link now sits with the TMDB/IMDb/Trakt look-ups, not up top. */}
      <ExternalLinks item={item} lead={lead} />
    </div>
  );
}

/** A rejected title — blocked from being suggested or requested. "Allow again" un-rejects it,
 *  moving it straight back to Waiting (metadata intact) so it can be sent. */
function RejectedRow({
  item,
  onAllowAgain,
  disabled,
}: {
  item: RequestCandidate;
  onAllowAgain: (id: number) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed px-3 py-2 text-sm">
      <div className="min-w-0">
        <span className="font-medium">{item.title}</span>{" "}
        <span className="text-muted-foreground">
          {item.year ? `· ${item.year} ` : ""}· wanted by {item.demand}
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant="secondary">rejected</Badge>
        <Button
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => onAllowAgain(item.id)}
          title="Move this back to Waiting so you can send it."
        >
          <RotateCcw aria-hidden="true" />
          Allow again
        </Button>
      </div>
    </div>
  );
}

/** Which slice of the inbox is on screen: the actionable queue, the send log, or rejected titles. */
type RequestView = "waiting" | "sent" | "rejected";

/** A missing title is exactly one media type, so the list can be split by the library it'd land in. */
type MediaFilter = "all" | "movie" | "show";

/** How the on-screen list is ordered: newest activity, best rated, or most-wanted first. */
type RequestSort = "recent" | "rating" | "demand";

const SORT_OPTIONS: { value: RequestSort; label: string }[] = [
  { value: "recent", label: "Recent" },
  { value: "rating", label: "Top rated" },
  { value: "demand", label: "Most wanted" },
];

/** A rating floor to hide weaker titles. Every queued title already cleared the request min-rating
 *  gate, so the useful thresholds sit above it — these narrow a crowded inbox to the strongest.
 *  Values are strings because the shared Segmented control keys on string values. */
const RATING_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any rating" },
  { value: "7", label: "7+" },
  { value: "8", label: "8+" },
  { value: "9", label: "9+" },
];

/** A vote-count floor — a high rating on a handful of votes is noise; this keeps only well-attested
 *  titles. Values are strings for the shared Segmented control. */
const VOTES_OPTIONS: { value: string; label: string }[] = [
  { value: "0", label: "Any votes" },
  { value: "100", label: "100+" },
  { value: "500", label: "500+" },
  { value: "1000", label: "1k+" },
];

/** Order a list by the chosen sort. `recent` = newest state change first, falling back to queue order
 *  (id) for items that were queued but never sent, so a sent log reads newest-first and a waiting
 *  queue keeps its arrival order. */
function sortRequests(
  list: RequestCandidate[],
  sort: RequestSort,
): RequestCandidate[] {
  const copy = [...list];
  if (sort === "rating") {
    copy.sort((a, b) => b.rating - a.rating || b.demand - a.demand);
  } else if (sort === "demand") {
    copy.sort((a, b) => b.demand - a.demand || b.rating - a.rating);
  } else {
    copy.sort((a, b) => {
      const ta = a.updated_at ? Date.parse(a.updated_at) : 0;
      const tb = b.updated_at ? Date.parse(b.updated_at) : 0;
      return tb - ta || b.id - a.id;
    });
  }
  return copy;
}

export function RequestsPage() {
  const requestsQuery = useRequests();
  const settingsQuery = useSettings();
  const send = useSendRequests();
  const reject = useRejectRequests();
  const del = useDeleteRequests();
  const restore = useRestoreRequests();
  const clear = useClearRequests();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Opens on Waiting, but a `?tab=sent` deep-link (e.g. the dashboard's "View the full send log")
  // lands straight on that view. `?tab=dismissed` is an accepted alias for the renamed Rejected tab.
  const [searchParams] = useSearchParams();
  const initialTab = searchParams.get("tab");
  const [view, setView] = useState<RequestView>(
    initialTab === "sent"
      ? "sent"
      : initialTab === "rejected" || initialTab === "dismissed"
        ? "rejected"
        : "waiting",
  );
  const [media, setMedia] = useState<MediaFilter>("all");
  const [sort, setSort] = useState<RequestSort>("recent");
  const [minRating, setMinRating] = useState("0");
  const [minVotes, setMinVotes] = useState("0");

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // `?? []` inline would be a fresh array every render, so both memos below would recompute on
  // every render (and eslint says so).
  const rows = useMemo(() => requestsQuery.data ?? [], [requestsQuery.data]);
  const pending = useMemo(
    () => rows.filter((r) => r.status === "pending"),
    [rows],
  );
  const sent = useMemo(() => rows.filter((r) => r.status === "sent"), [rows]);
  const rejected = useMemo(
    () => rows.filter((r) => r.status === "rejected"),
    [rows],
  );

  // The media filter (Movies/Shows) narrows whichever list is on screen — and select-all/counts
  // follow what's visible, not the whole queue. It only applies to a list that actually mixes both
  // types: once a list is single-type its filter control is hidden, so a stale "Shows" (e.g. after
  // the shows were all sent) must fall back to "all" rather than strand the remaining movies.
  const applyMedia = <T extends { media_type: string }>(list: T[]): T[] => {
    if (media === "all") return list;
    const mixed =
      list.some((r) => r.media_type === "movie") &&
      list.some((r) => r.media_type === "show");
    return mixed ? list.filter((r) => r.media_type === media) : list;
  };
  // The rating and vote floors hide weaker titles; like the media filter they narrow what's on
  // screen (and so what select-all/counts act on).
  const ratingFloor = Number(minRating) || 0;
  const votesFloor = Number(minVotes) || 0;
  const applyThresholds = <T extends { rating: number; vote_count: number }>(
    list: T[],
  ): T[] =>
    list.filter((r) => r.rating >= ratingFloor && r.vote_count >= votesFloor);
  const pendingShown = sortRequests(applyThresholds(applyMedia(pending)), sort);
  const sentShown = sortRequests(applyThresholds(applyMedia(sent)), sort);
  const rejectedShown = sortRequests(
    applyThresholds(applyMedia(rejected)),
    sort,
  );

  // Only visible pending rows are selectable, so an id lingering in the set after a send/reject or a
  // filter change is harmless, but scoping to what's shown keeps the count honest.
  const selectedPending = pendingShown
    .filter((r) => selected.has(r.id))
    .map((r) => r.id);
  const allChecked =
    pendingShown.length > 0 && selectedPending.length === pendingShown.length;
  const busy =
    send.isPending ||
    reject.isPending ||
    del.isPending ||
    restore.isPending ||
    clear.isPending;

  const toggleAll = () =>
    setSelected(
      allChecked ? new Set() : new Set(pendingShown.map((r) => r.id)),
    );

  const act = (mutate: () => void) => {
    mutate();
    setSelected(new Set());
  };

  return (
    <div>
      <PageHeader
        icon={Inbox}
        title="Requests"
        subtitle="Titles your people wanted that aren't in the library yet. Send the ones you want to Sonarr/Radarr."
      />

      {/* Whether requests are ON is a fact about the SETTING, never about whether the inbox happens
          to be empty — with the feature off and stale candidates on file, this page used to render
          the full inbox with a live Send button. Settings gets its own boundary so a cold load
          shows a skeleton rather than flashing "Requests are off" before the answer arrives. */}
      <QueryBoundary query={settingsQuery} skeleton={<RequestsSkeleton />}>
        {(settings) => {
          const requestsEnabled = settingBool(settings, "requests.enabled");
          const globalTag = settingString(settings, "requests.tag");
          const radarrUrl = settingString(settings, "requests.radarr.url");
          const sonarrUrl = settingString(settings, "requests.sonarr.url");
          return (
            <QueryBoundary
              query={requestsQuery}
              skeleton={<RequestsSkeleton />}
              isEmpty={(data) => data.length === 0}
              empty={
                requestsEnabled ? (
                  <EmptyState
                    title="Nothing waiting"
                    hint="When a run turns up a great pick that isn't in your library, it lands here for your approval. Strong picks are sent automatically (tune that in Settings → Requests)."
                  />
                ) : (
                  <EmptyState
                    title="Requests are off"
                    hint="Turn on Sonarr/Radarr requests to have Shortlist notice great picks your library is missing and offer to grab them."
                    action={
                      <Button asChild variant="outline" size="sm">
                        <Link to="/settings">Enable in Settings</Link>
                      </Button>
                    }
                  />
                )
              }
            >
              {() => {
                // Tabs, not a long stack: with a big queue the send log used to sit far below the
                // fold and read as missing. Waiting + Sent are always offered; Rejected appears
                // only once something's been rejected.
                const tabs: { value: RequestView; label: string }[] = [
                  {
                    value: "waiting",
                    label: `Waiting${pending.length ? ` (${pending.length})` : ""}`,
                  },
                  {
                    value: "sent",
                    label: `Sent${sent.length ? ` (${sent.length})` : ""}`,
                  },
                ];
                if (rejected.length > 0) {
                  tabs.push({
                    value: "rejected",
                    label: `Rejected (${rejected.length})`,
                  });
                }
                // A tab can vanish (e.g. the last dismissed item ages out) while it's selected —
                // fall back to Waiting so the view is never blank.
                const active = tabs.some((t) => t.value === view)
                  ? view
                  : "waiting";

                // The Movies/Shows split, scoped to the active tab's list — only offered when that list
                // actually mixes both types (splitting an all-movies queue helps no one).
                const activeFull =
                  active === "waiting"
                    ? pending
                    : active === "sent"
                      ? sent
                      : rejected;
                const movieCount = activeFull.filter(
                  (r) => r.media_type === "movie",
                ).length;
                const showCount = activeFull.length - movieCount;
                const showMediaFilter = movieCount > 0 && showCount > 0;

                return (
                  <div className="space-y-6">
                    {!requestsEnabled && <RequestsOffBanner />}

                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <Segmented
                        value={active}
                        options={tabs}
                        // Switching tabs clears the media filter so a stale "Movies" can't hide a
                        // shows-only list with no visible control to reset it.
                        onChange={(next) => {
                          setView(next);
                          setMedia("all");
                          setMinRating("0");
                          setMinVotes("0");
                        }}
                        ariaLabel="Which requests to show"
                      />
                      <div className="flex flex-wrap items-center gap-2">
                        {showMediaFilter && (
                          <Segmented
                            value={media}
                            onChange={setMedia}
                            ariaLabel="Filter by library"
                            options={[
                              {
                                value: "all",
                                label: `All (${activeFull.length})`,
                              },
                              {
                                value: "movie",
                                label: `Movies (${movieCount})`,
                              },
                              { value: "show", label: `Shows (${showCount})` },
                            ]}
                          />
                        )}
                        {activeFull.length > 1 && (
                          <Segmented
                            value={sort}
                            onChange={setSort}
                            ariaLabel="Sort requests"
                            options={SORT_OPTIONS}
                          />
                        )}
                        {activeFull.length > 1 && (
                          <Segmented
                            value={minRating}
                            onChange={setMinRating}
                            ariaLabel="Minimum rating"
                            options={RATING_OPTIONS}
                          />
                        )}
                        {activeFull.length > 1 && (
                          <Segmented
                            value={minVotes}
                            onChange={setMinVotes}
                            ariaLabel="Minimum vote count"
                            options={VOTES_OPTIONS}
                          />
                        )}
                      </div>
                    </div>

                    {active === "waiting" &&
                      (pending.length > 0 ? (
                        <section className="space-y-3">
                          <div className="flex flex-wrap items-center justify-between gap-3">
                            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                              <input
                                type="checkbox"
                                checked={allChecked}
                                disabled={!requestsEnabled}
                                onChange={toggleAll}
                                className="h-4 w-4 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
                              />
                              {selectedPending.length > 0
                                ? `${selectedPending.length} selected`
                                : `${pendingShown.length} waiting`}
                            </label>
                            <div className="flex items-center gap-2">
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() => del.mutate(selectedPending))
                                }
                                title="Remove from the list for now. If a later run's picks still want it, it comes back."
                              >
                                <Trash2 aria-hidden="true" />
                                Delete
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() => reject.mutate(selectedPending))
                                }
                                title="Never suggest or request these again. They won't come back."
                              >
                                <X aria-hidden="true" />
                                Reject
                              </Button>
                              <Button
                                size="sm"
                                loading={send.isPending}
                                disabled={
                                  !requestsEnabled ||
                                  selectedPending.length === 0 ||
                                  busy
                                }
                                onClick={() =>
                                  act(() =>
                                    send.mutate({ ids: selectedPending }),
                                  )
                                }
                                title="Ask Sonarr/Radarr to download the selected titles now."
                              >
                                {!send.isPending && <Send aria-hidden="true" />}
                                Send{" "}
                                {selectedPending.length > 0
                                  ? selectedPending.length
                                  : ""}{" "}
                                to Sonarr/Radarr
                              </Button>
                            </div>
                          </div>

                          {/* Always visible (not just on hover) so the Delete-vs-Reject difference is
                              never a guess — the two both clear the list but do opposite things next run. */}
                          <p className="text-xs text-muted-foreground">
                            <strong className="font-medium text-foreground">
                              Delete
                            </strong>{" "}
                            removes a title for now — it can return on a later
                            run if it&rsquo;s still wanted.{" "}
                            <strong className="font-medium text-foreground">
                              Reject
                            </strong>{" "}
                            blocks it for good — it won&rsquo;t come back.
                          </p>

                          {(send.isError || reject.isError || del.isError) && (
                            <p
                              role="alert"
                              className="text-sm text-destructive"
                            >
                              {apiErrorMessage(
                                send.error ?? reject.error ?? del.error,
                                "That didn't go through. Check the server log and try again.",
                              )}
                            </p>
                          )}

                          <div className="space-y-2">
                            {pendingShown.map((item) => (
                              <PendingRow
                                key={item.id}
                                item={item}
                                checked={selected.has(item.id)}
                                onToggle={toggle}
                                globalTag={globalTag}
                                disabled={!requestsEnabled}
                              />
                            ))}
                          </div>
                        </section>
                      ) : (
                        <EmptyState
                          title="Inbox clear"
                          hint="Nothing is waiting on you right now. New missing picks will show up here after the next run."
                        />
                      ))}

                    {active === "sent" && (
                      <section className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <h2 className="text-sm font-medium text-muted-foreground">
                            Sent to Radarr &amp; Sonarr
                          </h2>
                          {sentShown.length > 0 && (
                            <Button
                              variant="outline"
                              size="sm"
                              loading={clear.isPending}
                              disabled={busy}
                              onClick={() =>
                                clear.mutate(sentShown.map((r) => r.id))
                              }
                              title="Clear every entry here from the send log. The titles stay in Sonarr/Radarr and won't be re-requested."
                            >
                              {!clear.isPending && (
                                <Trash2 aria-hidden="true" />
                              )}
                              Clear all ({sentShown.length})
                            </Button>
                          )}
                        </div>
                        {clear.isError && (
                          <p role="alert" className="text-sm text-destructive">
                            {apiErrorMessage(
                              clear.error,
                              "That didn't go through. Check the server log and try again.",
                            )}
                          </p>
                        )}
                        {sentShown.length > 0 ? (
                          <div className="space-y-2">
                            {sentShown.map((item) => (
                              <SentRow
                                key={item.id}
                                item={item}
                                radarrUrl={radarrUrl}
                                sonarrUrl={sonarrUrl}
                                onClear={(id) => clear.mutate([id])}
                                clearing={clear.isPending}
                              />
                            ))}
                          </div>
                        ) : (
                          <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
                            Nothing sent yet. When a run auto-sends a strong
                            pick, or you approve one in Waiting, it&rsquo;s
                            logged here — the title, when it went, the
                            app&rsquo;s answer, and who it was for.
                          </p>
                        )}
                      </section>
                    )}

                    {active === "rejected" && rejected.length > 0 && (
                      <section className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <p className="text-sm text-muted-foreground">
                            These are blocked — no run suggests or requests
                            them.{" "}
                            <strong className="font-medium text-foreground">
                              Allow again
                            </strong>{" "}
                            moves one straight back to Waiting so you can send
                            it.
                          </p>
                          <Button
                            variant="outline"
                            size="sm"
                            loading={restore.isPending}
                            disabled={busy}
                            onClick={() =>
                              restore.mutate(rejectedShown.map((r) => r.id))
                            }
                            title="Move every rejected title here back to Waiting."
                          >
                            {!restore.isPending && (
                              <RotateCcw aria-hidden="true" />
                            )}
                            Allow all again ({rejectedShown.length})
                          </Button>
                        </div>
                        {restore.isError && (
                          <p role="alert" className="text-sm text-destructive">
                            {apiErrorMessage(
                              restore.error,
                              "That didn't go through. Check the server log and try again.",
                            )}
                          </p>
                        )}
                        <div className="space-y-2">
                          {rejectedShown.map((item) => (
                            <RejectedRow
                              key={item.id}
                              item={item}
                              onAllowAgain={(id) => restore.mutate([id])}
                              disabled={busy}
                            />
                          ))}
                        </div>
                      </section>
                    )}
                  </div>
                );
              }}
            </QueryBoundary>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
