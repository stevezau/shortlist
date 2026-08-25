import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, Eye, Loader2, UserPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { BackLink } from "@/components/back-link";
import {
  OWNER_SHELF_ALERT_ID,
  OWNER_SHELF_NOTE_ID,
} from "@/components/owner-note";
import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiErrorMessage } from "@/lib/api";
import { encode, hasHome, hasLibrary } from "@/lib/placement";
import {
  queryKeys,
  useCollections,
  useDismissNotification,
  useHomeUserCandidates,
  useTransferWatchHistory,
  useUndoWatchTransfer,
  useUsers,
  useWatchSnapshots,
} from "@/lib/queries";
import type { Collection, TransferResult } from "@/lib/types";

/** Rows that put ONE COLLECTION PER PERSON on a library's Recommended shelf — the only kind that
 *  stacks up on the owner's shelf. A shared row is a single collection everybody is meant to see. */
export function rowsOnTheSharedShelf(collections: Collection[]): Collection[] {
  return collections.filter(
    (row) =>
      row.enabled &&
      row.build === "per_person" &&
      hasLibrary(row.placement_friends),
  );
}

function Step({
  n,
  title,
  children,
}: {
  /** Omitted when this section is mounted on its own — a lone "3" with no 1 or 2 above it reads as
   *  a missing step rather than a numbered one. The setup wizard embeds only the third section. */
  n?: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-3 text-lg font-semibold">
        {n !== undefined && (
          <span
            aria-hidden="true"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-muted text-sm font-medium text-muted-foreground"
          >
            {n}
          </span>
        )}
        {title}
      </h2>
      <div className={n === undefined ? "space-y-3" : "space-y-3 pl-10"}>
        {children}
      </div>
    </section>
  );
}

function OptionCard({
  title,
  body,
  action,
  recommended,
}: {
  title: string;
  body: React.ReactNode;
  action: React.ReactNode;
  recommended?: boolean;
}) {
  return (
    <Card className={recommended ? "border-foreground/30" : undefined}>
      <CardContent className="flex flex-col gap-3 pt-6 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 space-y-1">
          <p className="flex items-center gap-2 font-medium">
            {title}
            {recommended && (
              <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-normal uppercase tracking-wide text-muted-foreground">
                Recommended
              </span>
            )}
          </p>
          <p className="text-sm text-muted-foreground">{body}</p>
        </div>
        <div className="shrink-0">{action}</div>
      </CardContent>
    </Card>
  );
}

/**
 * The one place that turns "Plex shows the server owner everyone's rows" from a limitation you are
 * told about into a decision you can act on.
 *
 * Three surfaces already explained the limitation correctly and all three dead-ended at "watch on a
 * second account" — advice with no next step, which is why the same question kept arriving. This
 * page is that next step. It is deliberately NOT in Settings: the people who need it do not know it
 * exists, so it has to be reachable from where they meet the problem (the row editor's placement
 * grid, the Users page, and the notification bell all link here).
 */
