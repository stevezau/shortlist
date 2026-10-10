import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, Info, Search, ShieldCheck, UserPlus } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";

import { MutationAlert } from "@/components/mutation-alert";
import { HeaderPopover } from "@/components/layout/header-popover";
import { OwnerNote } from "@/components/owner-note";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import {
  ColdStartBadge,
  DepartedBadge,
  SharingUntouchedBadge,
} from "@/components/user-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { GatedSwitch } from "@/components/ui/gated-switch";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { noRequestSource } from "@/lib/row-kinds";
import { rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { profileName, USER_TYPE_LABEL } from "@/lib/user-profile";
import { profileBlocksRows, userState, type UserState } from "@/lib/user-state";
import type { AccountPrivacy, Collection, PrivacyStatus, RowSources, User } from "@/lib/types";
import { capitalise, formatDate, timeAgo } from "@/lib/format";
import { dayTime } from "@/lib/when";
import { coarseHitArea } from "@/lib/hit-area";
import {
  queryKeys,
  useCollections,
  usePrivacyStatus,
  useRemoveUser,
  useRequestRowSources,
  useSetAllUsersEnabled,
  usePatchUser,
  useUsers,
} from "@/lib/queries";
import { personName } from "@/lib/user-names";

function needsAttention(user: User): boolean {
  return Boolean(user.restriction_profile || user.unhidden_rows || user.departed);
}

type StatusFilter = "all" | "attention" | UserState;

const STATUS_FILTERS: readonly { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "attention", label: "Needs attention" },
  { value: "on", label: "On" },
  { value: "paused", label: "Paused" },
  { value: "off", label: "Off" },
];

function matchesStatus(user: User, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "attention") return needsAttention(user);
  return userState(user) === filter;
}

const STATE_LABEL: Record<UserState, string> = { on: "On", paused: "Paused", off: "Off" };

function stateTitle(user: User, state: UserState): string {
  if (user.departed) return "Plex no longer lists this account, so Shortlist switched them off and removed their rows.";
  if (profileBlocksRows(user)) {
    return `Plex's ${profileName(user)} restriction profile is set on this account, so Shortlist builds no new rows for it.`;
  }
  if (state === "paused") return "Their rows are off Home and Recommended and stop updating until you resume them.";
  if (state === "off") return "Shortlist builds no new rows for them.";
  return "Their rows run on each row's schedule.";
}

/** Paused and Off say something the row's switch can't; On is exactly what the switch already shows. */
function StatePill({ user }: { user: User }) {
  const state = userState(user);
  if (state === "on") return null;
  const variant = state === "paused" ? "warning" : "outline";
  return (
    <Badge
      variant={variant}
      data-testid="user-state"
      title={stateTitle(user, state)}
      className={state === "off" ? "text-muted-foreground" : undefined}
    >
      {STATE_LABEL[state]}
    </Badge>
  );
}

/** Plex's own parental-control profile: a fact about the account, not a fault, so it is never red. */
function RestrictedPill({ user }: { user: User }) {
  if (!user.restriction_profile) return null;
  return (
    <Badge
      variant="outline"
      data-testid="restricted-pill"
      // Never broken over two lines at 390px: the name row wraps the pill as one unit instead.
      className="whitespace-nowrap font-medium text-muted-foreground"
      title={`Plex's ${profileName(user)} restriction profile is set on this account. Plex refuses hide rules for profiled accounts, so no row is built for it. Shortlist never changes the profile; set it to None in Plex to give this person recommendations.`}
    >
      Restricted · <span>{profileName(user)}</span>
    </Badge>
  );
}

/**
 * Each privacy state in the Privacy page's words, short enough for a column: the same live reading
 * (`usePrivacyStatus`), so the two pages can never disagree about an account.
 */
const PRIVACY_WORDS: Record<string, { label: string; dot: string }> = {
  hiding: { label: "Hiding every row", dot: "bg-success" },
  missing: { label: "Missing hide rules", dot: "bg-destructive" },
  unreadable_filter: { label: "Plex can’t read its restrictions", dot: "bg-destructive" },
  left_alone: { label: "Left alone by choice", dot: "bg-muted-foreground" },
  refused_by_plex: { label: "Won’t accept hide rules", dot: "bg-warning" },
  owner: { label: "You own the server", dot: "bg-muted-foreground" },
  unknown: { label: "Not checked", dot: "bg-muted-foreground" },
};

