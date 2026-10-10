import { RotateCcw, Send, Trash2, X } from "lucide-react";
import { Link } from "react-router";

import { EmptyState } from "@/components/query-boundary";
import {
  NoMatches,
  PendingRow,
  RejectedRow,
  SentRow,
  SETTINGS_LINK,
  type ArrView,
} from "@/components/requests/request-parts";
import { Button } from "@/components/ui/button";
import { apiErrorMessage } from "@/lib/api";
import { settingBool, settingString } from "@/lib/format";
import type {
  useClearRequests,
  useDeleteRequests,
  useRejectRequests,
  useRestoreRequests,
  useSendRequests} from "@/lib/queries";
import {
  useAcquisitionClaims,
  useReleaseAcquisitionClaim,
  useSettings,
} from "@/lib/queries";
import type { RequestCandidate } from "@/lib/types";
import type { DisplayNameLookup } from "@/lib/user-names";
import { cn } from "@/lib/utils";

type SendMutation = ReturnType<typeof useSendRequests>;
type RejectMutation = ReturnType<typeof useRejectRequests>;
type DeleteMutation = ReturnType<typeof useDeleteRequests>;
type RestoreMutation = ReturnType<typeof useRestoreRequests>;
type ClearMutation = ReturnType<typeof useClearRequests>;

/** The one-line "where requests go" note under the page header. */
export function SendingSummary() {
  const settings = useSettings().data;
  if (!settings) return null;
  return (
    <p className="mb-5 rounded-lg border bg-card px-4 py-2.5 text-[13px] text-muted-foreground">
      {settingBool(settings, "requests.enabled")
        ? `Sends to ${settingString(settings, "requests.target", "arr") === "overseerr" ? "Overseerr" : "Radarr (movies) and Sonarr (shows)"} · Auto-send is ${settingBool(settings, "requests.auto_send") ? "on" : "off: you approve each one"}`
        : "Sending is disabled"}
      {" · "}
      <Link className="text-foreground underline underline-offset-2" to={SETTINGS_LINK}>
        Settings &rarr;
      </Link>
    </p>
  );
}

/** Sends the server could not confirm, listed until the owner has checked the destination and allows a retry. */
export function AcquisitionClaims() {
  const claimsQuery = useAcquisitionClaims();
  const releaseClaim = useReleaseAcquisitionClaim();
  if (!claimsQuery.data || claimsQuery.data.items.length === 0) return null;
  return (
    <section className="mx-auto mb-5 max-w-6xl rounded-lg border border-warning/50 bg-warning/5 px-4 py-4 sm:px-5" aria-labelledby="acquisition-recovery-title">
      <h2 id="acquisition-recovery-title" className="font-semibold">Acquisition checks needing review</h2>
      <p className="mt-1 text-sm text-muted-foreground">Inspect the actual destination before allowing a title to be requested again. Releasing a claim does not send the title.</p>
      <div className="mt-3 divide-y rounded-md border bg-card">
        {claimsQuery.data.items.map((claim) => {
          const active = claim.status === "reserved" || claim.status === "external_started";
          const releaseable = claim.status === "outcome_unknown" || claim.status === "succeeded";
          return (
            <div key={claim.id} className="flex flex-wrap items-center justify-between gap-3 px-3 py-3">
              <div className="min-w-0">
                <p className="font-medium">{claim.title}</p>
                <p className="text-xs text-muted-foreground">{claim.destination} · {claim.status === "outcome_unknown" ? "Outcome unknown" : claim.status === "succeeded" ? "Succeeded" : claim.status === "external_started" ? "Send in progress" : "Reserved"}</p>
              </div>
              {releaseable && (
                <Button
                  variant="outline"
                  size="sm"
                  loading={releaseClaim.isPending && releaseClaim.variables?.id === claim.id}
                  disabled={releaseClaim.isPending}
                  onClick={() => {
                    if (claim.status === "outcome_unknown" || claim.status === "succeeded") {
                      releaseClaim.mutate({ id: claim.id, reviewToken: claim.review_token, expectedStatus: claim.status });
                    }
                  }}
                >
                  Allow retry
                </Button>
              )}
              {active && <span className="text-xs text-muted-foreground">Active claims cannot be released.</span>}
            </div>
          );
        })}
      </div>
      {releaseClaim.isError && <p role="alert" className="mt-3 text-sm text-destructive-text">{apiErrorMessage(releaseClaim.error, "Could not release this acquisition claim. Check the destination again.")}</p>}
    </section>
  );
}

