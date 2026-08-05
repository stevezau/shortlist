import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, Eye, Loader2, UserPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";

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
  useUsers,
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
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <h2 className="flex items-center gap-3 text-lg font-semibold">
        <span
          aria-hidden="true"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-muted text-sm font-medium text-muted-foreground"
        >
          {n}
        </span>
        {title}
      </h2>
      <div className="space-y-3 pl-10">{children}</div>
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
  const [chose, setChose] = useState<"shelf-off" | "transfer" | null>(null);

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
          {others.length > 0 && ` — there are ${others.length} other ${others.length === 1 ? "person" : "people"} on this server`}
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
            Saved. Plex still shows the rows until each one is rebuilt &mdash; placement is applied
            by a row&rsquo;s next run, not straight away. Rows with a schedule clear on their next
            one; for any without, use <strong>Run now</strong> on the Rows page.
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
function TransferSteps() {
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
  const [scrobble, setScrobble] = useState(false);
  const [preview, setPreview] = useState<TransferResult | null>(null);
  const transfer = useTransferWatchHistory();

  // The transfer keys on Shortlist's own user id, which only exists once a user sync has picked the
  // new Home account up. Anyone not yet synced is shown with that as the next step, rather than
  // being silently missing from a list they can see in Plex.
  const byAccount = new Map(
    (users.data ?? []).map((u) => [u.plex_account_id, u]),
  );

  const run = (dryRun: boolean) => {
    if (target === null) return;
    transfer.mutate(
      { to_user_id: target, scrobble, dry_run: dryRun },
      { onSuccess: (result) => setPreview(dryRun ? result : null) },
    );
  };

  return (
    <div ref={ref} className="scroll-mt-6">
      <Step n={3} title="Set up the watching account">
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
                    onChange={() => known && setTarget(known.id)}
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
                        <strong className="text-foreground">Sync users</strong>{" "}
                        in Jobs, then come back.
                      </span>
                    )}
                    {candidate.protected && (
                      <span className="block text-xs text-muted-foreground">
                        PIN-protected, so Shortlist can&rsquo;t sign in as it
                        &mdash; the Plex checkmarks option below won&rsquo;t
                        work for this account.
                      </span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        )}
      </QueryBoundary>

      <label className="flex items-start gap-3 rounded-md border border-dashed p-3 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={scrobble}
          onChange={(e) => setScrobble(e.target.checked)}
        />
        <span>
          <span className="font-medium">
            Also carry your watched status across in Plex
          </span>
          <span className="block text-xs text-muted-foreground">
            Leave this off and Shortlist knows what you&rsquo;ve seen but Plex
            doesn&rsquo;t &mdash; the new account starts with everything
            unwatched, no ticks, and half-finished shows missing from Continue
            Watching. Turn it on and each title is marked watched on the new
            account, one write apiece.{" "}
            <strong className="text-foreground">
              Plex records them all as watched today
            </strong>{" "}
            &mdash; it has no way to store the original dates. Shortlist keeps
            the real ones itself either way, so your recommendations are
            unaffected.
          </span>
        </span>
      </label>

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
          disabled={target === null || transfer.isPending}
          onClick={() => run(false)}
        >
          Move my history across
        </Button>
      </div>

      {preview && (
        <p className="rounded-md border border-dashed bg-muted/30 p-3 text-sm">
          Would copy <strong>{preview.copied}</strong> titles
          {preview.already_present > 0 &&
            `, leaving ${preview.already_present} they've already watched alone`}
          {scrobble && `, and mark ${preview.scrobbled} of them watched in Plex`}
          . Nothing has been changed yet.
        </p>
      )}

      {transfer.isSuccess && !transfer.data.dry_run && (
        <p className="flex items-start gap-2 rounded-md border bg-muted/40 p-3 text-sm">
          <Check
            className="mt-0.5 h-4 w-4 shrink-0 text-success"
            aria-hidden="true"
          />
          <span>
            Copied <strong>{transfer.data.copied}</strong> titles
            {transfer.data.scrobbled > 0 &&
              ` and marked ${transfer.data.scrobbled} watched in Plex`}
            . Switch to that account in your Plex app and watch there from now
            on &mdash; its row fills in on the next run.
          </span>
        </p>
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