export function WatchingAccountPage() {
  const usersQuery = useUsers();
  const collectionsQuery = useCollections();
  const dismiss = useDismissNotification();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  // `?setup=1` opens straight on the transfer step. The Users page links here that way, so pressing
  // "Watching account" lands on the tool rather than on the explainer above it — the guide is what
  // you need once, and the tool is what you come back for.
  const [chose, setChose] = useState<"shelf-off" | "transfer" | null>(
    params.get("setup") ? "transfer" : null,
  );

  const users = usersQuery.data ?? [];
  const collections = collectionsQuery.data ?? [];
  const others = users.filter(
    (user) => user.user_type !== "owner" && user.enabled,
  );
  const affected = rowsOnTheSharedShelf(collections);

  /** Take every per-person row off the friends' Recommended shelf, leaving its Home placement alone.
   *  One PATCH per row — the endpoint is partial, so only `placement_friends` moves and every other
   *  column on the row keeps its value. */
  // How many rows were actually written before a failure. The PATCHes are sequential, so "it
  // failed" and "nothing changed" are different claims — row 3 of 5 failing leaves 1 and 2 saved.
  const [changed, setChanged] = useState(0);
  const shelfOff = useMutation({
    mutationFn: async () => {
      setChanged(0);
      for (const [i, row] of affected.entries()) {
        await api.updateCollection(row.id, {
          name: row.name,
          placement_friends: encode(false, hasHome(row.placement_friends)),
        });
        setChanged(i + 1);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.collections });
      setChose("shelf-off");
    },
  });

  return (
    <div className="space-y-8">
      <BackLink to="/users" label="Users" />
      <PageHeader
        icon={Eye}
        title="You see everyone's rows"
        subtitle="Why the Recommended shelf in your libraries shows every person's row to you, and the three ways to deal with it."
      />

      <Step n={1} title="What's happening">
        <p className="text-sm text-muted-foreground">
          Shortlist gives each person their own row and keeps them apart with a
          Plex label, hidden from everyone else through the{" "}
          <strong className="text-foreground">share</strong> you gave them. You
          own this server, so you have no share with yourself &mdash; and there
          is nothing for Plex to hide behind.
        </p>
        <p className="text-sm text-muted-foreground">
          The result: your own Home screen is fine and only ever shows your row,
          but the <strong className="text-foreground">Recommended shelf</strong>{" "}
          inside Movies and TV Shows shows you{" "}
          <strong className="text-foreground">everyone&rsquo;s row</strong>
          {others.length > 0 &&
            ` — there are ${others.length} other ${others.length === 1 ? "person" : "people"} on this server`}
          . Everyone else still sees only their own. This is a Plex limitation,
          not something Shortlist can switch off.
        </p>
      </Step>

      <Step n={2} title="Your options">
        <OptionCard
          title="Take the rows off the library shelf"
          body={
            affected.length
              ? `Rows show on everyone's Home screen only. Nobody sees anyone else's, including you. You lose the row inside Movies and TV Shows. Affects ${affected.length} row${affected.length === 1 ? "" : "s"}.`
              : "Already done — no row is on the friends' Recommended shelf."
          }
          action={
            chose === "shelf-off" ? (
              <span className="flex items-center gap-1.5 text-sm text-success">
                <Check className="h-4 w-4" aria-hidden="true" />
                Saved
              </span>
            ) : (
              <Button
                variant="outline"
                disabled={!affected.length || shelfOff.isPending}
                onClick={() => shelfOff.mutate()}
              >
                {shelfOff.isPending && (
                  <Loader2
                    className="h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                )}
                Do this for me
              </Button>
            )
          }
        />

        <OptionCard
          title="Leave it — I don't mind seeing them"
          body="Nothing changes. Shortlist stops mentioning it — both the alert and the note on the Users page."
          action={
            <Button
              variant="ghost"
              disabled={dismiss.isPending}
              // Unlike a bell click, this IS a decision about the fact itself, so it retires both
              // surfaces. Sequential rather than parallel: the second write reads the list the first
              // one wrote, and firing them together loses one to a last-write-wins race.
              onClick={async () => {
                await dismiss.mutateAsync(OWNER_SHELF_ALERT_ID);
                await dismiss.mutateAsync(OWNER_SHELF_NOTE_ID);
              }}
            >
              {dismiss.isSuccess ? "Dismissed" : "Dismiss"}
            </Button>
          }
        />

        <OptionCard
          recommended
          title="Move my watching to a separate account"
          body="Keep the library shelf AND stop seeing everyone else's rows. You watch on a second Plex account and keep this one for administering the server. Shortlist can bring your watch history across."
          action={
            <Button onClick={() => setChose("transfer")}>
              <UserPlus className="h-4 w-4" aria-hidden="true" />
              Set it up
              <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          }
        />

        {chose === "shelf-off" && (
          <p className="rounded-md border border-dashed bg-muted/30 p-3 text-sm">
            Saved. Plex still shows the rows until each one is rebuilt &mdash;
            placement is applied by a row&rsquo;s next run, not straight away.
            Rows with a schedule clear on their next one; for any without, use{" "}
            <strong>Run now</strong> on the Rows page.
          </p>
        )}

        {shelfOff.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t change the rows:{" "}
            {apiErrorMessage(
              shelfOff.error,
              "something went wrong talking to Shortlist.",
            )}{" "}
            {changed > 0
              ? `Changed ${changed} of ${affected.length} before this failed — run it again to finish the rest.`
              : "Nothing was changed — try again."}
          </p>
        )}
      </Step>

      {chose === "transfer" && <TransferSteps />}
    </div>
  );
}