/** What the inbox says when nothing is on file: requests off, or switched on with nothing waiting. */
export function RequestsEmptyState({ requestsEnabled, autoSend }: { requestsEnabled: boolean; autoSend: boolean }) {
  return (
    requestsEnabled ? (
      <EmptyState
        title="Nothing waiting"
        hint={
          autoSend
            ? "When a run turns up a great pick your library doesn't have, it lands here for your approval. The strongest ones are sent for you — Settings → Requests decides where that line sits."
            : "When a run turns up a great pick your library doesn't have, it lands here for your approval. Nothing is sent without you: automatic sending is off in Settings → Requests."
        }
        action={
          <Button asChild variant="outline" size="sm">
            <Link to={SETTINGS_LINK}>
              Go to Settings &rarr; Requests
            </Link>
          </Button>
        }
      />
    ) : (
      // The hint used to restate the page subtitle directly above it in different
      // words. An empty state's job is to say what to do next, not to re-introduce the
      // feature the reader just read about.
      <EmptyState
        title="Requests are off"
        hint="Switch them on and missing titles start collecting here."
        action={
          <Button asChild variant="outline" size="sm">
            <Link to={SETTINGS_LINK}>
              Go to Settings &rarr; Requests
            </Link>
          </Button>
        }
      />
    )
  );
}

