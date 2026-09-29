import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, RefreshCw, Search, ShieldCheck, Users as UsersIcon } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router";

import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";

import { MutationAlert } from "@/components/mutation-alert";
import { OwnerNote } from "@/components/owner-note";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
import {
  ColdStartBadge,
  DepartedBadge,
  RestrictedBadge,
  SharingUntouchedBadge,
  UnhiddenRowsBadge,
  UserTypeBadge,
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
import { profileName } from "@/lib/user-profile";
import type { RowSources, User } from "@/lib/types";
import { formatHitRate, timeAgo } from "@/lib/format";
import {
  queryKeys,
  useHitRatesMatured,
  useRemoveUser,
  useRequestRowSources,
  useSetAllUsersEnabled,
  usePatchUser,
  useUsers,
} from "@/lib/queries";

/** Request context for one person: whether a Your requests row can find anything of theirs.
 *
 *  Reads the ONE row-sources query the page makes (`useRequestRowSources("")` costs up to a few
 *  dozen HTTP calls to Overseerr and the Arrs, so it is never made per row). A dash carries its
 *  reason in `title`: the dashes — can't read the sources, nothing connected, Overseerr not
 *  connected — would otherwise be indistinguishable from each other and from "no requests". */
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
      <span title={reason} aria-label={reason} data-empty-request={reason === "No request source connected" || reason === "Overseerr isn’t connected" ? "true" : undefined}>
        <span className="hidden lg:inline">—</span><span className="lg:hidden">{reason}</span>
      </span>,
      null,
    );
  const data = sources.data;
  if (sources.isError || !data) return dash(unreadableSources(data));
  if ([data.overseerr, data.radarr, data.sonarr].every((s) => s === "off")) {
    return dash("No request source connected");
  }
  // The endpoint never fails: a configured-but-down Overseerr answers 200 with nobody linked, which
  // would read as every shared person lacking an account and every managed one unable to get one.
  if (data.overseerr === "unreachable") return dash("Couldn’t read Overseerr");

  const person = data.people.find((p) => p.user_id === user.id);
  const ready = person?.ready ?? 0;
  const readyNote = ready > 0 ? `${ready} ready` : null;
  // Without Overseerr only the tag applies — "hasn't signed in" would blame them for an account
  // that can't exist — but tag-credited titles still count as ready.
  if (data.overseerr === "off") {
    return tag || readyNote
      ? cell(null, readyNote)
      : dash("Overseerr isn’t connected");
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
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState("name");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchError, setBatchError] = useState("");
  const matchesStatus = (user: User, value: string) => value === "all" ||
    (value === "active" && user.enabled && !user.prefs.paused && !user.restriction_profile) ||
    (value === "paused" && user.enabled && user.prefs.paused) ||
    (value === "off" && !user.enabled) ||
    (value === "attention" && Boolean(user.restriction_profile || user.unhidden_rows || user.departed));
  const statusOptions = [["all", "All"], ["active", "Active"], ["paused", "Paused"], ["off", "Off"], ["attention", "Needs attention"]] as const;
  const attentionCount = (usersQuery.data ?? []).filter((user) => matchesStatus(user, "attention")).length;
  const visibleUsers = (usersQuery.data ?? []).filter((user) => {
    const matchesName = `${user.display_name} ${user.username}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase());
    return matchesName && matchesStatus(user, status);
  }).sort((a, b) => sort === "history" ? b.history_depth - a.history_depth :
    sort === "last-run" ? (b.last_run_at ?? "").localeCompare(a.last_run_at ?? "") :
    (a.display_name || a.username).localeCompare(b.display_name || b.username));
  const navigate = useNavigate();
  const patchUser = usePatchUser();
  const toggleSelected = (id: number) => setSelected((before) => { const next = new Set(before); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const selectedUsers = (usersQuery.data ?? []).filter((user) => selected.has(user.id));
  const selectedVisible = visibleUsers.filter((user) => selected.has(user.id));
  const pauseSelected = async (paused: boolean) => {
    setBatchBusy(true); setBatchError("");
    const failed = new Set<number>();
    for (const user of selectedUsers) {
      try { await patchUser.mutateAsync({ id: user.id, patch: { prefs: { paused } } }); }
      catch { failed.add(user.id); }
    }
    setSelected(failed); setBatchBusy(false);
    if (failed.size) setBatchError(`${failed.size} ${failed.size === 1 ? "person couldn’t" : "people couldn’t"} be updated. They stay selected; try again. Other changes were saved.`);
    else toast.success(paused ? "Rebuilding paused. Existing rows stay on Plex." : "Rebuilding resumed for enabled people.");
  };
  const ratesMatured = useHitRatesMatured();
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
    const who = user.display_name || user.username;
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
              onClick: () => navigate("/jobs"),
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
  const userCount = usersQuery.data?.length ?? 0;

  // The wizard syncs on its way past, but an install that finished setup had NO way to pull the
  // roster again — so someone newly invited to Plex, and the owner's own row, never appeared.
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
        : toast.success(
            result.added || result.updated
              ? `Synced — ${result.added} new, ${result.updated} updated`
              : "Synced — no changes",
          ),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

  return (
    <div>
      <PageHeader
        icon={UsersIcon}
        title="Users"
        subtitle="Who’s getting recommendations, and who needs a look."
        className="[&>div>span]:hidden"
        actions={
          // Wraps: three buttons need 345px in one line and ran off a 320px screen.
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => sync.mutate()}
              loading={sync.isPending}
              title="Pull the latest users from Plex and friendly names from Tautulli (if connected)"
            >
              <RefreshCw aria-hidden="true" />
              Sync users
            </Button>
            {/* Neither trigger carries `loading` — both merely OPEN a dialog; the mutation (and its
                loading state) belongs to the confirm button inside it. */}
            {/* Beside the people it is about, not in a Settings section: "is everyone's row
                actually hidden from everyone else" is a question the owner asks while looking at
                this list. */}
            <Button variant="outline" asChild>
              <Link to="/sharing">
                <ShieldCheck aria-hidden="true" />
                Sharing and privacy
              </Link>
            </Button>
            <details className="relative">
              <summary className="flex h-9 cursor-pointer list-none items-center rounded-md border px-3 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">All users…</summary>
              <div className="absolute right-0 z-20 mt-2 grid min-w-40 gap-2 rounded-md border bg-popover p-2 shadow-md">
            <Button
              variant="outline"
              onClick={() => setConfirmEnableOpen(true)}
            >
              Enable all
            </Button>
            <Button
              variant="outline"
              onClick={() => setConfirmDisableOpen(true)}
            >
              Disable all
            </Button>
              </div>
            </details>
            {/* Always here, and deliberately not behind the owner note — that note is dismissible,
                and dismissing "you see everyone's rows" is how people say "yes, I know" rather than
                "I never want the tool again". Before this, hiding the note hid the only way back to
                it short of remembering the URL. */}
            {(usersQuery.data ?? []).some(
              (user) => user.user_type === "owner",
            ) && (
              <Button variant="outline" asChild>
                <Link to="/watching-account?setup=1">
                  <Eye aria-hidden="true" />
                  Watching account
                </Link>
              </Button>
            )}
          </div>
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
              Remove {removing?.display_name || removing?.username}?
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
                      `${target.display_name || target.username} removed — ${result.picks_deleted} picks and ${result.runs_deleted} runs dropped`,
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
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Shortlist hasn’t found anyone on your server. Press “Sync users” above to ask plex.tv again — and if that turns up nothing, check the server address and token in Settings → Connections, on the Plex card."
          />
        }
      >
        {(users) => (
          <div className="space-y-4">
            {users.some((user) => user.user_type === "owner") && <OwnerNote />}
            <div className="flex flex-wrap items-center justify-between gap-4" role="search" aria-label="Find a user">
              <div className="relative w-full sm:max-w-xs"><Search aria-hidden="true" className="absolute left-3 top-2.5 size-4 text-muted-foreground" /><Input type="search" aria-label="Search users" placeholder="Find a person…" value={search} onChange={(event) => setSearch(event.target.value)} className="pl-9" /></div>
              <div aria-label="User status" className="flex flex-wrap gap-1">{statusOptions.map(([value, label]) => <button key={value} type="button" aria-pressed={status === value} onClick={() => setStatus(value)} className={`rounded-md border px-3 py-2 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${status === value ? "border-primary/30 bg-primary/10 text-primary" : "border-transparent text-muted-foreground hover:bg-muted hover:text-foreground"}`}>{label}<span className="ml-2 opacity-70">{users.filter((user) => matchesStatus(user, value)).length}</span></button>)}</div>
            </div>
            {attentionCount > 0 && <div className="flex items-center justify-between gap-3 rounded-lg border border-warning/25 bg-warning/5 px-4 py-3 text-sm"><p className="text-muted-foreground"><strong className="font-medium text-warning">{attentionCount} {attentionCount === 1 ? "person needs" : "people need"} attention.</strong> Check their Plex sharing or account status.</p><Button variant="ghost" size="sm" className="shrink-0 text-warning" onClick={() => setStatus("attention")}>Review →</Button></div>}
            {selectedUsers.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-primary/30 bg-primary/5 px-4 py-3"><div className="text-sm"><strong>{selectedUsers.length} selected</strong><button type="button" className="ml-3 text-xs text-muted-foreground underline" disabled={batchBusy} onClick={() => setSelected(new Set())}>Clear</button><p className="mt-1 text-xs text-muted-foreground">Pausing keeps current rows on Plex. Off accounts stay off.</p></div><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={batchBusy} onClick={() => void pauseSelected(false)}>Resume rebuilding</Button><Button size="sm" variant="outline" loading={batchBusy} onClick={() => void pauseSelected(true)}>Pause rebuilding</Button></div></div>}
            {batchError && <p role="alert" className="text-sm text-destructive-text">{batchError}</p>}
            {visibleUsers.length === 0 && <EmptyState title="No matching users" hint="Try another name or status." action={<Button variant="outline" onClick={() => { setSearch(""); setStatus("all"); }}>Clear filters</Button>} />}
            <div className="overflow-hidden rounded-xl border bg-card">
              <Table>
                <TableHeader className="hidden lg:table-header-group bg-muted/20">
                  <TableRow className="hover:bg-transparent"><TableHead className="w-10 pl-4"><input type="checkbox" aria-label="Select visible users" checked={visibleUsers.length > 0 && selectedVisible.length === visibleUsers.length} ref={(box) => { if (box) box.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleUsers.length; }} disabled={batchBusy || visibleUsers.length === 0} onChange={(event) => setSelected((before) => { const next = new Set(before); for (const user of visibleUsers) { if (event.target.checked) next.add(user.id); else next.delete(user.id); } return next; })} className="size-4 accent-primary" /></TableHead><TableHead>Person</TableHead><TableHead>Rebuilding</TableHead><TableHead>Watch history</TableHead><TableHead>Last run</TableHead><TableHead className="pr-4 text-right">Enabled</TableHead></TableRow>
                </TableHeader>
                <TableBody className="grid lg:table-row-group">
                  {visibleUsers.map((user) => <TableRow key={user.id} className={`group flex flex-wrap gap-x-3 gap-y-2 px-4 py-3 lg:table-row lg:p-0 [&>td]:p-0 lg:[&>td]:px-3 lg:[&>td]:py-3 ${selected.has(user.id) ? "bg-primary/5" : ""}`}>
                    <TableCell className="col-start-1 row-start-1 align-top lg:pl-4"><input type="checkbox" aria-label={`Select ${user.display_name || user.username}`} checked={selected.has(user.id)} disabled={batchBusy} onChange={() => toggleSelected(user.id)} className="mt-1.5 size-4 accent-primary" /></TableCell>
                    <TableCell className="col-start-2 row-start-1 min-w-0 basis-[calc(100%-5rem)] lg:basis-auto lg:w-[42%]">
                      <div className="flex items-start gap-3">
                        <UserAvatar name={user.username} size="sm" />
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <Link to={`/users/${user.id}`} className="min-w-0 break-words font-medium hover:text-primary hover:underline" title={`Plex username: ${user.username}`}>{user.display_name || user.username}</Link>
                            <UserTypeBadge user={user} />
                          </div>
                          {(user.restriction_profile || user.unhidden_rows || !user.manage_sharing || user.departed) ? <div className="flex flex-wrap gap-1.5"><RestrictedBadge user={user} /><UnhiddenRowsBadge user={user} /><SharingUntouchedBadge user={user} /><DepartedBadge user={user} /></div> : null}
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                            {user.display_name && user.display_name !== user.username && <span className="break-all" title="Plex username">{user.username}</span>}
                            <RequestsCell user={user} sources={requestSources} />
                            <span title="Share of Shortlist’s picks this person has watched, over all time">Picks watched {!ratesMatured && (user.hit_rate ?? 0) <= 0 && "(too early)"}: <span className="tabular-nums">{formatHitRate(user.hit_rate, ratesMatured)}</span></span>
                          </div>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="order-1 ml-7 text-xs lg:ml-0 lg:text-sm"><span className={user.restriction_profile ? "text-warning" : user.enabled ? user.prefs.paused ? "text-warning" : "text-success" : "text-muted-foreground"}>{user.restriction_profile ? "Restricted" : !user.enabled ? "Off" : user.prefs.paused ? "Paused" : "Active"}</span></TableCell>
                    <TableCell className="order-1 text-xs text-muted-foreground lg:text-sm"><span className="flex flex-wrap items-center gap-2"><span className="tabular-nums">{user.history_depth} titles</span><ColdStartBadge user={user} /></span></TableCell>
                    <TableCell className="order-1 text-xs text-muted-foreground lg:text-sm"><span className="lg:hidden">Last run: </span>{timeAgo(user.last_run_at)}</TableCell>
                    <TableCell className="col-start-3 row-start-1 whitespace-nowrap text-right lg:pr-4">
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
            <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground"><p role="status">Showing {visibleUsers.length} of {users.length} people</p><label className="flex items-center gap-2">Sort by<select aria-label="Sort users" value={sort} onChange={(event) => setSort(event.target.value)} className="rounded-md border bg-background px-2 py-1.5 text-xs"><option value="name">Name A–Z</option><option value="history">Most watch history</option><option value="last-run">Latest run</option></select></label></div>
            <p className="text-xs leading-relaxed text-muted-foreground">Pausing keeps their current rows on Plex. Turning a person off removes their rows.<br />Select people to pause or resume rebuilding; Enabled controls whether Shortlist builds their rows.</p>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