/** Step 3: pick the account, then move the history onto it.
 *
 *  Creating the Plex Home user is left to Plex on purpose — it is two taps in the Plex app, and
 *  doing it there means Shortlist never has to create an account that briefly exists with no
 *  library filters on it. "I've made it" simply re-reads the Home roster.
 */
/** Exported so the SETUP WIZARD can mount the very same flow.
 *
 *  Not copied into the wizard: this is the only place that knows how a transfer works (which Home
 *  accounts are candidates, that the target must have been synced first, dry-run before commit), and
 *  a second copy would drift from it the first time any of that changed. The wizard cannot link here
 *  instead — until setup completes, every route redirects to /setup — so the component travels to the
 *  wizard rather than the owner travelling to the page. */
export function TransferSteps({ numbered = true }: { numbered?: boolean }) {
  // "Set it up" mounts this section below the fold, so without moving the viewport the button reads
  // as having done nothing. Scrolled on mount rather than in the click handler: the section does not
  // exist yet at click time. `prefers-reduced-motion` gets a jump instead of a glide.
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const reduced = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    ref.current?.scrollIntoView({
      behavior: reduced ? "auto" : "smooth",
      block: "start",
    });
  }, []);

  const candidates = useHomeUserCandidates();
  const users = useUsers();
  const [target, setTarget] = useState<number | null>(null);
  const [preview, setPreview] = useState<TransferResult | null>(null);
  // Which account the preview describes. Without it, previewing account A and then selecting B left
  // A's numbers and A's acknowledgement on screen, authorising a real run against B.
  const [previewOf, setPreviewOf] = useState<number | null>(null);
  // Copying is additive; removing is not. A preview that says "this un-marks 412 things" has to be
  // acknowledged before the real run, so the destructive half is never a surprise.
  const [acceptedRemovals, setAcceptedRemovals] = useState(false);
  const transfer = useTransferWatchHistory();
  // TWO instances, deliberately. One served both the dry run and the real one, so previewing an
  // undo set `isSuccess` — which disabled the real Undo button and rendered "Put back exactly as it
  // was", a factual claim about a Plex write that never happened.
  const undoPreviewCall = useUndoWatchTransfer();
  const undo = useUndoWatchTransfer();
  // Undos that are still available, from the server rather than from the last response. The queue
  // exists precisely so the work survives a request timing out — and in exactly that case the
  // response carrying the snapshot id never arrives, so the completed destructive run had no way
  // back. A page reload had the same effect.
  const snapshots = useWatchSnapshots();
  // The undo's own preview. It is a mirror too — it removes whatever the snapshot lacks, which is
  // everything watched on that account since — and it had none of the protection the transfer got.
  const [undoPreview, setUndoPreview] = useState<{
    id: number;
    report: TransferResult;
  } | null>(null);
  // WHICH snapshot the last real undo restored, and for WHICH account. `undo.isSuccess` is
  // mutation-wide, so restoring an older snapshot marked a later copy's Undo as already done —
  // disabled, and claiming "Put back exactly as it was" about a write that never happened. The
  // account is needed too: the list offers every account's snapshots.
  const [undone, setUndone] = useState<{ id: number; userId: number } | null>(
    null,
  );
  // WHICH account the last real copy actually ran against — not the live radio selection. `target`
  // moves the moment someone clicks another Home user, and the success panel does not reset with it,
  // so comparing against `target` made the panel re-claim "now matches yours" about an account that
  // had just been restored. Same reason `previewOf` exists for the preview.
  const [transferredTo, setTransferredTo] = useState<number | null>(null);
  // Which snapshot a failed undo was for AND why — scoped to the row so it does not render under
  // every snapshot, and carrying the server's own sentence because a refusal comes back as a 200
  // with the reason in `errors`, where React Query has no error object and the generic fallback
  // ("please try again") invited a retry of something that can never succeed.
  const [undoFailure, setUndoFailure] = useState<{
    id: number;
    reason: string;
  } | null>(null);
  // A real run that came back `dry_run` — safe mode forced it. Tracked because the page would
  // otherwise reset itself silently, with nothing to say why nothing happened.
  const [safeModeBlocked, setSafeModeBlocked] = useState(false);
  // Offered right here rather than as "go and run the watch sync": until setup finishes every route
  // redirects back to /setup, so a wizard reading "go to Jobs" is being sent somewhere it cannot go.
  const readHistory = useMutation({
    mutationFn: () => api.runJob("sync.history", {}, true),
  });

  // Nothing has ever been read for the owner, so the copy could not have copied anything. Kept
  // apart from `copied === 0` — as a bare count it reads as a feature that worked and found
  // nothing, which is exactly how it went unnoticed (#88).
  const nothingToCopy = transfer.isSuccess && transfer.data.source_empty;

  // The transfer keys on Shortlist's own user id, which only exists once a user sync has picked the
  // new Home account up. Anyone not yet synced is shown with that as the next step, rather than
  // being silently missing from a list they can see in Plex.
  const byAccount = new Map(
    (users.data ?? []).map((u) => [u.plex_account_id, u]),
  );

  const run = (dryRun: boolean) => {
    if (target === null) return;
    transfer.mutate(
      { to_user_id: target, dry_run: dryRun },
      {
        onSuccess: (result) => {
          // Safe mode forces `dry_run` on server-side even when the real button was pressed, so a
          // "real" run can come back having written nothing. Keying the reset on the LOCAL `dryRun`
          // then wiped the preview, re-disabled the button and restored "Press Preview first" — with
          // no mention anywhere that safe mode was the reason. The undo half got that sentence in an
          // earlier round; this half never did.
          const reallyWrote = !dryRun && !result.dry_run;
          // Asked for a real run and got a dry one back: safe mode. Without this the page simply
          // reset itself and said nothing at all had happened.
          setSafeModeBlocked(!dryRun && result.dry_run);
          setPreview(reallyWrote ? null : result);
          setPreviewOf(reallyWrote ? null : target);
          if (!reallyWrote) setAcceptedRemovals(dryRun ? false : acceptedRemovals);
          if (reallyWrote) {
            // A new copy must never inherit an earlier restore's verdict.
            setUndone(null);
            setTransferredTo(target);
            // A real copy changes the very account any pending undo preview described, so that
            // preview's numbers and its enabled "Restore it" button are no longer about anything
            // that exists. The transfer's own preview has `previewOf` for this; the undo had nothing.
            setUndoPreview(null);
            setUndoFailure(null);
          }
        },
      },
    );
  };

  const removals = preview ? preview.unmarks + preview.offsets_cleared : 0;
  // Blocked until a preview FOR THIS ACCOUNT has been seen, and its removals accepted.
  //
  // Keying only on `removals > 0` meant no preview at all read as "nothing to remove": pressing the
  // real button first un-ticked a Home user's watch history with no listing, no count and no
  // tick-box. And `previewOf` is compared to the current target because an acknowledgement given for
  // one account must not authorise a real run against another.
  const staleTarget = preview !== null && previewOf !== target;
  /** A 200 is only a restore when it carried no errors AND actually wrote.
   *
   *  Safe mode forces `dry_run` on server-side even when the real button was pressed, so a report
   *  can come back `{dry_run: true, applied: 1, errors: []}` — which the previous check read as a
   *  completed restore and captioned "Put back exactly as it was." The transfer half of this page
   *  already guarded on `dry_run`; the undo half did not. */
  const undoLanded = (r: TransferResult) => r.errors.length === 0 && !r.dry_run;

  const undoFailureReason = (r: TransferResult) =>
    r.errors[0] ??
    (r.dry_run
      ? "Safe mode is on, so nothing was written."
      : "please try again.");

  // Did the last real undo restore THIS transfer's snapshot? Mutation-wide `isSuccess` could not
  // answer that, and got it wrong whenever an older snapshot had been restored in the same session.
  // "Has a restore reversed what THIS panel is describing?"
  //
  // Two ways to get this wrong, both of which put a false claim about a Plex write on screen:
  //   * matching only on snapshot id says NO for a converged re-run, which takes no snapshot — so
  //     restoring from the list left the panel still claiming "that account now matches yours";
  //   * accepting any restore when the id is null says YES for a restore of a DIFFERENT account —
  //     the list offers every account's snapshots, so restoring kids-tv flipped steve-tv's panel.
  //
  // So it matches on the snapshot when there is one, and on the ACCOUNT when there is not.
  const undoneThisOne =
    undone !== null &&
    (transfer.data?.snapshot_id == null
      ? undone.userId === transferredTo
      : undone.id === transfer.data.snapshot_id);
  const blocked =
    preview === null || staleTarget || (removals > 0 && !acceptedRemovals);

  return (
    <div ref={ref} className="scroll-mt-6">
      <Step n={numbered ? 3 : undefined} title="Set up the watching account">
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            Create the account in Plex &mdash;{" "}
            <strong className="text-foreground">
              Settings &rarr; Home &rarr; Add user
            </strong>{" "}
            &mdash; and share the same libraries you can see. Then pick it here.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => candidates.refetch()}
            disabled={candidates.isFetching}
          >
            {candidates.isFetching && (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            )}
            I&rsquo;ve made it &mdash; look again
          </Button>
        </div>

        <QueryBoundary
          query={candidates}
          skeleton={<Skeleton className="h-24 w-full" />}
          isEmpty={(list) => list.length === 0}
          empty={
            <EmptyState
              title="No other Plex Home users yet"
              hint="Add one in Plex under Settings → Home, share your libraries with it, then use the button above."
            />
          }
        >
          {(list) => (
            <div className="space-y-2">
              {list.map((candidate) => {
                const known = byAccount.get(candidate.plex_account_id);
                const blocked = candidate.already_a_shortlist_user || !known;
                return (
                  <label
                    key={candidate.plex_account_id}
                    className={`flex items-start gap-3 rounded-md border p-3 text-sm ${blocked ? "opacity-60" : "cursor-pointer"}`}
                  >
                    <input
                      type="radio"
                      name="watching-account"
                      className="mt-1"
                      disabled={blocked}
                      checked={known ? target === known.id : false}
                      onChange={() => {
                        if (!known) return;
                        setTarget(known.id);
                        // A preview describes ONE account. Carrying it (and its acknowledgement)
                        // across a change of account would authorise a real run against numbers
                        // that were never shown for it.
                        setPreview(null);
                        setPreviewOf(null);
                        setAcceptedRemovals(false);
                      }}
                    />
                    <span className="min-w-0">
                      <span className="font-medium">{candidate.title}</span>
                      {candidate.already_a_shortlist_user && (
                        <span className="block text-xs text-muted-foreground">
                          Already has a row of its own &mdash; copying your
                          history onto it would blend two people&rsquo;s taste
                          into one.
                        </span>
                      )}
                      {!known && !candidate.already_a_shortlist_user && (
                        <span className="block text-xs text-muted-foreground">
                          Shortlist hasn&rsquo;t picked this account up yet. Run{" "}
                          <strong className="text-foreground">
                            Sync users
                          </strong>{" "}
                          in Jobs, then come back.
                        </span>
                      )}
                      {candidate.protected && (
                        <span className="block text-xs text-muted-foreground">
                          PIN-protected, so Shortlist can&rsquo;t sign in as it
                          &mdash; it can&rsquo;t copy your history onto this
                          account.
                        </span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </QueryBoundary>

        <div className="rounded-md border border-dashed p-3 text-sm">
          <p className="font-medium">What this does to the new account</p>
          <p className="mt-1 text-xs text-muted-foreground">
            It ends up matching yours: the same films ticked off, the same
            episodes of each show, and anything you&rsquo;re part-way through
            sitting at the same point in Continue Watching. Anything watched on
            that account that you haven&rsquo;t watched is un-ticked, so the two
            really do match. Your own account is never written to.{" "}
            <strong className="text-foreground">
              Plex records it all as watched today
            </strong>{" "}
            &mdash; nothing can store the original dates. They&rsquo;re written
            oldest first so Continue Watching still comes out in the right
            order, and Shortlist keeps the real dates itself, so your
            recommendations are unaffected.
          </p>
        </div>

        {/* Hidden only when the success panel's OWN inline undo has taken over — which needs a
            snapshot to exist. A converged re-run, the very thing this page tells you to do ("Run it
            again — it only writes what's still missing"), writes nothing, takes no snapshot and
            returns `snapshot_id: null`, so the panel renders no Undo. Hiding the list on top of that
            left no route back to the earlier copy at all, which is the exact failure the list was
            added to fix. */}
        {(snapshots.data ?? []).length > 0 &&
          !(
            transfer.isSuccess &&
            !transfer.data.dry_run &&
            !nothingToCopy &&
            transfer.data.snapshot_id !== null
          ) && (
          <div className="space-y-2 rounded-md border border-dashed p-3 text-sm">
            <p className="font-medium">An earlier copy can still be undone</p>
            {(snapshots.data ?? []).map((snapshot) => (
              <div key={snapshot.id} className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={undoPreviewCall.isPending || !snapshot.complete}
                    onClick={() =>
                      (setUndoFailure(null),
                      undoPreviewCall.mutate(
                        { snapshot_id: snapshot.id, dry_run: true },
                        {
                          onSuccess: (r) =>
                            setUndoPreview({ id: snapshot.id, report: r }),
                          onError: (e) =>
                            setUndoFailure({
                              id: snapshot.id,
                              reason: apiErrorMessage(e, "please try again."),
                            }),
                        },
                      ))
                    }
                  >
                    Preview undoing the copy onto {snapshot.username}
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    {snapshot.complete
                      ? `Would put back ${snapshot.entries} title${snapshot.entries === 1 ? "" : "s"} as they were.`
                      : "Can't be undone — a library wasn't readable when that copy ran, so the saved state is incomplete."}
                  </span>
                </div>
                {undoFailure?.id === snapshot.id && (
                  <p className="text-xs text-destructive">
                    Couldn&rsquo;t undo it: {undoFailure.reason}
                  </p>
                )}
                {undoPreview?.id === snapshot.id && (
                  <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
                    {/* Undo is a mirror in the other direction, so it REMOVES anything watched on
                        that account since the copy. It used to be one unguarded click. */}
                    <p>
                      Restoring makes {snapshot.username} match the saved state
                      again. That un-ticks{" "}
                      <strong>
                        {undoPreview.report.unmarks +
                          undoPreview.report.offsets_cleared}
                      </strong>{" "}
                      thing(s) watched on it since &mdash; including anything
                      watched there after the copy.
                    </p>
                    {undoPreview.report.removals_preview.length > 0 && (
                      <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-muted-foreground">
                        {undoPreview.report.removals_preview.map((title) => (
                          <li key={title}>{title}</li>
                        ))}
                      </ul>
                    )}
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={undo.isPending}
                      onClick={() =>
                        (setUndoFailure(null),
                        undo.mutate(
                          { snapshot_id: snapshot.id, dry_run: false },
                          {
                            onSuccess: (r) => {
                              if (!undoLanded(r)) {
                                setUndoFailure({
                                  id: snapshot.id,
                                  reason: undoFailureReason(r),
                                });
                                return;
                              }
                              setUndoPreview(null);
                              setUndoFailure(null);
                              setUndone({ id: snapshot.id, userId: snapshot.user_id });
                            },
                            onError: (e) =>
                              setUndoFailure({
                                id: snapshot.id,
                                reason: apiErrorMessage(e, "please try again."),
                              }),
                          },
                        ))
                      }
                    >
                      Restore it
                    </Button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            disabled={target === null || transfer.isPending}
            onClick={() => run(true)}
          >
            {transfer.isPending && (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            )}
            Preview
          </Button>
          <Button
            disabled={target === null || transfer.isPending || blocked}
            onClick={() => run(false)}
          >
            Copy my history across
          </Button>
          {blocked && (
            <span className="text-xs text-muted-foreground">
              {preview === null || staleTarget
                ? "Press Preview first \u2014 it shows what would change, including anything it would un-tick."
                : "Tick the box above to continue."}
            </span>
          )}
        </div>

        {nothingToCopy && (
          <div className="space-y-2 rounded-md border border-dashed p-3 text-sm">
            <p>
              Shortlist hasn&rsquo;t read your watch history yet, so there is
              nothing to copy across. It reads everyone&rsquo;s overnight
              &mdash; or you can do it now and come straight back.
            </p>
            {readHistory.isSuccess ? (
              <p className="text-muted-foreground">
                Reading your watch history. On a large library this takes a few
                minutes &mdash; try the copy again once it has finished. If it
                still comes up empty, the read didn&rsquo;t get through: check{" "}
                <strong className="text-foreground">Sync watch history</strong>{" "}
                on the Jobs page.
              </p>
            ) : (
              <Button
                variant="outline"
                size="sm"
                onClick={() => readHistory.mutate()}
                disabled={readHistory.isPending}
              >
                {readHistory.isPending && (
                  <Loader2
                    className="h-4 w-4 animate-spin"
                    aria-hidden="true"
                  />
                )}
                Read my watch history now
              </Button>
            )}
            {readHistory.isError && (
              <p className="text-destructive">
                Couldn&rsquo;t start the read:{" "}
                {apiErrorMessage(readHistory.error, "please try again.")}
              </p>
            )}
          </div>
        )}

        {safeModeBlocked && (
          <p className="rounded-md border border-dashed p-3 text-sm">
            <strong>Safe mode is on, so nothing was written.</strong> Turn it off
            in Settings to let this run for real.
          </p>
        )}

        {preview && !nothingToCopy && (
          <div className="space-y-2 rounded-md border border-dashed bg-muted/30 p-3 text-sm">
            <p>
              Would tick <strong>{preview.marks}</strong>{" "}
              {preview.marks === 1 ? "title" : "titles"} on that account
              {preview.offsets_set > 0 &&
                `, and set ${preview.offsets_set} back to where you'd got to`}
              . Nothing has been changed yet.
            </p>
            {preview.target_unreadable.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {/* `unreachable` was the obvious field and it is structurally 0 on a preview — every
                    write returns True under dry run — so that paragraph could never render.
                    `target_unreadable` is the preview's only working channel for "what can't be
                    written there". Built as ONE string rather than interleaved JSX so the sentence
                    stays a single text node: that is what a person reads and what a test can find. */}
                {preview.target_unreadable.length === 1
                  ? "That account can't see one of your libraries, so anything in it will be skipped. Share it with that account if you want those carried across too."
                  : `That account can't see ${preview.target_unreadable.length} of your libraries, so anything in them will be skipped. Share them with that account if you want those carried across too.`}
              </p>
            )}
            {removals > 0 && (
              <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
                <p>
                  <strong>
                    This also un-ticks {removals}{" "}
                    {removals === 1 ? "thing" : "things"}
                  </strong>{" "}
                  that account has watched and you haven&rsquo;t. That is what
                  makes the two match &mdash; and it&rsquo;s what repairs an
                  account an older version of Shortlist over-marked.
                </p>
                {preview.removals_preview.length > 0 && (
                  <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-muted-foreground">
                    {preview.removals_preview.map((title) => (
                      <li key={title}>{title}</li>
                    ))}
                  </ul>
                )}
                {removals > preview.removals_preview.length && (
                  <p className="text-xs text-muted-foreground">
                    &hellip;and {removals - preview.removals_preview.length}{" "}
                    more.
                  </p>
                )}
                <label className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={acceptedRemovals}
                    onChange={(e) => setAcceptedRemovals(e.target.checked)}
                  />
                  <span className="text-xs">
                    {/* The "can be undone" half is CONDITIONAL. A target that cannot see one of the
                        libraries gets an incomplete snapshot, and `undo_transfer` refuses to restore
                        from one — so promising an undo here, on the screen that authorises deleting
                        someone's watch history, was a promise the transfer itself would break. */}
                    I understand these will be un-ticked.{" "}
                    {preview.target_unreadable.length > 0 ? (
                      <strong className="text-foreground">
                        This copy will NOT be undoable, because that account
                        can&rsquo;t see all of your libraries &mdash; Shortlist
                        can&rsquo;t save a complete picture of its current state.
                      </strong>
                    ) : (
                      <>
                        Shortlist saves that account&rsquo;s current state first,
                        so this can be undone.
                      </>
                    )}
                  </span>
                </label>
              </div>
            )}
          </div>
        )}

        {transfer.isSuccess && !transfer.data.dry_run && !nothingToCopy && (
          <div className="space-y-2 rounded-md border bg-muted/40 p-3 text-sm">
            <p className="flex items-start gap-2">
              <Check
                className="mt-0.5 h-4 w-4 shrink-0 text-success"
                aria-hidden="true"
              />
              <span>
                Copied <strong>{transfer.data.applied}</strong>{" "}
                {transfer.data.applied === 1 ? "change" : "changes"} across
                {transfer.data.unmarks > 0 &&
                  `, including ${transfer.data.unmarks} un-ticked`}
                . Switch to that account in your Plex app and watch there from
                now on &mdash; its row fills in on the next run.
              </span>
            </p>
            {/* Re-read afterwards rather than trusting the writes: Plex accepting a write is not
                the same as the write taking effect, and the old version reported counts it had
                never checked. */}
            {undoneThisOne ? (
              /* The verify lines below are present-tense claims about the account's CURRENT state,
                 and a landed undo reverses exactly that state — so they went on asserting "that
                 account now matches yours" beside "Put back exactly as it was.", two contradictory
                 claims about one account. The mismatch branch was worse: "Run it again" told the
                 owner to redo the copy they had just reversed. */
              <p className="text-xs text-muted-foreground">
                That account is back to how it was before the copy.
              </p>
            ) : transfer.data.verify_mismatched > 0 ? (
              <p className="text-xs text-destructive">
                {transfer.data.verify_mismatched} didn&rsquo;t take effect when
                Shortlist checked afterwards. Run it again &mdash; it only
                writes what&rsquo;s still missing.
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Checked afterwards: that account now matches yours.
              </p>
            )}
            {transfer.data.unreachable > 0 && (
              <p className="text-xs text-muted-foreground">
                {transfer.data.unreachable} were in libraries that account
                can&rsquo;t see and were skipped.
              </p>
            )}
            {/* A partial target read makes the snapshot incomplete, and `undo_transfer` refuses it —
                returning 200 with the reason in `errors`. Offering the button anyway meant the
                refusal fired `onSuccess` and the panel flipped to "Put back exactly as it was."
                about a Plex write that never happened. The snapshot LIST already got this right;
                this was the un-fixed half. */}
            {transfer.data.target_unreadable.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {transfer.data.target_unreadable.length === 1
                  ? "This can't be undone: one library wasn't readable for that account, so the saved state is incomplete."
                  : `This can't be undone: ${transfer.data.target_unreadable.length} libraries weren't readable for that account, so the saved state is incomplete.`}
              </p>
            )}
            {transfer.data.errors.length > 0 && (
              <p className="text-xs text-destructive">
                {transfer.data.errors[0]}
              </p>
            )}
            {transfer.data.snapshot_id !== null &&
              transfer.data.target_unreadable.length === 0 && (
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={undo.isPending || undoneThisOne}
                  onClick={() =>
                    (setUndoFailure(null),
                    undo.mutate(
                      {
                        snapshot_id: transfer.data.snapshot_id as number,
                        dry_run: false,
                      },
                      {
                        // A 200 carrying `errors` is a REFUSAL — an incomplete snapshot, an account
                        // that is no longer a Home user, a restore that did not land. Treating any
                        // 200 as a completed restore is what let the panel claim success for one.
                        onSuccess: (r) =>
                          undoLanded(r)
                            ? (setUndoFailure(null),
                              setUndone({
                                id: transfer.data.snapshot_id as number,
                                // The account the COPY ran against, not whatever the radio shows
                                // now — the two diverge as soon as someone selects another user.
                                userId: transferredTo as number,
                              }))
                            : setUndoFailure({
                                id: transfer.data.snapshot_id as number,
                                reason: undoFailureReason(r),
                              }),
                        onError: (e) =>
                          setUndoFailure({
                            id: transfer.data.snapshot_id as number,
                            reason: apiErrorMessage(e, "please try again."),
                          }),
                      },
                    ))
                  }
                >
                  {undo.isPending && (
                    <Loader2
                      className="h-4 w-4 animate-spin"
                      aria-hidden="true"
                    />
                  )}
                  Undo this
                </Button>
                <span className="text-xs text-muted-foreground">
                  {/* Says what it removes. This button is offered straight after a copy, when the
                      account has had no chance to accumulate anything of its own — but the panel
                      stays mounted, so the copy states the consequence rather than assuming. */}
                  {undoneThisOne
                    ? "Put back exactly as it was."
                    : "Puts that account back as it was before \u2014 un-ticking anything watched on it since."}
                </span>
              </div>
            )}
            {undoFailure?.id === transfer.data.snapshot_id && (
              <p className="text-xs text-destructive">
                {/* Rendered here BECAUSE the snapshot list is hidden while this panel is up. A
                    refusal comes back as a 200, so `undo.isError` is false for it — a partial or
                    refused restore used to show nothing while the line above still claimed the
                    account matched. */}
                Couldn&rsquo;t undo it: {undoFailure.reason}
              </p>
            )}
          </div>
        )}

        {transfer.isError && (
          <p className="text-sm text-destructive">
            Couldn&rsquo;t move the history:{" "}
            {apiErrorMessage(
              transfer.error,
              "something went wrong talking to Plex.",
            )}
          </p>
        )}
      </Step>
    </div>
  );
}