/** The Waiting tab: the select-all bar, the send/reject/dismiss actions, and the rows. */
export function WaitingSection({
  pending,
  pendingShown,
  selected,
  selectedPending,
  allChecked,
  requestsEnabled,
  busy,
  viaSeerr,
  globalTag,
  preferredLanguages,
  languageModeOn,
  arrView,
  nameOf,
  send,
  reject,
  del,
  toggle,
  toggleAll,
  act,
  decide,
  clearSelection,
}: {
  /** Every waiting title on file, before filters, for the "nothing clears these filters" count. */
  pending: RequestCandidate[];
  pendingShown: RequestCandidate[];
  selected: Set<number>;
  selectedPending: number[];
  allChecked: boolean;
  requestsEnabled: boolean;
  busy: boolean;
  viaSeerr: boolean;
  globalTag: string;
  preferredLanguages: string[];
  languageModeOn: boolean;
  arrView: (item: RequestCandidate) => ArrView;
  nameOf: DisplayNameLookup;
  send: SendMutation;
  reject: RejectMutation;
  del: DeleteMutation;
  toggle: (id: number) => void;
  toggleAll: () => void;
  act: (mutate: () => void) => void;
  decide: (id: number, mutate: () => void) => void;
  clearSelection: () => void;
}) {
  if (pending.length === 0) {
    return (
      <EmptyState
        title="Inbox clear"
        hint="Nothing is waiting on you right now. Titles your people would have loved, but your library doesn't have, will show up here after the next run."
      />
    );
  }
  return (
    <section className={cn("space-y-3", selectedPending.length > 0 && "pb-24")}>
      {/* The action bar. Quiet until something is ticked, then it lights up and says
          what it will act on. Reject-vs-Dismiss is spelled out in each row's ⋯ menu,
          where the choice is made one title at a time. */}
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-3 gap-y-2 px-1 py-2",
          selectedPending.length > 0
            ? "rounded-lg border border-border-strong bg-elevated px-3"
            : "",
        )}
      >
        <label className="flex min-h-6 cursor-pointer items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={allChecked}
            // Some ticked but not all: a half-tick, so "3 selected" beside an empty
            // box does not read as nothing selected.
            ref={(box) => {
              if (box) box.indeterminate = selectedPending.length > 0 && !allChecked;
            }}
            disabled={!requestsEnabled}
            onChange={toggleAll}
            className="h-4 w-4 accent-primary disabled:cursor-not-allowed disabled:opacity-50"
          />
          {`${pendingShown.length} waiting`}
        </label>
        {/* Send first, and separated: it is the reason the page exists, and the
            page's one filled amber control. Named, because the cards carry their
            own Send and Reject — this group acts on the ticked rows. */}
        <div
          role="group"
          aria-label="Actions for the selected titles"
          // Once something is ticked the actions float at the bottom of the screen, so they stay in
          // reach however far down a long queue the ticked rows are.
          className={
            selectedPending.length > 0
              ? "fixed bottom-4 left-4 right-4 z-20 mx-auto flex max-w-3xl flex-wrap items-center gap-3 rounded-xl border border-border-strong bg-elevated px-4 py-3 shadow-elevated md:left-[calc(15rem+1rem)] md:right-8"
              : "hidden"
          }
        >
          <span className="text-sm font-medium">
            {selectedPending.length} selected
          </span>
          <span
            aria-hidden="true"
            className="mx-1 h-5 w-px bg-border"
          />
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
            title={`Ask ${viaSeerr ? "Overseerr" : "Radarr or Sonarr"} for the selected titles now.`}
          >
            {!send.isPending && <Send aria-hidden="true" />}
            Send {selectedPending.length}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={
              !requestsEnabled ||
              selectedPending.length === 0 ||
              busy
            }
            onClick={() =>
              act(() => reject.mutate(selectedPending))
            }
            title="Never ask Radarr or Sonarr for these again. They won't come back to this list."
          >
            <X aria-hidden="true" />
            Reject
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="text-muted-foreground"
            disabled={
              !requestsEnabled ||
              selectedPending.length === 0 ||
              busy
            }
            onClick={() =>
              act(() => del.mutate(selectedPending))
            }
            title="Take these off the list for now. If a later run turns one up again, it comes back."
          >
            <Trash2 aria-hidden="true" />
            Dismiss
          </Button>
        </div>
        {selectedPending.length > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={clearSelection}
          >
            Clear selection
          </Button>
        )}
      </div>

      {(send.isError || reject.isError || del.isError) && (
        <p
          role="alert"
          className="text-sm text-destructive-text"
        >
          {apiErrorMessage(
            send.error ?? reject.error ?? del.error,
            "That didn't go through. Check the server log and try again.",
          )}
        </p>
      )}

      {pendingShown.length > 0 ? (
        <div className="divide-y divide-border rounded-xl border bg-card shadow-elevated">
          {pendingShown.map((item) => (
            <PendingRow
              key={item.id}
              item={item}
              viaSeerr={viaSeerr}
              checked={selected.has(item.id)}
              onToggle={toggle}
              globalTag={globalTag}
              preferredLanguages={preferredLanguages}
              languageModeOn={languageModeOn}
              disabled={!requestsEnabled}
              arrView={arrView(item)}
              nameOf={nameOf}
              busy={busy}
              // `decide`, not `act`: a per-row decision must not clear a batch
              // the owner had half-assembled in the checkboxes.
              onSend={(id) =>
                decide(id, () => send.mutate({ ids: [id] }))
              }
              onDelete={(id) =>
                decide(id, () => del.mutate([id]))
              }
              onReject={(id) =>
                decide(id, () => reject.mutate([id]))
              }
              sending={
                send.isPending &&
                send.variables?.ids?.length === 1 &&
                send.variables.ids[0] === item.id
              }
            />
          ))}
        </div>
      ) : (
        // Filtered down to nothing — say so, or the queue reads as empty when
        // {pending.length} titles are one click away.
        <NoMatches label="waiting" total={pending.length} />
      )}
    </section>
  );
}

