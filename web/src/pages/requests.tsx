import { Clapperboard, Inbox, Send, Star, X } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { apiErrorMessage } from "@/lib/api";
import { useRejectRequests, useRequests, useSendRequests } from "@/lib/queries";
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

/** The facts that let the owner judge a title at a glance: type, rating, and how many people wanted it. */
function TitleMeta({ item }: { item: RequestCandidate }) {
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
      <span>
        wanted by {item.demand} {item.demand === 1 ? "person" : "people"}
      </span>
    </div>
  );
}

function PendingRow({
  item,
  checked,
  onToggle,
}: {
  item: RequestCandidate;
  checked: boolean;
  onToggle: (id: number) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50">
      <input
        type="checkbox"
        checked={checked}
        onChange={() => onToggle(item.id)}
        className="mt-1 h-4 w-4 shrink-0 accent-primary"
      />
      <div className="min-w-0 space-y-1">
        <p className="font-medium">{item.title}</p>
        <TitleMeta item={item} />
        {item.detail ? (
          <p className="text-xs text-muted-foreground">
            Last attempt: {item.detail}
          </p>
        ) : null}
      </div>
    </label>
  );
}

function HandledRow({ item }: { item: RequestCandidate }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-dashed px-3 py-2 text-sm">
      <div className="min-w-0">
        <span className="font-medium">{item.title}</span>{" "}
        <span className="text-muted-foreground">
          {item.year ? `· ${item.year} ` : ""}· wanted by {item.demand}
        </span>
      </div>
      <Badge variant={item.status === "sent" ? "success" : "secondary"}>
        {item.status === "sent" ? "sent to Sonarr/Radarr" : "dismissed"}
      </Badge>
    </div>
  );
}

export function RequestsPage() {
  const requestsQuery = useRequests();
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

  const rows = requestsQuery.data ?? [];
  const pending = useMemo(
    () => rows.filter((r) => r.status === "pending"),
    [rows],
  );
  const handled = useMemo(
    () => rows.filter((r) => r.status !== "pending"),
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

      <QueryBoundary
        query={requestsQuery}
        skeleton={<RequestsSkeleton />}
        isEmpty={(data) => data.length === 0}
        empty={
          <EmptyState
            title="Nothing waiting"
            hint="When a run turns up a great pick that isn't in your library, it lands here for your approval. Strong picks are sent automatically (tune that in Settings → Requests)."
          />
        }
      >
        {() => (
          <div className="space-y-8">
            {pending.length > 0 ? (
              <section className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
                    <input
                      type="checkbox"
                      checked={allChecked}
                      onChange={toggleAll}
                      className="h-4 w-4 accent-primary"
                    />
                    {selectedPending.length > 0
                      ? `${selectedPending.length} selected`
                      : `${pending.length} waiting`}
                  </label>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={selectedPending.length === 0 || busy}
                      onClick={() => act(() => reject.mutate(selectedPending))}
                    >
                      <X aria-hidden="true" />
                      Reject
                    </Button>
                    <Button
                      size="sm"
                      loading={send.isPending}
                      disabled={selectedPending.length === 0 || busy}
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

            {handled.length > 0 && (
              <section className="space-y-3">
                <h2 className="text-sm font-medium text-muted-foreground">
                  Already handled
                </h2>
                <div className="space-y-2">
                  {handled.map((item) => (
                    <HandledRow key={item.id} item={item} />
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