type PrivacyQuery = { data?: PrivacyStatus; isPending: boolean; isError: boolean; error: unknown };

/** Rows on the server this person can see that are not theirs; 0 until the live reading is in. */
function exposedRows(user: User, privacy: PrivacyQuery): number {
  const account = privacy.data?.accounts.find((a) => a.user_id === user.id);
  return account && privacy.data ? rowsNotHidden(account, privacy.data) : 0;
}

function PrivacyCell({ user, privacy }: { user: User; privacy: PrivacyQuery }) {
  const account: AccountPrivacy | undefined = privacy.data?.accounts.find((a) => a.user_id === user.id);
  // The same figure, in the same words, as the Dashboard's Privacy cell and the Privacy page's
  // "Hides X of Y rows" (`rowsNotHidden`) — never the run's per-library collection count. The cell
  // is tinted by the row (see `exposedRows`) and leads to the remedy: a profiled account's own page
  // says how to clear the profile; anything else is the Privacy page's to explain.
  const exposed = exposedRows(user, privacy);
  const name = personName(user);
  const exposure =
    exposed > 0 ? (
      <p className="mt-0.5" title={capitalise(rowsNotTheirs(exposed))}>
        <span className="font-medium">Sees {exposed} {exposed === 1 ? "row" : "rows"} not theirs</span>
        {" · "}
        <Link
          to={account?.state === "refused_by_plex" ? `/users/${user.id}` : "/privacy"}
          aria-label={`${name} ${rowsNotTheirs(exposed)} — how to fix it`}
          className="whitespace-nowrap underline underline-offset-2"
        >
          {account?.state === "refused_by_plex" ? "Fix in Plex →" : "See Privacy →"}
        </Link>
      </p>
    ) : null;

  if (privacy.isPending) return <Skeleton className="h-5 w-32" />;
  const failure = privacy.isError
    ? apiErrorMessage(privacy.error, "Couldn’t read plex.tv.")
    : (privacy.data?.error ?? null);
  let state: ReactNode;
  if (failure && !account) {
    state = (
      <span className="text-muted-foreground" title={failure}>
        Couldn’t read
      </span>
    );
  } else if (!account) {
    state = (
      <span className="text-muted-foreground" title="plex.tv doesn't list a share for this account.">
        —
      </span>
    );
  } else {
    const words = PRIVACY_WORDS[account.state] ?? PRIVACY_WORDS.unknown;
    state = (
      <span className="inline-flex items-center gap-2">
        <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${words?.dot ?? ""}`} />
        {words?.label}
      </span>
    );
  }
  return (
    <div>
      {state}
      {exposure}
    </div>
  );
}

type CollectionsQuery = { data?: Collection[]; isPending: boolean; isError: boolean };

/** How many switched-on rows have this person in their audience; a dash when they get none at all. */
function RowsCell({ user, collections }: { user: User; collections: CollectionsQuery }) {
  if (collections.isPending) return <Skeleton className="h-5 w-6" />;
  if (collections.isError || !collections.data) {
    return (
      <span data-testid="user-rows" title="Couldn’t load the rows">
        —
      </span>
    );
  }
  if (userState(user) === "off") {
    return (
      <span data-testid="user-rows" title="Off: no new rows are built for them">
        —
      </span>
    );
  }
  const count = collections.data.filter(
    (row) => row.enabled && (row.audience === "everyone" || row.audience_user_ids.includes(user.id)),
  ).length;
  return (
    <span data-testid="user-rows" className="tabular-nums">
      {count}
    </span>
  );
}

function PicksCell({ user }: { user: User }) {
  if (user.picks_watched_30d === null) {
    return <span title="Hasn’t had a pick yet">—</span>;
  }
  return (
    <span
      className="tabular-nums"
      title={
        user.last_pick_watched_at
          ? `Last watched a pick ${timeAgo(user.last_pick_watched_at)}`
          : "Hasn’t watched a pick yet"
      }
    >
      {user.picks_watched_30d}
    </span>
  );
}

/** "Today 02:30", "Yesterday 02:30", "Fri 9 Oct 02:30" — when the last run that included the person finished. It counts dry and cancelled runs too, so it must never be labelled as a build. */
function builtAt(iso: string | null): string {
  return iso ? dayTime(iso) : "Never";
}

/** A data cell's own label on a phone, where the person is a card and there are no column headers. */
function CellLabel({ children }: { children: ReactNode }) {
  return (
    <span className="mb-0.5 block text-xs font-semibold uppercase tracking-wide text-faint-foreground lg:hidden">
      {children}
    </span>
  );
}

/** Request context for one person: whether a Your requests row can find anything of theirs.
 *
 *  Reads the ONE row-sources query the page makes (`useRequestRowSources("")` costs up to a few
 *  dozen HTTP calls to Overseerr and the Arrs, so it is never made per row). A dash carries its
 *  reason in `title`: the dashes — can't read the sources, Overseerr not connected — would otherwise
 *  be indistinguishable from each other and from "no requests". With no source connected at all it
 *  says nothing: the page says that once, above the list. */
function RequestsCell({
  user,
  sources,
}: {
  user: User;
  sources: { data?: RowSources; isPending: boolean; isError: boolean };
}) {
  if (sources.isPending) {
    return <Skeleton data-testid="requests-loading" className="h-5 w-24" />;
  }
  const tag = user.requested_by_tag ? (
    <Badge variant="outline" className="max-w-full break-all">Tag: {user.requested_by_tag}</Badge>
  ) : null;
  const cell = (badge: ReactNode, note: string | null) => (
    <span className="inline-flex max-w-full flex-wrap items-center gap-x-2 gap-y-1">
      <span className="sr-only">Request status: </span>
      <span className="inline-flex max-w-full flex-wrap items-center gap-1.5">
        {badge}
        {tag}
      </span>
      {note && <span className="text-xs text-muted-foreground">{note}</span>}
    </span>
  );
  const dash = (reason: string) =>
    cell(
      <span title={reason} aria-label={reason}>
        <span className="hidden xl:inline">—</span><span className="xl:hidden">{reason}</span>
      </span>,
      null,
    );
  const data = sources.data;
  if (sources.isError || !data) return dash(unreadableSources(data));
  if (noRequestSource(data)) return null;
  // The endpoint never fails: a configured-but-down Overseerr answers 200 with nobody linked, which
  // would read as every shared person lacking an account and every managed one unable to get one.
  if (data.overseerr === "unreachable") return dash("Couldn’t read Overseerr");

  const person = data.people.find((p) => p.user_id === user.id);
  const ready = person?.ready ?? 0;
  const readyNote = ready > 0 ? `${ready} ready` : null;
  // Without Overseerr only the tag applies — "hasn't signed in" would blame them for an account
  // that can't exist — but tag-credited titles still count as ready.
  if (data.overseerr === "off") {
    return tag || readyNote ? cell(null, readyNote) : null;
  }
  if (person?.linked) {
    return cell(<Badge variant="success" title="Requests linked to their Overseerr account">Linked</Badge>, readyNote);
  }
  if (user.user_type === "managed") {
    return cell(
      <Badge variant="secondary">Can’t use Overseerr</Badge>,
      "Managed profiles can’t sign in to it",
    );
  }
  return cell(
    <Badge variant="secondary">No account</Badge>,
    "Hasn’t signed in to Overseerr",
  );
}

/** Why the Requests column is blank after a failed read. A failed REFETCH still has the last answer,
 *  which says which sources are configured — so an install with only Radarr/Sonarr is not told that
 *  an Overseerr it never connected is down. With no answer at all, nothing can be blamed by name. */
function unreadableSources(data: RowSources | undefined): string {
  const configured = data
    ? (
        [
          ["Overseerr", data.overseerr],
          ["Radarr", data.radarr],
          ["Sonarr", data.sonarr],
        ] as const
      )
        .filter(([, state]) => state !== "off")
        .map(([name]) => name)
    : [];
  return configured.length > 0
    ? `Couldn’t read ${configured.join("/")}`
    : "Couldn’t read the request sources";
}

/** Plex's word for each account type, as plain text under the name — every person has one. */
function UsersSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

export function UsersPage() {
  const usersQuery = useUsers();
  const privacy = usePrivacyStatus();
  const collections = useCollections();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [sort, setSort] = useState("name");
  // Bulk selection is a mode: the checkboxes and the bulk bar appear when the owner asks for them, so
  // the list itself reads as people rather than as a form.
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [allActionsOpen, setAllActionsOpen] = useState(false);
  const [batchError, setBatchError] = useState("");
  const users = usersQuery.data ?? [];
  const visibleUsers = users.filter((user) => {
    const matchesName = `${user.display_name} ${user.username}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
    return matchesName && matchesStatus(user, status);
  }).sort((a, b) => sort === "history" ? b.history_depth - a.history_depth :
    sort === "last-run" ? (b.last_run_at ?? "").localeCompare(a.last_run_at ?? "") :
    (personName(a)).localeCompare(personName(b)));
  const navigate = useNavigate();
  const patchUser = usePatchUser();
  const toggleSelected = (id: number) => setSelected((before) => { const next = new Set(before); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const selectedUsers = users.filter((user) => selected.has(user.id));
  const selectedVisible = visibleUsers.filter((user) => selected.has(user.id));
  const stopSelecting = () => { setSelecting(false); setSelected(new Set()); setBatchError(""); };
  const pauseSelected = async (paused: boolean) => {
    setBatchBusy(true); setBatchError("");
    const failed = new Set<number>();
    for (const user of selectedUsers) {
      try { await patchUser.mutateAsync({ id: user.id, patch: { prefs: { paused } } }); }
      catch { failed.add(user.id); }
    }
    setSelected(failed); setBatchBusy(false);
    if (failed.size) setBatchError(`${failed.size} ${failed.size === 1 ? "person couldn’t" : "people couldn’t"} be updated. They stay selected; try again. Other changes were saved.`);
    // users.py: pausing takes their rows off every surface NOW (`user.pause.hide`) and resuming
    // re-promotes them — it is not "rows stay exactly as they are".
    else toast.success(paused ? "Paused. Their rows are off Home until you resume; nothing is deleted." : "Resumed for the people who are on. Their rows go back on Home.");
  };
  // ONCE for the page, never per row — see RequestsCell.
  const requestSources = useRequestRowSources("", true);

  /**
   * Toggle one person, and say so immediately.
   *
   * Turning someone OFF removes their rows from Plex and re-merges every account's share filter
   * before the request returns — seconds of real work on a shared server. Waiting for the response
   * to give feedback made the click look ignored, and the only thing that eventually appeared was a
   * generic job toast ("Remove a disabled person's rows") that named the machinery rather than the
   * decision. This names the person and the consequence, up front, and resolves in place.
   */
  const toggleUser = (user: User, enabled: boolean) => {
    const who = personName(user);
    const id = `user-toggle-${user.id}`;
    toast.loading(enabled ? `Turning on ${who}…` : `Turning off ${who}…`, {
      id,
      description: enabled
        ? "They get a row on the next run. Restoring their access to the shared rows now."
        : "Removing their rows from Plex and updating everyone's share filters.",
    });
    patchUser.mutate(
      { id: user.id, patch: { enabled } },
      {
        onSuccess: () =>
          toast.success(enabled ? `${who} is on` : `${who} is off`, {
            id,
            description: enabled
              ? "Their row is rebuilt on the next run."
              : "Their rows are off Plex and everyone else's filters are updated.",
            action: {
              label: "Jobs",
              onClick: () => navigate("/activity?tab=jobs"),
            },
          }),
        onError: (error) =>
          toast.error(`Couldn't turn ${enabled ? "on" : "off"} ${who}`, {
            id,
            description: apiErrorMessage(error, "The change was not saved."),
          }),
      },
    );
  };
  const setAll = useSetAllUsersEnabled();
  const queryClient = useQueryClient();
  const [confirmDisableOpen, setConfirmDisableOpen] = useState(false);
  const [confirmEnableOpen, setConfirmEnableOpen] = useState(false);
  // Who the owner is about to file away. Confirmed rather than one-click: it drops their pick history
  // and run history, and that is not something to undo by clicking again.
  const [removing, setRemoving] = useState<User | null>(null);
  const removeUser = useRemoveUser();
  const userCount = users.length;

  // "Add people" is the roster sync: Shortlist finds people through Plex, so adding someone means
  // sharing the server with them in Plex and pulling the roster again. The wizard syncs on its way
  // past, but an install that finished setup had NO other way to pull it — so someone newly invited
  // to Plex, and the owner's own row, never appeared.
  const sync = useMutation({
    mutationFn: api.syncUsers,
    // A sync is a Plex WRITER (it renames collections when a nickname changes), so it waits for a
    // run to finish rather than writing underneath it. Without this the button would just stop
    // spinning and nothing would visibly change — the roster is unchanged because it has not run yet.
    onSuccess: (result) =>
      result.queued
        ? toast.success("Sync queued", {
            description:
              "A run is using Plex right now, so this will happen the moment it finishes.",
          })
        : result.added || result.updated
          ? toast.success(`Synced — ${result.added} new, ${result.updated} updated`)
          : toast.success("Synced — no changes", {
              description:
                "Nobody new on plex.tv. Share your server with them in Plex first, then press Add people again.",
            }),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
  const hasOwner = users.some((user) => user.user_type === "owner");

  return (
    <div>
      <PageHeader
        title="Users"
        subtitle="Who gets a row, whether it’s private, and whether they watch it."
        actions={
          <>
            {/* Neither the trigger nor its items carry `loading` — they merely OPEN a confirmation; the
                mutation (and its loading state) belongs to the confirm button inside it. */}
            <HeaderPopover open={allActionsOpen} onOpenChange={setAllActionsOpen} align="right" label="All user actions" width={192} className="grid gap-2 p-2" trigger={<Button variant="outline">All users…</Button>}>
              <Button variant="outline" onClick={() => { setAllActionsOpen(false); setConfirmEnableOpen(true); }}>Enable all</Button>
              <Button variant="outline" onClick={() => { setAllActionsOpen(false); setConfirmDisableOpen(true); }}>Disable all</Button>
            </HeaderPopover>
            <Button
              onClick={() => sync.mutate()}
              loading={sync.isPending}
              title="Pulls in everyone your Plex server is shared with. Share it with someone in Plex first."
            >
              <UserPlus aria-hidden="true" />
              Add people
            </Button>
          </>
        }
      />

      {sync.isError && (
        <MutationAlert
          className="mb-4"
          error={sync.error}
          fallback="Couldn’t reach plex.tv to refresh the user list. Try again."
          onRetry={() => sync.mutate()}
        />
      )}

      {/* The Switch reads the server's answer, so a rejected PATCH just snaps it back — which is
          indistinguishable from the click never landing unless we say what happened. */}
      {patchUser.isError && (
        <MutationAlert
          className="mb-4"
          error={patchUser.error}
          fallback="Couldn’t change that user. Try again."
          onRetry={() => {
            const last = patchUser.variables;
            if (last) patchUser.mutate(last);
          }}
        />
      )}
      {setAll.isError && (
        <MutationAlert
          className="mb-4"
          error={setAll.error}
          fallback="Couldn’t update everyone at once. Try again."
        />
      )}

      <Dialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Remove {removing && personName(removing)}?
            </DialogTitle>
            <DialogDescription>
              Plex no longer has this account, so their rows are already gone
              from the server. Removing them here deletes their pick and run
              history and takes them out of this list &mdash; which also changes
              your dashboard totals, since those count every pick ever
              delivered. Their original Plex share settings are kept, so
              uninstalling Shortlist can still put this account back the way it
              found it.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoving(null)}>
              Keep them
            </Button>
            <Button
              variant="destructive"
              disabled={removeUser.isPending}
              onClick={() => {
                const target = removing;
                if (!target) return;
                removeUser.mutate(target.id, {
                  onSuccess: (result) => {
                    toast.success(
                      `${personName(target)} removed — ${result.picks_deleted} picks and ${result.runs_deleted} runs dropped`,
                    );
                    setRemoving(null);
                  },
                  onError: (e) => toast.error((e as Error).message),
                });
              }}
            >
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={confirmEnableOpen} onOpenChange={setConfirmEnableOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn on all {userCount} users?</DialogTitle>
            {/* "each user gets a row" was an over-claim: an account with a Plex restriction
                profile is skipped (Plex refuses the privacy filter for it), and a person still
                needs to be in some row's audience. */}
            <DialogDescription>
              Everyone here is switched on, so each of them gets their own
              private row the next time the rows they&rsquo;re in are built.
              Accounts Plex won&rsquo;t show collections to &mdash; the ones
              badged with a restriction profile &mdash; stay skipped. Turn
              anyone back off individually whenever you like.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmEnableOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={setAll.isPending && setAll.variables === true}
              onClick={() =>
                setAll.mutate(true, {
                  onSuccess: () => setConfirmEnableOpen(false),
                })
              }
            >
              Enable all
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmDisableOpen} onOpenChange={setConfirmDisableOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn off every user?</DialogTitle>
            {/* NOT "share filters are left untouched" — turning someone off queues a privacy pass
                that WRITES their share filter (users.py `remove_users_rows` → `queue_privacy_sync`),
                which is what stops a disabled account seeing the shared rows too. */}
            <DialogDescription>
              This turns everyone off and takes every Picked-for-You row off
              Plex right away. Each account&rsquo;s Plex sharing settings are
              updated to match, so nobody is left seeing a row that no longer
              belongs to them. The record of how sharing looked before you
              installed Shortlist is kept, so a full uninstall can still put it
              back &mdash; and turning anyone back on rebuilds their row on its
              next run.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setConfirmDisableOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={setAll.isPending && setAll.variables === false}
              onClick={() =>
                setAll.mutate(false, {
                  onSuccess: () => setConfirmDisableOpen(false),
                })
              }
            >
              Disable all
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <QueryBoundary
        query={usersQuery}
        skeleton={<UsersSkeleton />}
        isEmpty={(list) => list.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Shortlist hasn’t found anyone on your server. Press “Add people” above to ask plex.tv again — and if that turns up nothing, check the server address and token in Settings → Connections, on the Plex card."
          />
        }
      >
        {(list) => (
          <div className="space-y-4">
            {hasOwner && <OwnerNote />}
            {/* Once for the page: per person it was the same dash forty times over. */}
            {requestSources.data && !requestSources.isError && noRequestSource(requestSources.data) && (
              <p className="flex items-start gap-2 text-sm text-muted-foreground">
                <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                <span>
                  No request source is connected, so nobody&rsquo;s missing picks can be requested.{" "}
                  <Link to="/settings/connections" className="text-accent-foreground underline underline-offset-4">
                    Set one up in Connections
                  </Link>
                </span>
              </p>
            )}
            <div role="group" aria-label="Filter and sort users" className="flex flex-wrap items-center gap-3">
            <div className="relative w-full sm:w-60" role="search" aria-label="Find a user">
              <Search aria-hidden="true" className="absolute left-3 top-2.5 size-4 text-faint-foreground" />
              <Input type="search" aria-label="Search users" placeholder="Find a person…" value={search} onChange={(event) => setSearch(event.target.value)} className="pl-9" />
            </div>
            <Segmented<StatusFilter>
              joined
              ariaLabel="Show"
              value={status}
              onChange={setStatus}
              options={STATUS_FILTERS.map(({ value, label }) => ({
                value,
                label: (
                  <>
                    {label}
                    <span className="ml-1.5 tabular-nums opacity-70">{users.filter((user) => matchesStatus(user, value)).length}</span>
                  </>
                ),
              }))}
            />
              <label className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">Sort by<select aria-label="Sort users" value={sort} onChange={(event) => setSort(event.target.value)} className="h-9 rounded-md border bg-background px-2 text-sm text-foreground"><option value="name">Name A–Z</option><option value="history">Most watch history</option><option value="last-run">Last run</option></select></label>
            </div>
            <div role="group" aria-label="User list controls" className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 text-xs text-muted-foreground">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                {selecting ? (
                  <>
                    <Button size="sm" variant="outline" disabled={batchBusy} onClick={stopSelecting}>Done selecting</Button>
                    <label className={`flex cursor-pointer items-center gap-2 text-foreground ${coarseHitArea}`}>
                      <input type="checkbox" aria-label="Select visible users" checked={visibleUsers.length > 0 && selectedVisible.length === visibleUsers.length} ref={(box) => { if (box) box.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleUsers.length; }} disabled={batchBusy || visibleUsers.length === 0} onChange={(event) => setSelected((before) => { const next = new Set(before); for (const user of visibleUsers) { if (event.target.checked) next.add(user.id); else next.delete(user.id); } return next; })} className="size-4 shrink-0 accent-primary" />
                      Select visible
                    </label>
                  </>
                ) : (
                  <Button size="sm" variant="outline" onClick={() => setSelecting(true)} title="Pick people to pause or resume together">Select people</Button>
                )}
                <p role="status">Showing {visibleUsers.length} of {list.length} people</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {/* Always here, and deliberately not behind the owner note — that note is dismissible,
                    and dismissing "you see everyone's rows" is how people say "yes, I know" rather than
                    "I never want the tool again". Before this, hiding the note hid the only way back to
                    it short of remembering the URL. */}
                {/* Beside the people it is about, not in a Settings section: "is everyone's row actually
                    hidden from everyone else" is a question the owner asks while looking at this list. */}
                <Button size="sm" variant="ghost" asChild>
                  <Link to="/privacy">
                    <ShieldCheck aria-hidden="true" />
                    Privacy
                  </Link>
                </Button>
                {hasOwner && (
                  <Button size="sm" variant="ghost" asChild>
                    <Link to="/watching-account?setup=1">
                      <Eye aria-hidden="true" />
                      Watching account
                    </Link>
                  </Button>
                )}
              </div>
            </div>
            {selecting && selectedUsers.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border-strong bg-elevated px-4 py-3"><div className="text-sm"><strong>{selectedUsers.length} selected</strong><button type="button" className="ml-3 text-xs text-muted-foreground underline" disabled={batchBusy} onClick={() => setSelected(new Set())}>Clear</button><p className="mt-1 text-xs text-muted-foreground">Pausing takes their rows off Home until you resume; nothing is deleted. Off accounts stay off.</p></div><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={batchBusy} onClick={() => void pauseSelected(false)}>Resume rebuilding</Button><Button size="sm" variant="outline" loading={batchBusy} onClick={() => void pauseSelected(true)}>Pause rebuilding</Button></div></div>}
            {batchError && <p role="alert" className="text-sm text-destructive-text">{batchError}</p>}
            {visibleUsers.length === 0 ? (
              <EmptyState title="No matching users" hint="Try another name or status." action={<Button variant="outline" onClick={() => { setSearch(""); setStatus("all"); }}>Clear filters</Button>} />
            ) : (
              <div className="overflow-hidden rounded-xl border bg-card">
                <Table>
                  <TableHeader className="hidden lg:table-header-group">
                    <TableRow className="hover:bg-transparent"><TableHead className="pl-4">Person</TableHead><TableHead>Status</TableHead><TableHead className="text-right">Rows</TableHead><TableHead className="text-right lg:pr-6">Picks watched<span className="block text-xs font-normal text-faint-foreground">30 days</span></TableHead><TableHead>Privacy</TableHead><TableHead>Last run</TableHead><TableHead className="pr-4 text-right"><span className="sr-only">Shortlist row on or off</span></TableHead></TableRow>
                  </TableHeader>
                  <TableBody className="grid lg:table-row-group">
                    {visibleUsers.map((user) => <TableRow key={user.id} className={`grid grid-cols-2 gap-x-4 gap-y-3 px-4 py-4 lg:table-row lg:p-0 [&>td]:p-0 lg:[&>td]:px-3 lg:[&>td]:py-3 ${selecting && selected.has(user.id) ? "bg-raised" : ""}`}>
                      <TableCell className="col-span-2 min-w-0 lg:col-span-1 lg:w-[30%] lg:pl-4">
                        <div className="flex items-start gap-3">
                          {selecting && <label className={`mt-1.5 flex shrink-0 cursor-pointer ${coarseHitArea}`}><input type="checkbox" aria-label={`Select ${personName(user)}`} checked={selected.has(user.id)} disabled={batchBusy} onChange={() => toggleSelected(user.id)} className="size-4 shrink-0 accent-primary" /></label>}
                          <UserAvatar name={user.username} size="sm" />
                          <div className="min-w-0 space-y-1">
                            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                              <Link to={`/users/${user.id}`} className="min-w-0 break-words font-semibold hover:text-primary hover:underline" title={`Plex username: ${user.username}`}>{personName(user)}</Link>
                              <RestrictedPill user={user} />
                            </div>
                            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                              <span>{USER_TYPE_LABEL[user.user_type] ?? user.user_type}</span>
                              {user.display_name && user.display_name !== user.username && <span className="break-all" title="Plex username">{user.username}</span>}
                              <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1"><span className="whitespace-nowrap tabular-nums">{user.history_depth} titles watched</span><ColdStartBadge user={user} /></span>
                            </div>
                            {(!user.manage_sharing || user.departed) && <div className="flex flex-wrap gap-1.5"><SharingUntouchedBadge user={user} /><DepartedBadge user={user} /></div>}
                            <div className="text-xs text-muted-foreground"><RequestsCell user={user} sources={requestSources} /></div>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="col-span-2 empty:hidden lg:col-span-1 lg:empty:table-cell"><StatePill user={user} /></TableCell>
                      <TableCell className="text-sm lg:text-right"><CellLabel>Rows</CellLabel><RowsCell user={user} collections={collections} /></TableCell>
                      <TableCell className="text-sm lg:pr-6 lg:text-right"><CellLabel>Picks watched (30 days)</CellLabel><PicksCell user={user} /></TableCell>
                      <TableCell className={`text-sm ${exposedRows(user, privacy) > 0 ? "bg-warning/10 text-warning lg:px-3" : ""} lg:min-w-44`}><CellLabel>Privacy</CellLabel><PrivacyCell user={user} privacy={privacy} /></TableCell>
                      <TableCell className="whitespace-nowrap text-sm" title={user.last_run_at ? formatDate(user.last_run_at) : undefined}><CellLabel>Last run</CellLabel>{builtAt(user.last_run_at)}</TableCell>
                      <TableCell className="col-span-2 flex items-center justify-end gap-2 whitespace-nowrap lg:table-cell lg:pr-4 lg:text-right">
                        <GatedSwitch
                          checked={user.enabled && !user.restriction_profile}
                          reason={
                            user.restriction_profile
                              ? `Plex's ${profileName(user)} restriction profile is set on this account — Plex won't let Shortlist hide anything from it, so a row here couldn't be kept private. Clear the Restriction Profile in Plex (Settings → Users & Sharing) to enable.`
                              : undefined
                          }
                          onCheckedChange={(enabled) =>
                            toggleUser(user, enabled)
                          }
                          aria-label={`Shortlist row for ${user.username}`}
                        />
                        {/* Only for someone Plex no longer lists. On an active account this would
                            read as "delete this user" — dropping their history while the nightly
                            run carries on rebuilding their row. */}
                        {user.departed && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="ml-2 text-destructive-text"
                            onClick={() => setRemoving(user)}
                          >
                            Remove
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>)}
                  </TableBody>
                </Table>
              </div>
            )}

            <p className="text-xs leading-relaxed text-muted-foreground">
              <strong className="font-semibold text-foreground">On</strong>: rows run on each row&rsquo;s schedule.{" "}
              <strong className="font-semibold text-foreground">Paused</strong>: their rows come off Home and Recommended and stop updating; resuming puts them back, nothing is deleted.{" "}
              <strong className="font-semibold text-foreground">Off</strong>: their rows come off Plex.{" "}
              <strong className="font-semibold text-foreground">Restricted</strong> is Plex&rsquo;s own parental-control profile; Shortlist never changes it.
            </p>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