/** The Sent tab: the send log, with its clear actions. */
export function SentSection({
  sent,
  sentShown,
  viaSeerr,
  radarrUrl,
  sonarrUrl,
  overseerrUrl,
  clear,
  busy,
  arrView,
  nameOf,
}: {
  sent: RequestCandidate[];
  sentShown: RequestCandidate[];
  viaSeerr: boolean;
  radarrUrl: string;
  sonarrUrl: string;
  overseerrUrl: string;
  clear: ClearMutation;
  busy: boolean;
  arrView: (item: RequestCandidate) => ArrView;
  nameOf: DisplayNameLookup;
}) {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-muted-foreground">
          Sent to {viaSeerr ? "Overseerr" : "Radarr & Sonarr"}
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
            title="Clear every entry shown here from the send log. The titles stay in Radarr/Sonarr and won't be asked for again."
          >
            {!clear.isPending && (
              <Trash2 aria-hidden="true" />
            )}
            Clear all ({sentShown.length})
          </Button>
        )}
      </div>
      {clear.isError && (
        <p
          role="alert"
          className="text-sm text-destructive-text"
        >
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
              overseerrUrl={overseerrUrl}
              onClear={(id) => clear.mutate([id])}
              clearing={clear.isPending}
              arrView={arrView(item)}
              nameOf={nameOf}
            />
          ))}
        </div>
      ) : sent.length > 0 ? (
        // Sent titles ARE on file, the filters are just hiding them all — saying
        // "nothing sent yet" here would be plainly false.
        <NoMatches label="sent" total={sent.length} />
      ) : (
        <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">
          Nothing sent yet. Whenever a title goes out &mdash;
          because you approved it in Waiting, or because
          Shortlist sent it for you &mdash; it&rsquo;s logged
          here: the title, when it went, what the app said,
          and who wanted it.
        </p>
      )}
    </section>
  );
}

/** The Rejected tab: blocked titles, with the way back to Waiting. */
export function RejectedSection({
  rejected,
  rejectedShown,
  restore,
  busy,
  nameOf,
}: {
  rejected: RequestCandidate[];
  rejectedShown: RequestCandidate[];
  restore: RestoreMutation;
  busy: boolean;
  nameOf: DisplayNameLookup;
}) {
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          These are blocked: no run will ask Radarr or Sonarr
          for them again.{" "}
          <strong className="font-medium text-foreground">
            Allow again
          </strong>{" "}
          moves one straight back to Waiting so you can send
          it.
        </p>
        {rejectedShown.length > 0 && (
          <Button
            variant="outline"
            size="sm"
            loading={restore.isPending}
            disabled={busy}
            onClick={() =>
              restore.mutate(rejectedShown.map((r) => r.id))
            }
            title="Move every rejected title shown here back to Waiting."
          >
            {!restore.isPending && (
              <RotateCcw aria-hidden="true" />
            )}
            Allow all again ({rejectedShown.length})
          </Button>
        )}
      </div>
      {restore.isError && (
        <p
          role="alert"
          className="text-sm text-destructive-text"
        >
          {apiErrorMessage(
            restore.error,
            "That didn't go through. Check the server log and try again.",
          )}
        </p>
      )}
      {rejectedShown.length > 0 ? (
        <div className="space-y-2">
          {rejectedShown.map((item) => (
            <RejectedRow
              key={item.id}
              item={item}
              onAllowAgain={(id) => restore.mutate([id])}
              disabled={busy}
              nameOf={nameOf}
            />
          ))}
        </div>
      ) : (
        <NoMatches label="rejected" total={rejected.length} />
      )}
    </section>
  );
}
