import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { usePrivacyStatus } from "@/lib/queries";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * "Did the hiding actually happen?" — answered from live reads, and only where an answer exists.
 *
 * Everything on this page is either a reading taken just now or the words "not checked". Nothing is
 * derived from what Shortlist WROTE: a recorded filter write proves an intention reached plex.tv, not
 * that plex.tv still stores it, and rendering one as "hidden" would be a false-privacy bug on the one
 * screen whose whole job is to be believed.
 *
 * The three things it deliberately refuses to say are as important as the three it says:
 *
 *  - **Nothing outside Home.** Shortlist has no recorded answer for whether Plex applies a share
 *    `label!=` filter to the Collections tab or to Related shelves, so the page says so instead of
 *    implying coverage it cannot back.
 *  - **Nothing green off a failed read.** A plex.tv outage, or a PMS read that came back empty,
 *    renders as "not current"/"unknown" — never as a clean bill of health.
 *  - **Nothing about the owner's own view.** Plex has no share for the account that owns the server,
 *    so the owner sees every row. That is Plex, not a fault, and saying otherwise would train the
 *    owner to ignore this page.
 */
export function SharingPage() {
  const query = usePrivacyStatus();

  return (
    <div className="space-y-6">
      <PageHeader
        icon={ShieldCheck}
        title="Sharing and privacy"
        subtitle="Whether each person's Plex account is set to hide the rows that aren't theirs, read live from plex.tv."
      />
      <QueryBoundary
        query={query}
        skeleton={
          <div className="space-y-3">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-48 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
        }
        isEmpty={(data) => data.accounts.length === 0 && !data.error}
        empty={
          <EmptyState
            icon={ShieldCheck}
            title="No Plex accounts to check"
            hint="plex.tv hasn't shared this server with anyone yet, so there are no share filters to read. Invite someone in Plex and this page fills in."
          />
        }
      >
        {(data) => (
          <div className="space-y-6">
            <Summary data={data} onRetry={() => void query.refetch()} />
            <AccountsTable accounts={data.accounts} />
            <EnforcementPanel data={data} />
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}

/**
 * The headline: the worst true thing, never an average, and never green off a failed read.
 *
 * Switches on `data.summary`, which the SERVER decides. It used to re-derive the verdict from
 * `error`/`rows_error`/`accounts[].state` — two implementations of the same ranking, and only the
 * server's one was documented. They had already diverged: the server ranks a measured enforcement
 * failure above everything but a failed read, and this file did not look at enforcement at all, so
 * it printed "Every account hides all N rows" directly above the panel saying Plex was ignoring the
 * filter. Only the detail text is drawn from the raw fields.
 */
function Summary({
  data,
  onRetry,
}: {
  data: PrivacyStatus;
  onRetry: () => void;
}) {
  if (data.summary === "unreadable") {
    return (
      <Banner tone="bad" role="alert">
        <p>
          Couldn't read your Plex sharing settings from plex.tv, so nothing
          below is current. {data.error}
        </p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </Banner>
    );
  }
  if (data.summary === "rows_unknown") {
    return (
      <Banner tone="bad" role="alert">
        <p>
          Couldn't read your rows from Plex, so there's nothing to check the
          filters against. The filters below are real — what's unknown is
          whether they cover every row. {data.rows_error}
        </p>
        <Button variant="outline" size="sm" onClick={onRetry}>
          Try again
        </Button>
      </Banner>
    );
  }
  if (data.summary === "not_enforced") {
    // Stored is not enforced. These accounts DO carry every hide rule — that is why the enforcement
    // check looked at them at all — so the "missing rules" wording below would be actively wrong,
    // and so would anything green.
    const who = Object.keys(data.enforcement.not_enforced);
    return (
      <Banner tone="bad" role="alert">
        <p>
          <strong>
            Plex saved every hide rule and is showing other people's rows
            anyway.
          </strong>{" "}
          Checked through {who.join(", ")}
          {who.length === 1 ? "'s" : "'"} own eyes in run #
          {data.enforcement.run_id}. Nothing below is wrong — the rules really
          are on the filters. Plex is not applying them.
        </p>
      </Banner>
    );
  }

  const short = data.accounts.filter((a) => a.state === "missing");
  if (data.summary === "missing") {
    return (
      <Banner tone="bad" role="alert">
        <p>
          <strong>
            {short.length === 1
              ? `${short[0]?.display_name} can see a row that isn't theirs.`
              : `${short.length} people can see a row that isn't theirs.`}
          </strong>{" "}
          Their Plex account is missing a hide rule. The next run merges it back
          in; if it keeps coming back, something else is rewriting their share
          filter.
        </p>
      </Banner>
    );
  }
  if (data.rows_on_plex.length === 0) {
    return (
      <Banner tone="neutral">
        <p>
          No per-person rows exist on Plex yet, so there's nothing for anyone to
          hide. <ReadAt at={data.read_at} />
        </p>
      </Banner>
    );
  }
  return (
    <Banner tone="good">
      <p>
        Every account hides all {data.rows_on_plex.length}{" "}
        {data.rows_on_plex.length === 1 ? "row" : "rows"} that aren't theirs.{" "}
        <ReadAt at={data.read_at} />
      </p>
    </Banner>
  );
}

function Banner({
  tone,
  role,
  children,
}: {
  tone: "good" | "bad" | "neutral";
  role?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role={role}
      className={cn(
        "flex flex-col items-start gap-3 rounded-lg border p-4 text-sm sm:flex-row sm:items-center sm:justify-between",
        tone === "bad" && "border-destructive/40 bg-destructive/10",
        tone === "good" && "border-success/40 bg-success/10",
        tone === "neutral" && "bg-muted/40",
      )}
    >
      {children}
    </div>
  );
}

/** The provenance, said out loud. A reading with no timestamp reads as a standing guarantee. */
function ReadAt({ at }: { at: string }) {
  return (
    <span className="text-muted-foreground">
      Read from plex.tv at {new Date(at).toLocaleTimeString()}.
    </span>
  );
}

/** What each state means, in the owner's terms. The evidence is in the sentence, never a bare tick. */
const STATE_COPY: Record<string, { label: string; tone: string }> = {
  hiding: {
    label: "Hiding every row — read from plex.tv just now",
    tone: "text-success",
  },
  missing: { label: "Missing hide rules", tone: "text-destructive-text" },
  left_alone: {
    label: "Left alone by choice, so it hides nothing",
    tone: "text-muted-foreground",
  },
  refused_by_plex: {
    label: "Plex won't accept hide rules for this account",
    tone: "text-warning",
  },
  owner: { label: "You own the server", tone: "text-muted-foreground" },
  unknown: { label: "Not checked", tone: "text-muted-foreground" },
};

function AccountsTable({ accounts }: { accounts: AccountPrivacy[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Accounts</CardTitle>
        <CardDescription>
          One row per Plex account, with the hide rules its share filter carries
          right now.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {/* Its own scroller: a long row name must never push the whole page sideways. */}
        <div className="overflow-x-auto">
          <ul className="min-w-[20rem] divide-y">
            {accounts.map((account) => (
              <AccountRow key={account.account_id} account={account} />
            ))}
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}

function AccountRow({ account }: { account: AccountPrivacy }) {
  const [open, setOpen] = useState(false);
  const copy = STATE_COPY[account.state] ?? STATE_COPY.unknown;

  return (
    <li className="flex flex-col gap-2 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
      <div className="flex min-w-0 items-start gap-3">
        <UserAvatar name={account.display_name} />
        <div className="min-w-0">
          <p className="truncate font-medium">
            {account.user_id !== null ? (
              <Link
                to={`/users/${account.user_id}`}
                className="underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none"
              >
                {account.display_name}
              </Link>
            ) : (
              account.display_name
            )}
          </p>
          <p className={cn("text-sm", copy?.tone)}>{copy?.label}</p>
          {account.state === "missing" && (
            <p className="mt-1 text-sm text-muted-foreground">
              Can see: {account.missing.join(", ")}
            </p>
          )}
          {account.state === "owner" && (
            <p className="mt-1 text-sm text-muted-foreground">
              Plex has no share to filter for you, so your Home shows every row.
              That's Plex, not a fault. To see what your users see, watch on a
              non-owner account.
            </p>
          )}
          {account.state === "left_alone" && (
            <p className="mt-1 text-sm text-muted-foreground">
              You asked Shortlist to leave this account's Plex sharing alone.
              That's the setting, not a fault.
            </p>
          )}
          {account.state === "refused_by_plex" && (
            <p className="mt-1 text-sm text-muted-foreground">
              Plex rejects hide rules for an account with a restriction profile
              ({account.restriction_profile}). Clear the profile in Plex and the
              next run can hide their view.
            </p>
          )}
          {account.user_id === null && (
            // A share added since the last user sync. Every attribute beside the filter is a
            // fallback (`user_type` reads "shared", `restriction_profile` reads ""), so the state
            // beside it may be wrong in the one direction that matters — a parental-profile account
            // would be badged "missing hide rules" when Plex simply refuses them.
            <p className="mt-1 text-sm text-muted-foreground">
              Plex knows this account, Shortlist hasn't synced it yet — run Sync
              users on the Users page for a reliable answer.
            </p>
          )}
          {account.other_conditions.length > 0 && (
            <div className="mt-2">
              <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                aria-expanded={open}
                className="text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none"
              >
                {open ? "Hide" : "Show"} their own filters (
                {account.other_conditions.length})
              </button>
              {open && (
                <ul className="mt-1 space-y-0.5">
                  {/* Indexed key: a filter can legitimately repeat a condition string, and two
                      identical keys make React drop one of them — on the list whose whole job is to
                      show the owner that we preserved their filters exactly. */}
                  {account.other_conditions.map((condition, index) => (
                    <li
                      key={`${index}-${condition}`}
                      className="break-all font-mono text-xs text-muted-foreground"
                    >
                      {condition}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      </div>
      {/* A number, not a tick: "12 of 12" and "0 of 0" must not look alike. */}
      <p className="shrink-0 whitespace-nowrap text-sm tabular-nums text-muted-foreground sm:pt-1">
        {account.state === "owner" || account.state === "unknown"
          ? "—"
          : `Hides ${account.hides.length} of ${account.should_hide.length}`}
      </p>
    </li>
  );
}

/**
 * A different question, kept visually apart from the table.
 *
 * The table says plex.tv is STORING a rule. This says a run looked through a real account's eyes and
 * saw whether Plex ACTS on it. `measured` is what keeps the two apart: an empty result on a run that
 * never looked is "nobody checked", and rendering that as "all clear" is exactly how a live alert
 * gets cleared.
 */
function EnforcementPanel({ data }: { data: PrivacyStatus }) {
  const { measured, run_id, measured_at, not_enforced } = data.enforcement;
  const exposed = Object.keys(not_enforced);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Is Plex applying the rules?</CardTitle>
        <CardDescription>
          Storing a hide rule and acting on it are different things, so each run
          reads one account of each kind as that person.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!measured && (
          <p className="text-muted-foreground">
            <strong className="text-foreground">Not checked recently.</strong>{" "}
            The last few runs didn't get as far as looking, so nothing here says
            whether Plex is applying the rules.
          </p>
        )}
        {measured && exposed.length === 0 && (
          <p>
            Checked in run #{run_id}
            {measured_at ? ` on ${new Date(measured_at).toLocaleString()}` : ""}
            : Plex was applying the hide rules on the accounts checked.
          </p>
        )}
        {measured && exposed.length > 0 && (
          <p className="text-destructive-text" role="alert">
            <strong>Plex is ignoring the privacy filter.</strong>{" "}
            {exposed.join(", ")} can see rows belonging to other people even
            though Plex saved the hide rules. Shortlist checks one account of
            each kind, so this is likely every shared or managed account, not
            only the {exposed.length === 1 ? "one" : "ones"} named. Please open
            an issue — there is no setting here that fixes it.
          </p>
        )}
        <p className="text-muted-foreground">
          These checks cover the Home screen. Shortlist has no way to confirm
          what Plex does on the Collections tab or in Related shelves.
        </p>
      </CardContent>
    </Card>
  );
}
