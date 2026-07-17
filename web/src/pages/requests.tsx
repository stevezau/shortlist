import { Clapperboard, ExternalLink, Inbox, Send, Star, X } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Link } from "react-router-dom";

import { apiErrorMessage } from "@/lib/api";
import { formatDate, settingBool, settingString } from "@/lib/format";
import {
  useRejectRequests,
  useRequests,
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

/** Quick look-it-up links: TMDB and Trakt jump straight to the title by its TMDB id; IMDb is a
 *  title search (Shortlist doesn't store an IMDb id). All open in a new tab. */
function ExternalLinks({ item }: { item: RequestCandidate }) {
  const tmdbPath = item.media_type === "movie" ? "movie" : "tv";
  const traktType = item.media_type === "movie" ? "movie" : "show";
  const links = [
    {
      label: "TMDB",
      href: `https://www.themoviedb.org/${tmdbPath}/${item.tmdb_id}`,
    },
    {
      label: "IMDb",
      // Deep-link straight to the title when we resolved its id; otherwise fall back to a search.
      href: item.imdb_id
        ? `https://www.imdb.com/title/${item.imdb_id}/`
        : `https://www.imdb.com/find/?q=${encodeURIComponent(item.title)}&s=tt`,
    },
    {
      label: "Trakt",
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
          className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground hover:underline focus-visible:text-foreground"
        >
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
  if (!why || why.length === 0) return null;
  return (
    <ul className="space-y-0.5 border-l-2 border-muted pl-3 text-xs text-muted-foreground">
      {why.map((w, i) => (
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
        {item.detail ? (
          <p className="text-xs text-muted-foreground">
            Last attempt: {item.detail}
          </p>
        ) : null}
      </div>
    </label>
  );
}

/** The send log: a title that went to Sonarr/Radarr — when it went, the app's answer, and why it
 *  was wanted in the first place. */
function SentRow({ item }: { item: RequestCandidate }) {
  return (
    <div className="space-y-1.5 rounded-lg border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-medium">{item.title}</p>
        <Badge variant="success">Sent to Sonarr/Radarr</Badge>
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
      <ExternalLinks item={item} />
    </div>
  );
}

/** A dismissed title — kept on file so a later run never re-queues it. */
function DismissedRow({ item }: { item: RequestCandidate }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed px-3 py-2 text-sm">
      <div className="min-w-0">
        <span className="font-medium">{item.title}</span>{" "}
        <span className="text-muted-foreground">
          {item.year ? `· ${item.year} ` : ""}· wanted by {item.demand}
        </span>
      </div>
      <Badge variant="secondary">dismissed</Badge>
    </div>
  );
}

export function RequestsPage() {
  const requestsQuery = useRequests();
  const settingsQuery = useSettings();
  const send = useSendRequests();
  const reject = useRejectRequests();
  const [selected, setSelected] = useState<Set<number>>(new Set());

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
  const dismissed = useMemo(
    () => rows.filter((r) => r.status === "rejected"),
    [rows],
  );

  // Only pending rows are selectable, so an id lingering in the set after a send/reject is harmless,
  // but clearing keeps the count honest.
  const selectedPending = pending
    .filter((r) => selected.has(r.id))
    .map((r) => r.id);
  const allChecked =
    pending.length > 0 && selectedPending.length === pending.length;
  const busy = send.isPending || reject.isPending;

  const toggleAll = () =>
    setSelected(allChecked ? new Set() : new Set(pending.map((r) => r.id)));

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
              {() => (
                <div className="space-y-8">
                  {!requestsEnabled && <RequestsOffBanner />}

                  {pending.length > 0 ? (
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
                            : `${pending.length} waiting`}
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
                              act(() => reject.mutate(selectedPending))
                            }
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
                              act(() => send.mutate({ ids: selectedPending }))
                            }
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

                      {(send.isError || reject.isError) && (
                        <p role="alert" className="text-sm text-destructive">
                          {apiErrorMessage(
                            send.error ?? reject.error,
                            "That didn't go through. Check the server log and try again.",
                          )}
                        </p>
                      )}

                      <div className="space-y-2">
                        {pending.map((item) => (
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
                  )}

                  {sent.length > 0 && (
                    <section className="space-y-3">
                      <h2 className="text-sm font-medium text-muted-foreground">
                        Sent to Sonarr/Radarr
                      </h2>
                      <div className="space-y-2">
                        {sent.map((item) => (
                          <SentRow key={item.id} item={item} />
                        ))}
                      </div>
                    </section>
                  )}

                  {dismissed.length > 0 && (
                    <section className="space-y-3">
                      <h2 className="text-sm font-medium text-muted-foreground">
                        Dismissed
                      </h2>
                      <div className="space-y-2">
                        {dismissed.map((item) => (
                          <DismissedRow key={item.id} item={item} />
                        ))}
                      </div>
                    </section>
                  )}
                </div>
              )}
            </QueryBoundary>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
